"""Parsing a dashboard definition into import candidates. Pure, offline.

One fixture covers every branch of design §5, so the failure modes stay visible
next to each other: a Loki panel, a variable datasource, a mixed panel, an
unsupported panel type, a time-window macro, a row with nested panels, a
multi-target panel, and thresholds that are deliberately ignored.

The re-import diff lives here too, because it is the same parse: a first import
is the special case where every entry is new, not a second mechanism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.grafana_import import (
    CandidateStatus,
    DiffKind,
    diff_candidates,
    parse_dashboard,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def dashboard() -> dict:
    with open(FIXTURES / "grafana_dashboard_sample.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def candidates(dashboard):
    return parse_dashboard(dashboard)


def _by_ref(candidates, panel_id: int, ref_id: str = "A"):
    for candidate in candidates:
        if candidate.panel_id == panel_id and candidate.ref_id == ref_id:
            return candidate
    raise AssertionError(f"no candidate for panel {panel_id} target {ref_id}")


class TestPanelWalk:
    def test_a_row_is_expanded_but_produces_nothing_itself(self, candidates) -> None:
        """Collapsed rows hold their panels inline. Missing that hides whole
        sections of a dashboard, and the reader has no way to tell."""

        assert _by_ref(candidates, 2).panel_title == "QPS"
        assert not [item for item in candidates if item.panel_title == "Overview"]

    def test_a_panel_with_no_targets_produces_nothing(self, candidates) -> None:
        assert not [item for item in candidates if item.panel_id == 11]

    def test_an_empty_expression_is_skipped(self, candidates) -> None:
        """Panel 12 target B has `expr: ""` — a real artifact of editing."""

        assert not [
            item for item in candidates if item.panel_id == 12 and item.ref_id == "B"
        ]

    def test_one_candidate_per_target_not_per_panel(self, candidates) -> None:
        """A/B/C in one panel are usually three different metrics. Collapsing
        them into one template would drop two, and a template is matched by
        labels and toggled on its own — glued together they cannot be either."""

        replication = [item for item in candidates if item.panel_id == 4]
        assert {item.ref_id for item in replication} == {"A", "B", "C"}

    def test_order_follows_the_dashboard_layout(self, candidates) -> None:
        """The author's arrangement is a free priority signal; ordering by name
        would throw it away."""

        orders = [item.order for item in candidates]
        assert orders == sorted(orders)
        assert _by_ref(candidates, 2).order < _by_ref(candidates, 3).order


class TestDatasourceFilterRunsFirst:
    def test_a_loki_panel_is_rejected_at_parse_time(self, candidates) -> None:
        """**Reverse-validated.** §14 R-1, an error the design review caught.

        Loki, SQL and Elasticsearch targets also carry an `expr` or equivalent.
        Without this filter they parse into candidates and only fail at probe
        time, where the failure is reported as a syntax or missing-metric
        problem — a thoroughly misleading answer for a panel that was simply
        never a Prometheus panel.

        This asserts the *reason*, not just the status: passing for the wrong
        reason is the failure mode being guarded against.
        """

        candidate = _by_ref(candidates, 6)

        assert candidate.status is CandidateStatus.UNSUPPORTED
        assert "数据源类型" in candidate.reason
        assert "loki" in candidate.reason
        assert "语法" not in candidate.reason
        assert "指标" not in candidate.reason

    def test_a_supported_panel_type_does_not_rescue_a_wrong_datasource(
        self, candidates
    ) -> None:
        """Panel 6 is a `timeseries`, which is on the supported list. Only the
        datasource rule can reject it, so the ordering is what this proves."""

        source = _by_ref(candidates, 6)
        assert source.status is CandidateStatus.UNSUPPORTED
        assert "panel 类型" not in source.reason

    def test_a_mixed_panel_is_judged_per_target(self, candidates) -> None:
        assert _by_ref(candidates, 8, "A").status is CandidateStatus.READY
        rejected = _by_ref(candidates, 8, "B")
        assert rejected.status is CandidateStatus.UNSUPPORTED
        assert "elasticsearch" in rejected.reason

    def test_a_variable_datasource_cannot_be_resolved_offline(
        self, candidates
    ) -> None:
        """`${DS_PROM}` names a datasource chosen at view time. Which store it
        points at is unknowable here, so guessing would produce a template
        pointed at nothing in particular."""

        candidate = _by_ref(candidates, 9)

        assert candidate.status is CandidateStatus.UNSUPPORTED
        assert "变量" in candidate.reason

    def test_an_absent_datasource_means_the_default_one(self, candidates) -> None:
        """Older dashboards omit the field entirely. Rejecting those would
        reject most of what a long-lived Grafana actually contains."""

        assert _by_ref(candidates, 10).status is CandidateStatus.READY


class TestPanelTypeFilter:
    def test_a_table_panel_is_unsupported_and_says_so(self, candidates) -> None:
        candidate = _by_ref(candidates, 5)

        assert candidate.status is CandidateStatus.UNSUPPORTED
        assert "panel 类型" in candidate.reason
        assert "table" in candidate.reason

    def test_the_chart_types_are_accepted(self, candidates) -> None:
        assert _by_ref(candidates, 2).panel_id == 2  # timeseries
        assert _by_ref(candidates, 3).status is CandidateStatus.NEEDS_DECISION  # stat


class TestMacros:
    def test_rate_interval_becomes_a_concrete_duration(self, candidates) -> None:
        candidate = _by_ref(candidates, 2)

        assert "$__rate_interval" not in candidate.resolved_promql
        assert "[5m]" in candidate.resolved_promql
        assert [item.macro for item in candidate.substitutions] == ["$__rate_interval"]
        assert candidate.substitutions[0].replacement == "5m"

    def test_range_becomes_a_concrete_duration(self, candidates) -> None:
        candidate = _by_ref(candidates, 12)

        assert "[1h]" in candidate.resolved_promql
        assert candidate.status is CandidateStatus.READY

    def test_the_original_text_is_kept_beside_the_resolved_one(
        self, candidates
    ) -> None:
        """The user confirms a substitution by reading both. Keeping only the
        result would ask them to approve a change they cannot see."""

        candidate = _by_ref(candidates, 2)

        assert "$__rate_interval" in candidate.raw_promql
        assert candidate.raw_promql != candidate.resolved_promql

    def test_a_time_window_macro_is_refused_rather_than_substituted(
        self, candidates
    ) -> None:
        """`$__from` means "the start of the panel's current time window". This
        system's window comes from when the alert fired. Substituting a concrete
        timestamp produces a curve that renders perfectly and means something
        else entirely — the worst possible failure, because nothing looks
        wrong."""

        candidate = _by_ref(candidates, 7)

        assert candidate.status is CandidateStatus.UNSUPPORTED
        assert "时间窗" in candidate.reason
        assert "$__from" in candidate.reason

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("rate(x[$__interval])", "rate(x[5m])"),
            ("rate(x[${__rate_interval}])", "rate(x[5m])"),
            ("x offset $__range_s", "x offset 3600"),
            ("x offset $__range_ms", "x offset 3600000"),
            ("x offset $__interval_ms", "x offset 300000"),
            ("sum_over_time(x[$__range])", "sum_over_time(x[1h])"),
        ],
    )
    def test_the_longer_macro_name_wins(self, expression, expected) -> None:
        """`$__interval_ms` must not be rewritten as `5m` followed by a stray
        `_ms`, and `$__range_s` must not become `1h_s`."""

        dashboard = {
            "title": "T",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "P",
                    "targets": [{"refId": "A", "expr": expression}],
                }
            ],
        }

        assert parse_dashboard(dashboard)[0].resolved_promql == expected


class TestVariables:
    def test_a_variable_makes_the_candidate_need_a_decision(
        self, candidates
    ) -> None:
        candidate = _by_ref(candidates, 3)

        assert candidate.status is CandidateStatus.NEEDS_DECISION
        assert {item.name for item in candidate.pending_variables} == {
            "cluster",
            "instance",
        }

    def test_a_label_shaped_name_is_suggested_for_binding(self, candidates) -> None:
        """`{{instance}}` is already how the built-in templates are written, so
        binding is the native form here rather than a new idea."""

        candidate = _by_ref(candidates, 3)
        instance = next(
            item for item in candidate.pending_variables if item.name == "instance"
        )

        assert instance.suggestion == "BIND_LABEL"
        assert instance.sample_value == "10.0.0.5:9104"

    def test_a_multi_value_variable_is_suggested_for_pinning(
        self, candidates
    ) -> None:
        """A multi-value variable expands to a regex alternation upstream. One
        alert carries one value for a label, so binding it would silently mean
        something narrower than the panel showed."""

        candidate = _by_ref(candidates, 3)
        cluster = next(
            item for item in candidate.pending_variables if item.name == "cluster"
        )

        assert cluster.suggestion == "PIN_VALUE"
        assert cluster.multi is True

    def test_the_all_sentinel_is_never_offered_as_a_sample(
        self, candidates
    ) -> None:
        """`$__all` is Grafana's marker, not a value any series carries."""

        candidate = _by_ref(candidates, 3)
        cluster = next(
            item for item in candidate.pending_variables if item.name == "cluster"
        )

        assert cluster.sample_value == "prod-a"

    def test_a_suggestion_is_not_a_decision(self, candidates) -> None:
        """Code suggests; the user chooses bind / pin / skip for each one. A
        candidate with anything undecided is not confirmable — that gate is in
        the frontend draft module, and this only guarantees the status says so."""

        assert _by_ref(candidates, 3).status is CandidateStatus.NEEDS_DECISION

    @pytest.mark.parametrize(
        "expression",
        [
            'x{a="$instance"}',
            'x{a="${instance}"}',
            'x{a=~"${instance:regex}"}',
        ],
    )
    def test_every_variable_syntax_is_recognised(self, expression) -> None:
        dashboard = {
            "title": "T",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "P",
                    "targets": [{"refId": "A", "expr": expression}],
                }
            ],
        }

        candidate = parse_dashboard(dashboard)[0]

        assert [item.name for item in candidate.pending_variables] == ["instance"]


class TestNamingAndDisplay:
    def test_the_name_carries_the_dashboard_it_came_from(self, candidates) -> None:
        """Panel titles repeat heavily across dashboards — "CPU", "QPS",
        "Memory". Without the dashboard prefix the template list stops being
        usable after the second import."""

        assert _by_ref(candidates, 3).suggested_name == "MySQL Overview · Connections"

    def test_a_multi_target_panel_disambiguates_its_candidates(
        self, candidates
    ) -> None:
        names = {item.ref_id: item.suggested_name for item in candidates if item.panel_id == 4}

        assert names["A"] == "MySQL Overview · Replication · seconds behind"
        assert names["B"] == "MySQL Overview · Replication · io running"
        # No legendFormat on C, so the target id disambiguates instead.
        assert names["C"] == "MySQL Overview · Replication · C"

    def test_a_single_target_panel_is_not_cluttered_with_a_suffix(
        self, candidates
    ) -> None:
        assert _by_ref(candidates, 2).suggested_name == "MySQL Overview · QPS"

    def test_the_unit_comes_across(self, candidates) -> None:
        assert _by_ref(candidates, 2).display_unit == "reqps"
        assert _by_ref(candidates, 4, "A").display_unit == ""

    def test_thresholds_are_not_imported_as_a_second_threshold_system(
        self, candidates
    ) -> None:
        """ADR 0015: Grafana colours do not imply a trustworthy direction."""

        assert not hasattr(_by_ref(candidates, 3), "threshold_suggestions")


class TestParseIsPure:
    def test_it_does_not_mutate_the_dashboard_it_was_given(self, dashboard) -> None:
        before = json.dumps(dashboard, sort_keys=True)

        parse_dashboard(dashboard)

        assert json.dumps(dashboard, sort_keys=True) == before

    def test_it_takes_no_session_and_no_client(self) -> None:
        """The deterministic core takes already-fetched data (AGENTS.md). That
        is what lets this whole file run against a JSON file."""

        import inspect

        params = set(inspect.signature(parse_dashboard).parameters)
        assert params == {"dashboard"}


class _Origin:
    """Minimal stand-in for a stored `MetricTemplateOrigin` row."""

    def __init__(
        self,
        panel_id: int,
        ref_id: str,
        imported_promql: str,
        template_id: int = 1,
        panel_title: str = "Panel",
    ):
        self.panel_id = panel_id
        self.ref_id = ref_id
        self.imported_promql = imported_promql
        self.template_id = template_id
        self.panel_title = panel_title


class _Template:
    def __init__(self, template_id: int, promql: str, user_modified: bool = False):
        self.id = template_id
        self.promql = promql
        self.user_modified = user_modified


class TestReimportDiff:
    def test_a_first_import_is_all_new(self, candidates) -> None:
        """The special case, not a second code path: nothing to align against."""

        entries = diff_candidates(candidates, [], [])

        assert {entry.kind for entry in entries} == {DiffKind.NEW}
        assert len(entries) == len(candidates)

    def test_an_unchanged_target_is_not_listed(self, candidates) -> None:
        """Listing 40 unchanged rows would bury the three that matter."""

        target = _by_ref(candidates, 4, "A")
        origins = [_Origin(4, "A", target.resolved_promql)]
        templates = [_Template(1, target.resolved_promql)]

        entries = diff_candidates(candidates, origins, templates)

        assert not [entry for entry in entries if entry.candidate is target]

    def test_an_upstream_edit_shows_both_versions(self, candidates) -> None:
        target = _by_ref(candidates, 4, "A")
        origins = [_Origin(4, "A", "mysql_slave_status_seconds_behind_master offset 5m")]
        templates = [_Template(1, "mysql_slave_status_seconds_behind_master offset 5m")]

        entry = next(
            item
            for item in diff_candidates(candidates, origins, templates)
            if item.candidate is target
        )

        assert entry.kind is DiffKind.UPSTREAM_CHANGED
        assert entry.imported_promql == "mysql_slave_status_seconds_behind_master offset 5m"
        assert entry.current_promql == "mysql_slave_status_seconds_behind_master offset 5m"
        assert entry.candidate.resolved_promql != entry.imported_promql

    def test_both_sides_changed_is_a_conflict_showing_three_texts(
        self, candidates
    ) -> None:
        """The user edited it *and* Grafana moved. Picking either one silently
        discards a deliberate act, so all three are shown and nothing is
        chosen for them."""

        target = _by_ref(candidates, 4, "A")
        origins = [_Origin(4, "A", "as_imported")]
        templates = [_Template(1, "as_i_edited_it", user_modified=True)]

        entry = next(
            item
            for item in diff_candidates(candidates, origins, templates)
            if item.candidate is target
        )

        assert entry.kind is DiffKind.CONFLICT
        assert entry.imported_promql == "as_imported"
        assert entry.current_promql == "as_i_edited_it"
        assert entry.candidate.resolved_promql not in ("as_imported", "as_i_edited_it")

    def test_a_target_grafana_no_longer_has_is_reported_never_deleted(
        self, candidates
    ) -> None:
        """Deleting is the user's action on the template page. An import that
        quietly removed templates would make re-import unsafe to run."""

        origins = [_Origin(999, "A", "vanished_metric")]
        templates = [_Template(1, "vanished_metric")]

        gone = [
            entry
            for entry in diff_candidates(candidates, origins, templates)
            if entry.kind is DiffKind.GONE
        ]

        assert len(gone) == 1
        assert gone[0].candidate is None
        assert gone[0].imported_promql == "vanished_metric"
        assert gone[0].panel_id == 999
        assert gone[0].panel_title == "Panel"
        assert gone[0].ref_id == "A"

    def test_multiple_gone_targets_keep_distinct_identities(self, candidates) -> None:
        origins = [
            _Origin(998, "A", "gone_a", template_id=1, panel_title="Old A"),
            _Origin(999, "B", "gone_b", template_id=2, panel_title="Old B"),
        ]
        templates = [_Template(1, "gone_a"), _Template(2, "gone_b")]

        gone = [
            entry
            for entry in diff_candidates(candidates, origins, templates)
            if entry.kind is DiffKind.GONE
        ]

        assert {(entry.panel_id, entry.ref_id) for entry in gone} == {
            (998, "A"),
            (999, "B"),
        }

    def test_alignment_is_by_panel_and_target(self, candidates) -> None:
        """Not by name: a renamed panel is still the same panel, and titles are
        not unique anyway."""

        target = _by_ref(candidates, 4, "B")
        origins = [_Origin(4, "B", target.resolved_promql)]
        templates = [_Template(1, target.resolved_promql)]

        entries = diff_candidates(candidates, origins, templates)

        assert not [entry for entry in entries if entry.candidate is target]
        assert not [entry for entry in entries if entry.kind is DiffKind.GONE]

    def test_the_comparison_is_a_plain_string_compare(self, candidates) -> None:
        """Review Q-补2: the origin keeps the query text, not a digest, so this
        is a comparison the conflict view can also *display*."""

        target = _by_ref(candidates, 4, "C")
        origins = [_Origin(4, "C", target.resolved_promql + " ")]
        templates = [_Template(1, target.resolved_promql + " ")]

        entry = next(
            item
            for item in diff_candidates(candidates, origins, templates)
            if item.candidate is target
        )

        assert entry.kind is DiffKind.UPSTREAM_CHANGED
