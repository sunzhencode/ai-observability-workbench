"""F27 auxiliary curves: seeding, matching and rendering.

Offline: an in-memory SQLite database and plain dicts, no network anywhere.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.registry_models import EventSource, F20Model, MetricQueryTemplate
from app.services.metric_budget import MAX_QUERIES_PER_ALERT
from app.services.metric_templates import (
    BUILTIN_TEMPLATES,
    MAX_AUXILIARY_CURVES,
    candidate_templates,
    matches,
    render,
    required_labels,
    seed_builtin_templates,
    set_template_scope,
    template_scope_for,
)

NODE_LABELS = {"instance": "10.0.0.1:9100", "cluster": "nonprod"}
POD_LABELS = {"namespace": "prod", "pod": "web-0", "cluster": "nonprod"}


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _all(session) -> list[MetricQueryTemplate]:
    return list(session.exec(select(MetricQueryTemplate)).all())


# --------------------------------------------------------------------------
# the catalogue itself
# --------------------------------------------------------------------------


def test_catalogue_keys_are_unique() -> None:
    keys = [builtin.key for builtin in BUILTIN_TEMPLATES]
    assert len(keys) == len(set(keys))


def test_every_builtin_declares_the_labels_its_query_uses() -> None:
    """A placeholder with no matching required label would never be filled."""

    for builtin in BUILTIN_TEMPLATES:
        placeholders = set(
            part.split("}}")[0].strip()
            for part in builtin.promql.split("{{")[1:]
        )
        assert placeholders <= set(builtin.required_labels), builtin.key


def test_every_builtin_explains_what_it_answers() -> None:
    """The description is what the model reads to pick a template in stage 2."""

    for builtin in BUILTIN_TEMPLATES:
        assert builtin.description.strip(), builtin.key


def test_counter_builtins_write_their_own_rate_or_increase() -> None:
    """The auto-rate rule applies to tier 2 only; templates are written whole.

    `node_cpu_seconds_total` wrapped again is a syntax error, and
    `kube_pod_container_status_restarts_total` under `rate()` becomes a line
    pinned to zero — "it restarted three times just now" disappears entirely.
    """

    by_key = {builtin.key: builtin for builtin in BUILTIN_TEMPLATES}
    assert "rate(" in by_key["node_cpu_busy_ratio"].promql
    assert "increase(" in by_key["pod_container_restarts"].promql
    assert "rate(" not in by_key["pod_container_restarts"].promql


def test_auxiliary_limit_leaves_room_for_the_primary_curve() -> None:
    assert MAX_AUXILIARY_CURVES == MAX_QUERIES_PER_ALERT - 1


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------


def test_seeding_inserts_the_catalogue_switched_off(session) -> None:
    written = seed_builtin_templates(session)

    rows = _all(session)
    assert written == len(BUILTIN_TEMPLATES) == len(rows)
    assert all(row.enabled is False for row in rows), (
        "unverified queries must not draw on their own"
    )
    assert all(row.builtin_key for row in rows)


def test_seeding_twice_writes_nothing_the_second_time(session) -> None:
    seed_builtin_templates(session)
    assert seed_builtin_templates(session) == 0
    assert len(_all(session)) == len(BUILTIN_TEMPLATES)


def test_reseeding_does_not_switch_a_disabled_curve_back_on(session) -> None:
    seed_builtin_templates(session)
    row = session.exec(select(MetricQueryTemplate)).first()
    row.enabled = True
    session.add(row)
    session.flush()

    seed_builtin_templates(session)

    session.refresh(row)
    assert row.enabled is True, "re-seeding must not touch the user's switch"


def test_a_corrected_query_reaches_an_untouched_row(session) -> None:
    seed_builtin_templates(session)
    row = session.exec(
        select(MetricQueryTemplate).where(
            MetricQueryTemplate.builtin_key == "pod_mem_vs_limit"
        )
    ).one()
    row.promql = "stale_query"
    row.user_modified = False
    session.add(row)
    session.flush()

    assert seed_builtin_templates(session) == 1

    session.refresh(row)
    assert row.promql != "stale_query"


def test_a_row_the_user_edited_is_never_overwritten(session) -> None:
    """Same trade-off as the F21 adoption: what the user changed wins."""

    seed_builtin_templates(session)
    row = session.exec(
        select(MetricQueryTemplate).where(
            MetricQueryTemplate.builtin_key == "pod_mem_vs_limit"
        )
    ).one()
    row.promql = "my_own_query{pod=\"{{pod}}\"}"
    row.user_modified = True
    session.add(row)
    session.flush()

    assert seed_builtin_templates(session) == 0

    session.refresh(row)
    assert row.promql == 'my_own_query{pod="{{pod}}"}'


def test_seeding_is_a_noop_without_the_table() -> None:
    """A pre-F27 database must not crash on startup."""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with Session(engine) as bare:
        assert seed_builtin_templates(bare) == 0


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------


def test_a_template_matches_only_when_every_label_is_present(session) -> None:
    template = MetricQueryTemplate(
        name="t", promql="m", required_labels_json=json.dumps(["namespace", "pod"])
    )
    assert matches(template, POD_LABELS)
    assert not matches(template, {"namespace": "prod"})
    assert not matches(template, {})


def test_blank_label_values_do_not_satisfy_a_requirement(session) -> None:
    """An empty value renders `pod=""`, which matches nothing and reads as a bug."""

    template = MetricQueryTemplate(
        name="t", promql="m", required_labels_json=json.dumps(["pod"])
    )
    assert not matches(template, {"pod": "   "})


def test_a_template_with_no_requirements_always_matches(session) -> None:
    template = MetricQueryTemplate(name="t", promql="m", required_labels_json="[]")
    assert matches(template, {})


def test_malformed_required_labels_are_read_as_none(session) -> None:
    template = MetricQueryTemplate(name="t", promql="m", required_labels_json="not json")
    assert required_labels(template) == ()
    assert matches(template, {})


def test_only_enabled_templates_are_candidates(session) -> None:
    seed_builtin_templates(session)
    assert candidate_templates(session, POD_LABELS).selected == []

    for row in _all(session):
        row.enabled = True
        session.add(row)
    session.flush()

    keys = [row.builtin_key for row in candidate_templates(session, POD_LABELS).selected]
    assert keys, "pod labels should match the pod templates"
    assert all(key.startswith("pod_") for key in keys)


def test_node_labels_select_the_node_templates(session) -> None:
    seed_builtin_templates(session)
    for row in _all(session):
        row.enabled = True
        session.add(row)
    session.flush()

    keys = [row.builtin_key for row in candidate_templates(session, NODE_LABELS).selected]
    assert keys and all(key.startswith("node_") for key in keys)


def test_candidates_are_capped_and_deterministically_ordered(session) -> None:
    for index in range(MAX_AUXILIARY_CURVES + 3):
        session.add(
            MetricQueryTemplate(
                name=f"t{index:02d}", promql="m", required_labels_json="[]", enabled=True
            )
        )
    session.flush()

    first = [row.name for row in candidate_templates(session, {}).selected]
    second = [row.name for row in candidate_templates(session, {}).selected]
    assert len(first) == MAX_AUXILIARY_CURVES
    assert first == second


def test_candidates_are_empty_without_the_table() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with Session(engine) as bare:
        assert candidate_templates(bare, POD_LABELS).selected == []


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_rendering_fills_every_placeholder(session) -> None:
    template = MetricQueryTemplate(
        name="t",
        promql='m{namespace="{{namespace}}",pod="{{pod}}"}',
        required_labels_json=json.dumps(["namespace", "pod"]),
    )
    assert render(template, POD_LABELS) == 'm{namespace="prod",pod="web-0"}'


def test_rendering_tolerates_whitespace_in_the_placeholder(session) -> None:
    template = MetricQueryTemplate(name="t", promql='m{pod="{{ pod }}"}')
    assert render(template, POD_LABELS) == 'm{pod="web-0"}'


def test_rendering_repeats_a_placeholder_used_twice(session) -> None:
    template = MetricQueryTemplate(
        name="t", promql='a{i="{{instance}}"} / b{i="{{instance}}"}'
    )
    rendered = render(template, NODE_LABELS)
    assert rendered.count("10.0.0.1:9100") == 2


def test_rendering_returns_none_when_a_value_is_missing(session) -> None:
    """Better no curve than a query still carrying a literal `{{pod}}`."""

    template = MetricQueryTemplate(name="t", promql='m{pod="{{pod}}"}')
    assert render(template, {"namespace": "prod"}) is None


def test_rendered_values_cannot_break_out_of_the_matcher(session) -> None:
    """Label values arrive from the monitored system and are hostile by default."""

    template = MetricQueryTemplate(name="t", promql='m{pod="{{pod}}"}')
    rendered = render(template, {"pod": 'x"} or up{'})
    assert rendered == 'm{pod="x\\"} or up{"}'
    assert '"} or up{' not in rendered.replace('\\"', "")


def test_an_empty_template_renders_to_none(session) -> None:
    assert render(MetricQueryTemplate(name="t", promql="   "), {}) is None


# ==========================================================================
# 设计审查返工（D40 / D47）：优先级与遗漏可见
# ==========================================================================


from app.registry_models import MetricTemplateExtras  # noqa: E402
from app.services.metric_templates import (  # noqa: E402
    DEFAULT_TEMPLATE_PRIORITY,
    extras_for,
    set_template_extras,
)


class TestPriorityReplacesNameOrder:
    """D40：按名称排序再静默取前 5，是这个仓库不允许的静默行为。"""

    def _many(self, session, count: int) -> None:
        for index in range(count):
            session.add(
                MetricQueryTemplate(
                    name=f"t{index:02d}", promql="m", required_labels_json="[]",
                    enabled=True,
                )
            )
        session.flush()

    def test_priority_wins_over_the_name(self, session) -> None:
        self._many(session, 3)
        rows = _all(session)
        # Name order would be t00, t01, t02; priority says otherwise.
        set_template_extras(session, rows[2].id, priority=1)
        set_template_extras(session, rows[0].id, priority=9)

        # t02 -> 1, t00 -> 9, t01 -> the default 100. Name order would have been
        # t00, t01, t02, so every position here contradicts it.
        result = candidate_templates(session, {})
        assert [row.name for row in result.selected] == ["t02", "t00", "t01"]

    def test_ties_break_on_id_so_the_order_is_deterministic(self, session) -> None:
        self._many(session, 3)
        first = [row.id for row in candidate_templates(session, {}).selected]
        second = [row.id for row in candidate_templates(session, {}).selected]
        assert first == second == sorted(first)

    def test_templates_without_extras_take_the_default_priority(self, session) -> None:
        self._many(session, 1)
        row = _all(session)[0]
        assert extras_for(session, row.id).priority == DEFAULT_TEMPLATE_PRIORITY

    def test_overflow_is_reported_not_silently_dropped(self, session) -> None:
        """The count of what did not fit must reach the reader."""

        self._many(session, MAX_AUXILIARY_CURVES + 3)
        result = candidate_templates(session, {})
        assert len(result.selected) == MAX_AUXILIARY_CURVES
        assert result.omitted == 3
        assert result.matched == MAX_AUXILIARY_CURVES + 3

    def test_nothing_omitted_when_everything_fits(self, session) -> None:
        self._many(session, 2)
        result = candidate_templates(session, {})
        assert result.omitted == 0

    def test_extras_are_loaded_once_before_sorting(self, session) -> None:
        """Forty imported templates must not turn sorting into forty SELECTs."""

        self._many(session, 8)
        rows = _all(session)
        for index, row in enumerate(rows):
            set_template_extras(session, row.id, priority=100 - index)
        statements: list[str] = []

        def before_cursor_execute(_conn, _cursor, statement, _params, _ctx, _many):
            if (
                statement.lstrip().lower().startswith("select")
                and "metrictemplateextras" in statement.lower()
            ):
                statements.append(statement)

        event.listen(session.bind, "before_cursor_execute", before_cursor_execute)
        try:
            result = candidate_templates(session, {})
        finally:
            event.remove(session.bind, "before_cursor_execute", before_cursor_execute)

        assert [row.id for row in result.selected] == [row.id for row in reversed(rows[-5:])]
        assert len(statements) == 1


class TestTemplateSourceScope:
    def _template(self, session, name: str) -> MetricQueryTemplate:
        row = MetricQueryTemplate(name=name, promql="up", enabled=True)
        session.add(row)
        session.flush()
        return row

    def test_no_scope_row_means_all_sources(self, session) -> None:
        row = self._template(session, "all")
        assert template_scope_for(session, row.id) == {
            "mode": "ALL",
            "source_ids": [],
        }

    def test_candidate_selection_filters_selected_sources_after_label_matching(
        self, session
    ) -> None:
        session.add(EventSource(id="src_a", name="A"))
        session.add(EventSource(id="src_b", name="B"))
        all_sources = self._template(session, "all")
        only_a = self._template(session, "a")
        only_b = self._template(session, "b")
        set_template_scope(
            session, only_a.id, {"mode": "SELECTED", "source_ids": ["src_a"]}
        )
        set_template_scope(
            session, only_b.id, {"mode": "SELECTED", "source_ids": ["src_b"]}
        )

        selected = candidate_templates(session, {}, source_id="src_a").selected

        assert [row.name for row in selected] == [all_sources.name, only_a.name]

    def test_selected_scope_requires_a_known_source(self, session) -> None:
        session.add(EventSource(id="known", name="Known"))
        session.flush()
        row = self._template(session, "t")
        with pytest.raises(ValueError, match="unknown EventSource"):
            set_template_scope(
                session,
                row.id,
                {"mode": "SELECTED", "source_ids": ["missing"]},
            )


class TestExtrasLiveInTheirOwnTable:
    """D47：v11 冻结了 MetricQueryTemplate，加字段会让所有已有库拒绝启动。"""

    def test_the_frozen_model_gained_no_fields(self) -> None:
        columns = set(MetricQueryTemplate.__table__.columns.keys())
        assert "priority" not in columns
        assert "display_unit" not in columns

    def test_extras_are_upserted_not_duplicated(self, session) -> None:
        session.add(MetricQueryTemplate(name="t", promql="m"))
        session.flush()
        row = _all(session)[0]

        set_template_extras(session, row.id, priority=5)
        set_template_extras(session, row.id, display_unit="bytes")

        stored = session.exec(select(MetricTemplateExtras)).all()
        assert len(stored) == 1
        assert stored[0].priority == 5
        assert stored[0].display_unit == "bytes"

    def test_a_missing_extras_row_is_not_an_error(self, session) -> None:
        session.add(MetricQueryTemplate(name="t", promql="m"))
        session.flush()
        extras = extras_for(session, _all(session)[0].id)
        assert extras.priority == DEFAULT_TEMPLATE_PRIORITY
        assert extras.display_unit == ""
