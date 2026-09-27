"""F27 evidence assembly: three tiers, four metric types, seven failure kinds.

Pure functions over fixtures — no network, no database. The user's real ES alert
appears again here because it is the sample that proves the degraded path is a
normal path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.metric_budget import (
    MAX_SERIES_PER_QUERY,
    BudgetExceeded,
    BudgetReason,
    QueryWindow,
)
from app.services.metric_evidence import (
    Curve,
    EvidenceFailureKind,
    ExprOrigin,
    MetricType,
    Tier,
    build_bundle,
    classify_metric_type,
    curve_from_matrix,
    curve_title,
    curve_id,
    fact_id,
    finalize_primary,
    is_cumulative,
    parse_matrix,
    plan_primary,
    rule_expression_for,
)

ES_EXPR = (
    '(elasticsearch_cluster_health_status{color="green"} == 0) '
    'or (elasticsearch_cluster_health_status{color="yellow"} == 1)'
)
ES_URL = (
    "http://prometheus-base-prometheus-0:9090/graph"
    "?g0.expr=%28elasticsearch_cluster_health_status%7Bcolor%3D%22green%22%7D"
    "+%3D%3D+0%29+or+%28elasticsearch_cluster_health_status"
    "%7Bcolor%3D%22yellow%22%7D+%3D%3D+1%29&g0.tab=1"
)
END = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
WINDOW = QueryWindow(start=END - timedelta(hours=2), end=END, step_seconds=60)


def _matrix(*series):
    return {"resultType": "matrix", "result": list(series)}


def _one(labels, points):
    return {"metric": labels, "values": [[ts, str(v)] for ts, v in points]}


# --------------------------------------------------------------------------
# expression source order
# --------------------------------------------------------------------------


def test_rules_endpoint_wins_over_the_generator_url() -> None:
    plan, failure = plan_primary(rule_expr="up == 0", generator_url=ES_URL)
    assert failure is None
    assert plan.expr_origin is ExprOrigin.RULES_API


def test_generator_url_is_used_when_the_rules_endpoint_is_empty() -> None:
    """Thanos Query with no Ruler behind it is a normal deployment shape."""

    plan, failure = plan_primary(rule_expr=None, generator_url=ES_URL)
    assert failure is None
    assert plan.expr_origin is ExprOrigin.GENERATOR_URL
    assert plan.source_expression == ES_EXPR


def test_blank_rule_expression_falls_through_rather_than_winning() -> None:
    plan, _ = plan_primary(rule_expr="   ", generator_url=ES_URL)
    assert plan.expr_origin is ExprOrigin.GENERATOR_URL


def test_no_expression_anywhere_is_a_named_failure() -> None:
    plan, failure = plan_primary(rule_expr=None, generator_url=None)
    assert plan is None
    assert failure.kind is EvidenceFailureKind.EXPR_UNAVAILABLE
    assert failure.subject == "primary"


# --------------------------------------------------------------------------
# the three tiers
# --------------------------------------------------------------------------


def test_tier_one_keeps_the_value_and_the_threshold() -> None:
    plan, _ = plan_primary(
        rule_expr="node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1",
        generator_url=None,
    )
    assert plan.tier is Tier.THRESHOLD
    assert plan.threshold == pytest.approx(0.1)
    assert plan.expression.endswith("node_memory_MemTotal_bytes")


def test_the_real_es_alert_lands_on_tier_two() -> None:
    plan, failure = plan_primary(rule_expr=ES_EXPR, generator_url=None)
    assert failure is None
    assert plan.tier is Tier.METRIC
    assert plan.metric == "elasticsearch_cluster_health_status"
    assert plan.threshold is None


def test_tier_three_charts_the_condition_rather_than_nothing() -> None:
    """Reached when every identifier turns out to be a function call.

    Rare in practice — tier 3 is the net under the other two, not a common
    landing. `1 > 0` does *not* reach it: that strips cleanly to tier 1, which is
    correct and was worth finding out.
    """

    plan, failure = plan_primary(rule_expr="vector(1)", generator_url=None)
    assert failure is None
    assert plan.tier is Tier.EXPRESSION_RESULT
    assert plan.expression == "vector(1)"


def test_no_tier_ever_yields_a_missing_plan_once_an_expression_exists() -> None:
    for expression in (ES_EXPR, "up == 0", "1 > 0", "vector(1)", "sum(up)"):
        plan, failure = plan_primary(rule_expr=expression, generator_url=None)
        assert plan is not None and failure is None, expression
        assert plan.tier in set(Tier)


# --------------------------------------------------------------------------
# metric type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("counter", MetricType.COUNTER),
        ("COUNTER", MetricType.COUNTER),
        ("gauge", MetricType.GAUGE),
        ("histogram", MetricType.GAUGE),
        ("summary", MetricType.GAUGE),
    ],
)
def test_declared_types_are_believed(declared: str, expected: MetricType) -> None:
    assert classify_metric_type("x", {"type": declared}) is expected


def test_missing_metadata_falls_back_to_the_naming_convention() -> None:
    """And says it is guessing — a silent wrong guess is the failure mode."""

    assert classify_metric_type("x_total", None) is MetricType.UNKNOWN_SUFFIX_GUESS
    assert classify_metric_type("x_count", {}) is MetricType.UNKNOWN_SUFFIX_GUESS


def test_a_metric_with_no_hint_at_all_is_unknown_not_gauge() -> None:
    assert classify_metric_type("some_metric", None) is MetricType.UNKNOWN


def test_only_cumulative_types_get_a_rate() -> None:
    assert is_cumulative(MetricType.COUNTER)
    assert is_cumulative(MetricType.UNKNOWN_SUFFIX_GUESS)
    assert not is_cumulative(MetricType.GAUGE)
    assert not is_cumulative(MetricType.UNKNOWN)


# --------------------------------------------------------------------------
# query composition
# --------------------------------------------------------------------------


def test_tier_two_query_reapplies_the_alert_labels() -> None:
    plan, _ = plan_primary(rule_expr=ES_EXPR, generator_url=None)
    query = finalize_primary(
        plan, {"cluster": "nonprod", "alertname": "ES"}, MetricType.GAUGE
    )
    assert query == 'elasticsearch_cluster_health_status{cluster="nonprod"}'


def test_tier_two_counter_is_wrapped_in_rate() -> None:
    plan, _ = plan_primary(rule_expr="x_total", generator_url=None)
    query = finalize_primary(plan, {"pod": "p"}, MetricType.COUNTER)
    assert query == 'rate(x_total{pod="p"}[5m])'


def test_tier_one_expression_is_forwarded_untouched_even_for_a_counter() -> None:
    """Tier 1 carries the author's own expression; rewriting it is not ours to do."""

    plan, _ = plan_primary(rule_expr="rate(x_total[5m]) > 5", generator_url=None)
    assert plan.tier is Tier.THRESHOLD
    assert finalize_primary(plan, {"pod": "p"}, MetricType.COUNTER) == (
        "rate(x_total[5m])"
    )


def test_tier_three_expression_is_forwarded_untouched() -> None:
    plan, _ = plan_primary(rule_expr="vector(1)", generator_url=None)
    assert finalize_primary(plan, {"pod": "p"}, MetricType.COUNTER) == "vector(1)"


def test_title_prefers_the_stores_help_text() -> None:
    plan, _ = plan_primary(rule_expr=ES_EXPR, generator_url=None)
    assert curve_title(plan, {"help": "Cluster health status"}) == (
        "Cluster health status"
    )
    assert curve_title(plan, None) == "elasticsearch_cluster_health_status"


# --------------------------------------------------------------------------
# evidence identity
# --------------------------------------------------------------------------


def _curve_args(**overrides):
    args = dict(
        alert_fingerprint="fp1",
        occurrence_no=2,
        source_config_version=3,
        kind="PRIMARY",
        query="up",
        window_mode="RECENT",
        window_start=END - timedelta(hours=2),
        window_end=END,
    )
    args.update(overrides)
    return args


def test_curve_id_is_stable_for_the_same_inputs() -> None:
    assert curve_id(**_curve_args()) == curve_id(**_curve_args())
    assert curve_id(**_curve_args()).startswith("cv_")


def test_curve_id_is_128_bit(pytestconfig=None) -> None:
    """D49: these go into long-lived snapshots; 48 bits was needlessly short."""

    assert len(curve_id(**_curve_args())) == len("cv_") + 32


def test_a_moved_window_produces_a_different_curve_id() -> None:
    """The evidence changed, so a citation must not silently still apply."""

    assert curve_id(**_curve_args()) != curve_id(
        **_curve_args(window_start=END - timedelta(minutes=5))
    )


def test_a_changed_source_config_produces_a_different_curve_id() -> None:
    """D38: the same address pointing at a different store is different evidence."""

    assert curve_id(**_curve_args()) != curve_id(**_curve_args(source_config_version=4))


def test_different_queries_get_different_ids() -> None:
    assert curve_id(**_curve_args(query="a")) != curve_id(**_curve_args(query="b"))


def test_fact_ids_distinguish_series_and_fact_kind() -> None:
    """A model may only cite these — a whole-curve reference says nothing."""

    curve = curve_id(**_curve_args())
    a = fact_id(curve=curve, series_labels={"pod": "a"}, fact_kind="max")
    b = fact_id(curve=curve, series_labels={"pod": "b"}, fact_kind="max")
    c = fact_id(curve=curve, series_labels={"pod": "a"}, fact_kind="min")
    assert len({a, b, c}) == 3
    assert all(value.startswith("ft_") for value in (a, b, c))


def test_fact_id_ignores_label_ordering() -> None:
    curve = curve_id(**_curve_args())
    assert fact_id(
        curve=curve, series_labels={"a": "1", "b": "2"}, fact_kind="max"
    ) == fact_id(curve=curve, series_labels={"b": "2", "a": "1"}, fact_kind="max")


# --------------------------------------------------------------------------
# matrix parsing
# --------------------------------------------------------------------------


def test_matrix_is_parsed_into_labelled_series() -> None:
    series = parse_matrix(_matrix(_one({"pod": "a"}, [(1, 1.5), (2, 2.5)])))
    assert len(series) == 1
    assert series[0].labels == {"pod": "a"}
    assert series[0].points == [(1, 1.5), (2, 2.5)]


def test_unusable_samples_are_dropped_not_zeroed() -> None:
    """A fabricated zero is indistinguishable from a real one."""

    payload = {
        "resultType": "matrix",
        "result": [
            {
                "metric": {},
                "values": [[1, "1.0"], [2, "NaN"], [3, "+Inf"], [4, "oops"], [5, "2.0"]],
            }
        ],
    }
    assert parse_matrix(payload)[0].points == [(1, 1.0), (5, 2.0)]


def test_malformed_payloads_parse_to_nothing_rather_than_raising() -> None:
    for payload in (None, {}, {"result": "nope"}, {"result": [None, 5]}):
        assert parse_matrix(payload) == [] or all(
            not s.points for s in parse_matrix(payload)
        )


# --------------------------------------------------------------------------
# curve assembly and the series ceiling
# --------------------------------------------------------------------------


def _curve(payload) -> Curve:
    return curve_from_matrix(
        curve_id_value="cv_x",
        kind="PRIMARY",
        title="t",
        query="up",
        expr_origin=ExprOrigin.RULES_API,
        tier=Tier.METRIC,
        metric_type=MetricType.GAUGE,
        threshold=None,
        window=WINDOW,
        payload=payload,
    )


def test_curve_carries_its_window_and_step() -> None:
    curve = _curve(_matrix(_one({}, [(1, 1.0)])))
    assert curve.window_start == WINDOW.start
    assert curve.step_seconds == WINDOW.step_seconds
    assert not curve.is_empty


def test_a_curve_with_no_points_reports_itself_empty() -> None:
    assert _curve(_matrix(_one({}, []))).is_empty


def test_too_many_series_is_refused_rather_than_trimmed() -> None:
    payload = _matrix(
        *[_one({"i": str(i)}, [(1, 1.0)]) for i in range(MAX_SERIES_PER_QUERY + 1)]
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        _curve(payload)
    assert excinfo.value.reason is BudgetReason.TOO_MANY_SERIES


def test_exactly_at_the_series_ceiling_is_allowed() -> None:
    payload = _matrix(
        *[_one({"i": str(i)}, [(1, 1.0)]) for i in range(MAX_SERIES_PER_QUERY)]
    )
    assert len(_curve(payload).series) == MAX_SERIES_PER_QUERY


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------


def test_bundle_puts_the_primary_curve_first() -> None:
    def make(kind: str, name: str) -> Curve:
        return Curve(
            curve_id=name,
            kind=kind,
            title=name,
            query=name,
            expr_origin=ExprOrigin.TEMPLATE,
            tier=None,
            metric_type=MetricType.GAUGE,
            threshold=None,
            window_start=WINDOW.start,
            window_end=WINDOW.end,
            step_seconds=60,
        )

    bundle = build_bundle(
        alert_starts_at=END,
        curves=[make("AUXILIARY", "a"), make("PRIMARY", "p"), make("AUXILIARY", "b")],
        failures=[],
    )
    assert [c.curve_id for c in bundle.curves] == ["p", "a", "b"]


def test_failure_kinds_are_distinct() -> None:
    """The count is not the invariant (D39) — distinctness and meaning are."""

    values = [kind.value for kind in EvidenceFailureKind]
    assert len(values) == len(set(values))


# --------------------------------------------------------------------------
# rule lookup
# --------------------------------------------------------------------------


def test_rule_expression_is_found_by_alert_name() -> None:
    rules = [
        {"name": "Other", "query": "x"},
        {"name": "ESClusterHealth", "query": ES_EXPR},
    ]
    assert rule_expression_for(rules, "ESClusterHealth") == (ES_EXPR, None)


def test_rule_lookup_misses_return_none() -> None:
    assert rule_expression_for([], "Anything") == (None, None)
    assert rule_expression_for([{"name": "A", "query": "x"}], "B") == (None, None)
    assert rule_expression_for([{"name": "A", "query": ""}], "A") == (None, None)
    assert rule_expression_for([{"name": "A", "query": "x"}], "  ") == (None, None)


# ==========================================================================
# 设计审查返工（D29 / D30 / D32 / D33 / D39）
# ==========================================================================


from app.services.metric_evidence import (  # noqa: E402
    EvidenceNote,
    EvidenceWarningKind,
    classify_empty_result,
)


class TestUniqueMetricOnly:
    """D32：多个不同指标名时取第一个是任意选择，可能画出与告警无关的东西。"""

    def test_two_distinct_metrics_fall_through_to_expression_result(self) -> None:
        plan, failure = plan_primary(
            rule_expr="node_load1 / node_cpu_count", generator_url=None
        )
        assert failure is None
        assert plan.tier is Tier.EXPRESSION_RESULT
        assert plan.metric == ""

    def test_the_same_metric_twice_is_still_unique(self) -> None:
        """The user's real ES alert names one metric in two selectors."""

        plan, _ = plan_primary(rule_expr=ES_EXPR, generator_url=None)
        assert plan.tier is Tier.METRIC
        assert plan.metric == "elasticsearch_cluster_health_status"

    def test_a_single_metric_with_a_function_still_reaches_metric_tier(self) -> None:
        plan, _ = plan_primary(rule_expr="avg_over_time(x[5m])", generator_url=None)
        assert plan.tier is Tier.METRIC
        assert plan.metric == "x"


class TestExpressionResultIsNotBoolean:
    """D29：普通比较过滤 series 而不返回 0/1，'条件成立与否'这个说法是错的。"""

    def test_the_tier_is_named_for_what_it_returns(self) -> None:
        assert Tier.EXPRESSION_RESULT.value == "EXPRESSION_RESULT"
        assert not hasattr(Tier, "CONDITION")

    def test_no_user_facing_text_claims_boolean_semantics(self) -> None:
        plan, _ = plan_primary(rule_expr="vector(1)", generator_url=None)
        title = curve_title(plan, None)
        assert "成立" not in title, "PromQL comparison filters series; it is not a boolean"


class TestThresholdOperatorReachesThePlan:
    """D33：阈值线没有方向读不懂。"""

    def test_plan_carries_the_operator(self) -> None:
        plan, _ = plan_primary(rule_expr="mem_ratio < 0.1", generator_url=None)
        assert plan.tier is Tier.THRESHOLD
        assert plan.threshold == pytest.approx(0.1)
        assert plan.threshold_operator == "<"

    def test_non_threshold_tiers_have_no_operator(self) -> None:
        plan, _ = plan_primary(rule_expr=ES_EXPR, generator_url=None)
        assert plan.threshold_operator is None


class TestEmptyResultsAreClassifiedBySemantics:
    """D30：空结果不能一律报"指标不存在"，四种原因指向四个不同的下一步。"""

    def test_a_metric_absent_from_the_store(self) -> None:
        note = classify_empty_result(
            tier=Tier.METRIC, metric="ghost", known_metrics={"real"}, series_found=0
        )
        assert note is EvidenceFailureKind.METRIC_NOT_FOUND

    def test_a_metric_that_exists_but_not_under_these_labels(self) -> None:
        note = classify_empty_result(
            tier=Tier.METRIC, metric="real", known_metrics={"real"}, series_found=0
        )
        assert note is EvidenceFailureKind.LABEL_SET_NOT_FOUND

    def test_series_exist_but_the_window_holds_no_samples(self) -> None:
        note = classify_empty_result(
            tier=Tier.METRIC, metric="real", known_metrics={"real"}, series_found=3
        )
        assert note is EvidenceFailureKind.NO_SAMPLES_IN_WINDOW

    def test_an_expression_that_never_held_is_its_own_answer(self) -> None:
        """Not a missing metric — it is the useful finding "this never happened"."""

        note = classify_empty_result(
            tier=Tier.EXPRESSION_RESULT, metric="", known_metrics=None, series_found=0
        )
        assert note is EvidenceFailureKind.EXPRESSION_NO_RESULT

    def test_without_a_metric_catalogue_we_do_not_claim_it_is_missing(self) -> None:
        """`METRIC_NOT_FOUND` may only be used when absence can be proven."""

        note = classify_empty_result(
            tier=Tier.METRIC, metric="x", known_metrics=None, series_found=0
        )
        assert note is not EvidenceFailureKind.METRIC_NOT_FOUND


class TestWarningsAreNotFailures:
    """D39：曲线画出来了但依据是推断的，和曲线没产生，是两件事。"""

    def test_bundle_keeps_three_separate_lists(self) -> None:
        curve = Curve(
            curve_id="cv_1", kind="PRIMARY", title="t", query="up",
            expr_origin=ExprOrigin.GENERATOR_URL, tier=Tier.METRIC,
            metric_type=MetricType.UNKNOWN_SUFFIX_GUESS, threshold=None,
            threshold_operator=None, display_unit="", window_mode="RECENT",
            queried_at=END, window_start=WINDOW.start, window_end=WINDOW.end,
            step_seconds=60, series=[],
        )
        bundle = build_bundle(
            alert_starts_at=END,
            curves=[curve],
            warnings=[EvidenceNote(kind=EvidenceWarningKind.METRIC_TYPE_UNKNOWN)],
            failures=[],
        )
        assert len(bundle.curves) == 1
        assert len(bundle.warnings) == 1
        assert bundle.failures == []

    def test_warning_and_failure_kinds_do_not_overlap(self) -> None:
        assert not (
            {kind.value for kind in EvidenceWarningKind}
            & {kind.value for kind in EvidenceFailureKind}
        )

    def test_falling_back_to_the_link_is_a_warning_kind(self) -> None:
        assert EvidenceWarningKind.EXPR_FROM_GENERATOR_URL
        assert EvidenceWarningKind.LABEL_SCHEMA_GUESSED
        assert EvidenceWarningKind.COUNTER_AS_AUTHORED


class TestAmbiguousRulesAreRefused:
    """D31：同名规则多条时任选一条会让曲线取决于返回顺序。"""

    def test_two_rules_with_the_same_name_and_no_discriminator(self) -> None:
        rules = [
            {"name": "A", "query": "x > 1", "labels": {"cluster": "east"}},
            {"name": "A", "query": "y > 2", "labels": {"cluster": "west"}},
        ]
        expr, note = rule_expression_for(rules, "A", alert_labels={})
        assert expr is None
        assert note is EvidenceFailureKind.EXPR_AMBIGUOUS

    def test_alert_labels_disambiguate(self) -> None:
        rules = [
            {"name": "A", "query": "x > 1", "labels": {"cluster": "east"}},
            {"name": "A", "query": "y > 2", "labels": {"cluster": "west"}},
        ]
        expr, note = rule_expression_for(rules, "A", alert_labels={"cluster": "west"})
        assert expr == "y > 2"
        assert note is None

    def test_a_single_match_needs_no_discriminator(self) -> None:
        rules = [{"name": "A", "query": "x > 1"}]
        assert rule_expression_for(rules, "A", alert_labels={}) == ("x > 1", None)


class TestHaFanoutIsNotAmbiguity:
    """真实环境发现（1.8）：Thanos Query 向多个 Prometheus 副本扇出。

    HA 部署下同一条规则会原样返回 N 份。D31 说"多条就拒绝，不任选"，但那是为了防止
    **在不同的规则之间**乱挑；完全相同的重复项里没有可挑的东西，拒绝只会让用户什么都
    看不到，而且是在一个毫无歧义的场景里。
    """

    def test_identical_duplicates_are_one_rule(self) -> None:
        rules = [
            {"name": "ESClusterNotGreen", "query": "es_health == 0"},
            {"name": "ESClusterNotGreen", "query": "es_health == 0"},
            {"name": "ESClusterNotGreen", "query": "es_health == 0"},
        ]
        assert rule_expression_for(rules, "ESClusterNotGreen", alert_labels={}) == (
            "es_health == 0",
            None,
        )

    def test_whitespace_differences_are_still_the_same_rule(self) -> None:
        rules = [
            {"name": "A", "query": "up == 0"},
            {"name": "A", "query": "up  ==  0 "},
        ]
        expr, note = rule_expression_for(rules, "A", alert_labels={})
        assert note is None and expr is not None

    def test_duplicates_that_differ_in_labels_only_are_still_one_rule(self) -> None:
        """Replica labels differ per Prometheus; the rule is the same rule."""

        rules = [
            {"name": "A", "query": "up == 0", "labels": {"replica": "0"}},
            {"name": "A", "query": "up == 0", "labels": {"replica": "1"}},
        ]
        assert rule_expression_for(rules, "A", alert_labels={})[1] is None

    def test_genuinely_different_expressions_are_still_ambiguous(self) -> None:
        """The guard D31 was written for must survive this fix."""

        rules = [
            {"name": "A", "query": "x > 1"},
            {"name": "A", "query": "y > 2"},
        ]
        assert rule_expression_for(rules, "A", alert_labels={})[1] is (
            EvidenceFailureKind.EXPR_AMBIGUOUS
        )

    def test_labels_still_disambiguate_genuinely_different_rules(self) -> None:
        rules = [
            {"name": "A", "query": "x > 1", "labels": {"cluster": "east"}},
            {"name": "A", "query": "y > 2", "labels": {"cluster": "west"}},
        ]
        assert rule_expression_for(rules, "A", alert_labels={"cluster": "west"}) == (
            "y > 2",
            None,
        )

    def test_per_cluster_rules_plus_ha_fanout(self) -> None:
        """Both at once — and the path the early return does *not* cover.

        Three definitions: one for east, and the west one returned twice because
        Thanos fanned out across an HA pair. The full set has two distinct
        expressions, so the early "only one expression" return is skipped; label
        filtering then leaves two rows that are the same rule. Counting *rows*
        there would call it ambiguous and show nothing.

        (Found by reverse verification: loosening the second check broke no test,
        which meant no test reached it.)
        """

        rules = [
            {"name": "A", "query": "east_expr > 1", "labels": {"cluster": "east"}},
            {"name": "A", "query": "west_expr > 1", "labels": {"cluster": "west"}},
            {"name": "A", "query": "west_expr > 1", "labels": {"cluster": "west"}},
        ]
        assert rule_expression_for(rules, "A", alert_labels={"cluster": "west"}) == (
            "west_expr > 1",
            None,
        )

    def test_rule_labels_matching_nothing_must_not_eliminate_every_candidate(self) -> None:
        """Real data (1.8): the alert's `cluster` matched neither rule's.

        The user's two `ElasticsearchClusterNotGreen` rules carry
        `cluster=devops-nonprod` and `cluster=uat-retail`, while the alert carries
        `cluster=archive-elasticsearch`. Compatibility filtering therefore drops
        *both* — and a filter that can empty the set must fall back to the full
        one, or a perfectly answerable lookup returns nothing.
        """

        rules = [
            {"name": "A", "query": "x > 1", "labels": {"cluster": "devops-nonprod"}},
            {"name": "A", "query": "y > 2", "labels": {"cluster": "uat-retail"}},
        ]
        expr, note = rule_expression_for(
            rules, "A", alert_labels={"cluster": "archive-elasticsearch"}
        )
        # Genuinely different expressions, so this stays ambiguous — but it must
        # be *ambiguous*, not "no rule found": the difference matters, because
        # only one of them tells the reader a rule exists at all.
        assert note is EvidenceFailureKind.EXPR_AMBIGUOUS
        assert expr is None

    def test_the_users_real_pair_resolves_to_its_one_expression(self) -> None:
        """The exact shape from their cluster, verbatim."""

        shared = (
            '(elasticsearch_cluster_health_status{color="green"} == 0) or '
            '(elasticsearch_cluster_health_status{color="yellow"} == 1)'
        )
        rules = [
            {
                "name": "ElasticsearchClusterNotGreen",
                "query": shared,
                "labels": {"cluster": "devops-nonprod", "severity": "critical"},
            },
            {
                "name": "ElasticsearchClusterNotGreen",
                "query": shared,
                "labels": {"cluster": "uat-retail", "severity": "critical"},
            },
        ]
        expr, note = rule_expression_for(
            rules,
            "ElasticsearchClusterNotGreen",
            alert_labels={"cluster": "archive-elasticsearch", "color": "yellow"},
        )
        assert note is None
        assert expr == shared


class TestTheRuleOwnVaryingLabelIsNotPinned:
    """1.8：用户看到的是一条 22 小时平直的 1.00，转折点被 color="yellow" 挡掉了。"""

    ES_EXPR_TWO_COLORS = (
        '(elasticsearch_cluster_health_status{color="green"} == 0) or '
        '(elasticsearch_cluster_health_status{color="yellow"} == 1)'
    )

    def test_the_varying_label_is_left_open(self) -> None:
        plan, _ = plan_primary(rule_expr=self.ES_EXPR_TWO_COLORS, generator_url=None)
        query = finalize_primary(
            plan,
            {"cluster": "uat-retail", "color": "yellow", "pod": "p"},
            MetricType.GAUGE,
            allowed_label_names={"cluster", "color", "pod"},
        )
        # Every colour stays in view, so green -> yellow is visible.
        assert "color=" not in query
        assert 'cluster="uat-retail"' in query

    def test_labels_the_rule_does_not_vary_are_still_pinned(self) -> None:
        """Only the varying one opens up; the curve stays about this object.

        The expression has to reach tier 2 for this to be about rebuilding a
        selector at all — tier 1 forwards the author's own sub-expression
        untouched, so a trailing `== 0` would test something else entirely.
        """

        plan, _ = plan_primary(
            rule_expr='avg_over_time(es_health{cluster="uat-retail",color="green"}[5m])',
            generator_url=None,
        )
        assert plan.tier is Tier.METRIC
        query = finalize_primary(
            plan,
            {"cluster": "uat-retail", "color": "yellow"},
            MetricType.GAUGE,
            allowed_label_names={"cluster", "color"},
        )
        assert 'cluster="uat-retail"' in query
        assert 'color="yellow"' in query, "one value only — nothing to open up"
