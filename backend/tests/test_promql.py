"""F27 tier detection: pure functions, no network, no database.

The load-bearing case is ``ES_GENERATOR_URL`` — a real alert from the user's
nonprod cluster (2026-07-30). It is kept verbatim because it falls on the
*degraded* path: the first real sample the design met refused tier 1, which is
why "no tier may produce an empty chart" is a requirement and not a nicety
(ADR 0009).
"""

from __future__ import annotations

import pytest

from app.services.promql import (
    IDENTITY_LABELS,
    build_metric_query,
    escape_label_value,
    expr_from_generator_url,
    extract_metric_names,
    strip_comparison,
)

ES_GENERATOR_URL = (
    "http://prometheus-base-prometheus-0:9090/graph"
    "?g0.expr=%28elasticsearch_cluster_health_status%7Bcolor%3D%22green%22%7D"
    "+%3D%3D+0%29+or+%28elasticsearch_cluster_health_status"
    "%7Bcolor%3D%22yellow%22%7D+%3D%3D+1%29&g0.tab=1"
)
ES_EXPR = (
    '(elasticsearch_cluster_health_status{color="green"} == 0) '
    'or (elasticsearch_cluster_health_status{color="yellow"} == 1)'
)


# --------------------------------------------------------------------------
# The real sample, end to end through all three tiers
# --------------------------------------------------------------------------


def test_es_alert_url_yields_its_expression() -> None:
    assert expr_from_generator_url(ES_GENERATOR_URL) == ES_EXPR


def test_es_alert_refuses_tier_one() -> None:
    """Top-level `or` means there is no trailing threshold to strip."""

    assert strip_comparison(ES_EXPR) is None


def test_es_alert_lands_on_tier_two_with_one_metric() -> None:
    """`color` is a label, `green`/`yellow` are string literals, `or` a keyword."""

    assert extract_metric_names(ES_EXPR) == ["elasticsearch_cluster_health_status"]


def test_es_alert_tier_two_query_is_the_metric_itself() -> None:
    metric = extract_metric_names(ES_EXPR)[0]
    query = build_metric_query(
        metric,
        {"cluster": "nonprod", "alertname": "ESClusterHealth", "severity": "warning"},
        is_counter=False,
    )
    assert query == 'elasticsearch_cluster_health_status{cluster="nonprod"}'


# --------------------------------------------------------------------------
# expr_from_generator_url
# --------------------------------------------------------------------------


def test_only_g0_is_read() -> None:
    url = "http://p:9090/graph?g0.expr=up&g1.expr=down&g0.tab=1"
    assert expr_from_generator_url(url) == "up"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "not a url at all",
        "http://p:9090/graph",  # no query
        "http://p:9090/graph?g0.tab=1",  # no expr
        "http://p:9090/graph?g0.expr=",  # blank expr
        "http://p:9090/graph?g0.expr=%20%20",  # whitespace only
        "http://p:9090/graph?g1.expr=up",  # only a non-g0 pane
    ],
)
def test_unusable_urls_return_none_without_raising(url: str | None) -> None:
    assert expr_from_generator_url(url) is None


def test_oversized_url_is_refused() -> None:
    assert expr_from_generator_url("http://p/graph?g0.expr=" + "a" * 20000) is None


def test_a_long_but_bounded_url_still_decodes() -> None:
    """Percent-encoding must not be mistaken for oversize."""

    url = "http://p/graph?g0.expr=" + "%61" * 2000  # 6000 chars of URL, 2000 of expr
    assert len(url) <= 8 * 1024
    assert expr_from_generator_url(url) == "a" * 2000


def test_url_bound_already_subsumes_the_expression_bound_on_this_path() -> None:
    """A URL under the bound cannot hold an over-bound expression.

    Percent-encoding only ever makes the URL longer than the expression it
    carries, so ``MAX_EXPR_LENGTH`` is unreachable through
    ``expr_from_generator_url``. It is not dead code: ``strip_comparison`` and
    ``extract_metric_names`` also accept expressions straight from the rules API,
    which never passes through a URL — those two paths are what the expression
    bound actually guards (see the two oversized tests below).
    """

    longest_expr_a_bounded_url_can_carry = "a" * (8 * 1024 - len("http://p/graph?g0.expr="))
    url = "http://p/graph?g0.expr=" + longest_expr_a_bounded_url_can_carry
    assert len(url) == 8 * 1024
    assert expr_from_generator_url(url) == longest_expr_a_bounded_url_can_carry
    assert len(longest_expr_a_bounded_url_can_carry) < 8 * 1024


# --------------------------------------------------------------------------
# strip_comparison — tier 1
# --------------------------------------------------------------------------


def test_tier_one_strips_a_trailing_threshold() -> None:
    expr = "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1"
    result = strip_comparison(expr)
    assert result is not None
    assert result.expression == (
        "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes"
    )
    assert result.operator == "<"
    assert result.threshold == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("expr", "op", "value"),
    [
        ("up == 0", "==", 0.0),
        ("up != 1", "!=", 1.0),
        ("x >= 90", ">=", 90.0),
        ("x <= 5e-3", "<=", 5e-3),
        ("x > .5", ">", 0.5),
        ("x < -2", "<", -2.0),
        ("x > +1.5e2", ">", 150.0),
    ],
)
def test_tier_one_number_forms(expr: str, op: str, value: float) -> None:
    result = strip_comparison(expr)
    assert result is not None, f"{op} should have been stripped"
    assert result.operator == op
    assert result.threshold == pytest.approx(value)


@pytest.mark.parametrize(
    "expr",
    [
        ES_EXPR,
        "up == 0 or up == 1",
        "a > 1 and b > 2",
        "a > 1 unless b",
        "UP == 0 OR up == 1",  # case-insensitive keywords
    ],
)
def test_top_level_set_operators_are_never_stripped(expr: str) -> None:
    assert strip_comparison(expr) is None


@pytest.mark.parametrize(
    "expr",
    [
        "a and b > 1",
        "up unless on() foo > 1",
        "a or b >= 0.5",
    ],
)
def test_a_threshold_after_a_set_operator_is_not_the_whole_expression(expr: str) -> None:
    """Comparison binds tighter than `and` / `or` / `unless` in PromQL.

    `a and b > 1` parses as `a and (b > 1)`, so the trailing `> 1` does **not**
    apply to the left side. Stripping it would chart `a and b` and label `1` as
    its threshold — a silently wrong picture.

    These cases have exactly one top-level comparison and a numeric right side,
    so the arity check cannot catch them: the set-operator guard is the only
    thing standing here. (Found by deliberately disabling that guard and
    noticing that nothing went red.)
    """

    assert strip_comparison(expr) is None


def test_comparison_against_a_vector_is_not_stripped() -> None:
    assert strip_comparison("node_load1 > node_cpu_count") is None


def test_bool_modifier_is_not_stripped() -> None:
    """`> bool 5` yields a 0/1 series, so the comparison is not a threshold."""

    assert strip_comparison("x > bool 5") is None


def test_offset_and_at_modifiers_are_not_stripped() -> None:
    assert strip_comparison("x offset 5m > 1") is None
    assert strip_comparison("x @ 1609746000 > 1") is None


def test_subquery_is_not_stripped() -> None:
    assert strip_comparison("max_over_time(rate(x[1m])[1h:5m]) > 3") is None


def test_range_selector_alone_is_still_strippable() -> None:
    """`[5m]` is a range, not a subquery — it must not block tier 1."""

    result = strip_comparison("rate(http_errors_total[5m]) > 5")
    assert result is not None
    assert result.expression == "rate(http_errors_total[5m])"
    assert result.operator == ">"
    assert result.threshold == pytest.approx(5.0)


def test_multiple_top_level_comparisons_are_ambiguous() -> None:
    assert strip_comparison("0 < x < 5") is None


def test_operators_inside_braces_and_strings_do_not_count() -> None:
    """A matcher's `=~` and a label value containing `or` must be invisible."""

    expr = 'sum(http_requests_total{path=~"/a|/b",note="or > 1"}) > 10'
    result = strip_comparison(expr)
    assert result is not None
    assert result.threshold == pytest.approx(10.0)


@pytest.mark.parametrize("expr", [None, "", "   ", "up", "sum(up)"])
def test_expressions_without_a_comparison_return_none(expr: str | None) -> None:
    assert strip_comparison(expr) is None


def test_oversized_expression_is_refused_by_tier_one() -> None:
    assert strip_comparison("a" * 9000 + " > 1") is None


# --------------------------------------------------------------------------
# extract_metric_names — tier 2
# --------------------------------------------------------------------------


def test_function_names_are_not_metrics() -> None:
    assert extract_metric_names("rate(x_total[5m]) > 5") == ["x_total"]


def test_nested_functions_are_not_metrics() -> None:
    expr = 'histogram_quantile(0.99, sum by (le) (rate(request_duration_seconds_bucket[5m])))'
    assert extract_metric_names(expr) == ["request_duration_seconds_bucket"]


def test_aggregation_operator_before_by_is_not_a_metric() -> None:
    """`sum by (ns) (...)` puts a keyword between `sum` and `(`."""

    assert extract_metric_names("sum by (namespace) (rate(foo_total[5m]))") == [
        "foo_total"
    ]


def test_label_names_in_by_and_on_groups_are_not_metrics() -> None:
    expr = (
        "sum(container_memory_working_set_bytes) by (namespace, pod) "
        "/ on(namespace, pod) group_left(container) "
        "sum(kube_pod_container_resource_limits) by (namespace, pod)"
    )
    assert extract_metric_names(expr) == [
        "container_memory_working_set_bytes",
        "kube_pod_container_resource_limits",
    ]


def test_matcher_label_names_and_values_are_not_metrics() -> None:
    expr = 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay",mountpoint="/"}'
    assert extract_metric_names(expr) == ["node_filesystem_avail_bytes"]


def test_range_durations_are_not_metrics() -> None:
    """`m` in `[5m]` starts an identifier but lives inside brackets."""

    assert extract_metric_names("increase(restarts_total[10m])") == ["restarts_total"]


def test_recording_rule_names_with_colons_are_metrics() -> None:
    assert extract_metric_names("job:http_errors:rate5m > 1") == [
        "job:http_errors:rate5m"
    ]


def test_metrics_are_deduplicated_in_first_appearance_order() -> None:
    expr = "b_metric / a_metric + b_metric"
    assert extract_metric_names(expr) == ["b_metric", "a_metric"]


def test_comments_are_ignored() -> None:
    assert extract_metric_names("up # not_a_metric\n") == ["up"]


@pytest.mark.parametrize("expr", [None, "", "   ", "1 > 0", "sum by (a) (1)"])
def test_expressions_with_no_metric_yield_tier_three(expr: str | None) -> None:
    """An empty list is the signal to fall through to tier 3."""

    assert extract_metric_names(expr) == []


def test_oversized_expression_is_refused_by_tier_two() -> None:
    assert extract_metric_names("a" * 9000) == []


# --------------------------------------------------------------------------
# build_metric_query
# --------------------------------------------------------------------------


def test_counter_is_wrapped_in_rate() -> None:
    query = build_metric_query("x_total", {"pod": "p"}, is_counter=True)
    assert query == 'rate(x_total{pod="p"}[5m])'


def test_gauge_is_not_wrapped() -> None:
    assert build_metric_query("x", {"pod": "p"}, is_counter=False) == 'x{pod="p"}'


def test_only_identity_labels_are_used() -> None:
    """An allowlist: an unknown label left in would silently select nothing."""

    labels = {
        "namespace": "prod",
        "pod": "web-0",
        "alertname": "HighMemory",
        "severity": "critical",
        "prometheus": "monitoring/k8s",
        "some_team_label": "whatever",
    }
    query = build_metric_query("m", labels, is_counter=False)
    assert query == 'm{namespace="prod",pod="web-0"}'
    for excluded in ("alertname", "severity", "prometheus", "some_team_label"):
        assert excluded not in query


def test_identity_label_order_is_stable() -> None:
    """Same labels in a different dict order produce the same query string."""

    a = build_metric_query("m", {"pod": "p", "cluster": "c"}, is_counter=False)
    b = build_metric_query("m", {"cluster": "c", "pod": "p"}, is_counter=False)
    assert a == b == 'm{cluster="c",pod="p"}'


def test_no_labels_yields_a_bare_selector() -> None:
    assert build_metric_query("m", {}, is_counter=False) == "m"
    assert build_metric_query("m", None, is_counter=False) == "m"


def test_blank_label_values_are_dropped() -> None:
    assert build_metric_query("m", {"pod": "  "}, is_counter=False) == "m"


def test_label_values_cannot_break_out_of_the_matcher() -> None:
    """Label values are external text; they must not close the matcher."""

    hostile = 'x"} or up{'
    query = build_metric_query("m", {"pod": hostile}, is_counter=False)
    assert query == 'm{pod="x\\"} or up{"}'
    assert escape_label_value(hostile) == 'x\\"} or up{'


def test_newlines_in_label_values_are_escaped() -> None:
    assert escape_label_value("a\nb\tc\rd") == "a\\nb\\tc\\rd"


def test_backslashes_are_escaped_before_quotes() -> None:
    """Order matters: escaping quotes first would double-escape the backslash."""

    assert escape_label_value('a\\"b') == 'a\\\\\\"b'


def test_identity_labels_has_no_duplicates() -> None:
    assert len(IDENTITY_LABELS) == len(set(IDENTITY_LABELS))


# ==========================================================================
# 设计审查返工（D33 / D37）：以下用例先于实现补入，实现前应当全部失败。
# ==========================================================================


from app.services.promql import (  # noqa: E402
    QueryScopeUnsafe,
    assert_query_scope_safe,
    labels_with_multiple_values,
    parse_promql_duration,
)


class TestThresholdKeepsItsDirection:
    """D33：只返回 (表达式, 阈值) 不够——阈值线没有方向读不懂。

    而且"何时进入告警区间 / 首次穿越 / 持续多久"这些确定性事实全靠 operator 算，
    没有它，阶段 2 喂给模型的统计里就少了最能说明问题的一项。
    """

    def test_split_carries_the_operator(self) -> None:
        split = strip_comparison("node_mem_ratio < 0.1")
        assert split is not None
        assert split.expression == "node_mem_ratio"
        assert split.operator == "<"
        assert split.threshold == pytest.approx(0.1)

    @pytest.mark.parametrize(
        ("expr", "operator"),
        [
            ("up == 0", "=="),
            ("up != 1", "!="),
            ("x >= 90", ">="),
            ("x <= 5", "<="),
            ("x > 1", ">"),
            ("x < 1", "<"),
        ],
    )
    def test_every_operator_survives(self, expr: str, operator: str) -> None:
        split = strip_comparison(expr)
        assert split is not None and split.operator == operator

    def test_a_reversed_comparison_is_normalised(self) -> None:
        """`0.1 > metric` means the same as `metric < 0.1`; keep the meaning."""

        split = strip_comparison("0.1 > node_mem_ratio")
        assert split is not None
        assert split.expression == "node_mem_ratio"
        assert split.operator == "<"
        assert split.threshold == pytest.approx(0.1)

    def test_reversed_normalisation_flips_each_operator(self) -> None:
        assert strip_comparison("5 <= x").operator == ">="
        assert strip_comparison("5 >= x").operator == "<="
        assert strip_comparison("5 == x").operator == "=="
        assert strip_comparison("5 != x").operator == "!="

    def test_numbers_on_both_sides_are_not_a_threshold(self) -> None:
        """Nothing to chart, and choosing a side would be arbitrary."""

        assert strip_comparison("1 > 0") is None


class TestQueryScopeIsBounded:
    """D37：外层 24 小时窗口管不住表达式内部的时间语义。

    `rate(x_total[30d]) > 5` 剥完得到 `rate(x_total[30d])` 原样发出去，
    上游照样扫 30 天。这是审查抓到的真窟窿。
    """

    @pytest.mark.parametrize(
        "text, seconds",
        [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("1w", 604800),
         ("1h30m", 5400), ("100ms", 0), ("1y", 365 * 86400)],
    )
    def test_duration_parsing(self, text: str, seconds: int) -> None:
        assert parse_promql_duration(text) == seconds

    def test_a_short_range_is_allowed(self) -> None:
        assert_query_scope_safe("rate(x_total[5m])")
        assert_query_scope_safe("sum(rate(a[1h]) + rate(b[6h]))")

    def test_a_long_range_is_refused(self) -> None:
        with pytest.raises(QueryScopeUnsafe):
            assert_query_scope_safe("rate(x_total[30d])")

    def test_exactly_at_the_ceiling_is_allowed(self) -> None:
        assert_query_scope_safe("rate(x[6h])")

    def test_offset_is_refused(self) -> None:
        with pytest.raises(QueryScopeUnsafe):
            assert_query_scope_safe("x offset 1y")

    def test_at_modifier_is_refused(self) -> None:
        with pytest.raises(QueryScopeUnsafe):
            assert_query_scope_safe("x @ 1700000000")

    def test_subquery_is_refused(self) -> None:
        with pytest.raises(QueryScopeUnsafe):
            assert_query_scope_safe("sum_over_time(x[7d:1s])")

    def test_a_plain_expression_is_allowed(self) -> None:
        assert_query_scope_safe("up")
        assert_query_scope_safe("a / b")

    def test_durations_inside_strings_do_not_count(self) -> None:
        """A label value that happens to look like a range is not a range."""

        assert_query_scope_safe('x{note="[30d]"}')

    def test_the_refusal_names_what_was_wrong(self) -> None:
        with pytest.raises(QueryScopeUnsafe) as excinfo:
            assert_query_scope_safe("rate(x[30d])")
        assert "30d" in str(excinfo.value) or "range" in str(excinfo.value).lower()


class TestLabelSourceIsInjectable:
    """D35：固定 allowlist 会丢掉 exporter 自定义标签（用户 ES 告警的 `color`）。

    真实 label schema 是主路径，allowlist 只是端点不可用时的兜底。
    """

    def test_an_explicit_label_set_overrides_the_allowlist(self) -> None:
        labels = {"cluster": "c", "color": "green", "alertname": "ES"}
        query = build_metric_query(
            "elasticsearch_cluster_health_status",
            labels,
            is_counter=False,
            allowed_label_names={"cluster", "color"},
        )
        assert query == (
            'elasticsearch_cluster_health_status{cluster="c",color="green"}'
        )

    def test_the_allowlist_is_still_the_fallback(self) -> None:
        labels = {"cluster": "c", "color": "green"}
        query = build_metric_query("m", labels, is_counter=False)
        assert query == 'm{cluster="c"}', "color is not in IDENTITY_LABELS"

    def test_an_explicit_empty_set_yields_a_bare_selector(self) -> None:
        """The metric genuinely has no labels in common with the alert."""

        query = build_metric_query("m", {"pod": "p"}, is_counter=False,
                                   allowed_label_names=set())
        assert query == "m"

    def test_explicit_names_absent_from_the_alert_are_skipped(self) -> None:
        query = build_metric_query("m", {"pod": "p"}, is_counter=False,
                                   allowed_label_names={"pod", "container"})
        assert query == 'm{pod="p"}'


class TestLabelsTheExpressionItselfVaries:
    """1.8 真实发现：把表达式自己在比较的那个标签钉死，会画掉转折点。

    用户那条 ES 告警的表达式同时看 green 和 yellow；告警之所以带 `color=yellow`，
    正是因为"变黄了"。拿它去约束查询等于只看变黄之后的那一段，绿变黄的那一刻反而没了。
    """

    def test_a_label_pinned_to_two_values_is_reported(self) -> None:
        expr = (
            '(elasticsearch_cluster_health_status{color="green"} == 0) or '
            '(elasticsearch_cluster_health_status{color="yellow"} == 1)'
        )
        assert labels_with_multiple_values(expr) == {"color"}

    def test_a_label_pinned_to_one_value_is_not_reported(self) -> None:
        """One value everywhere means the expression is not varying it."""

        expr = 'up{job="a"} == 0 or up{job="a"} == 1'
        assert labels_with_multiple_values(expr) == set()

    def test_labels_in_different_selectors_are_compared_together(self) -> None:
        expr = 'a{env="prod"} / b{env="staging"}'
        assert labels_with_multiple_values(expr) == {"env"}

    def test_only_equality_matchers_count(self) -> None:
        """`!=` and regex matchers do not pin a value, so they say nothing."""

        expr = 'x{fstype!~"tmpfs|overlay",mountpoint="/"}'
        assert labels_with_multiple_values(expr) == set()

    def test_no_selectors_at_all(self) -> None:
        assert labels_with_multiple_values("up") == set()
        assert labels_with_multiple_values("") == set()
        assert labels_with_multiple_values(None) == set()

    def test_the_varying_label_is_excluded_from_the_rebuilt_selector(self) -> None:
        labels = {"cluster": "uat-retail", "color": "yellow", "pod": "p"}
        query = build_metric_query(
            "elasticsearch_cluster_health_status",
            labels,
            is_counter=False,
            allowed_label_names={"cluster", "color", "pod"},
            exclude_label_names={"color"},
        )
        # Every colour stays in view, so the green -> yellow moment is visible.
        assert "color=" not in query
        assert 'cluster="uat-retail"' in query and 'pod="p"' in query
