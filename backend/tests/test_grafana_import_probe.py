"""Checking candidates against the real store, and writing the chosen ones.

Offline: a MockTransport stands in for Thanos, so every branch of the probe is
reachable without a network.

Why the probe exists at all: stage 1.8 of the metric-evidence work tried the
eight built-in templates against a real store and the result overturned the
standing guess about which one would need fixing. **Reading the JSON and
guessing which queries run gets it wrong.** That is also why "unverified" is its
own outcome rather than a shade of "ready" — a candidate nothing checked must
never be presented as working.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.registry_models import (
    EventSource,
    F20Model,
    MetricQueryTemplate,
    MetricTemplateExtras,
    MetricTemplateOrigin,
)
from app.services import metric_templates
from app.services.grafana_import import (
    CandidateStatus,
    diff_candidates,
    ConfirmItem,
    ConfirmOrigin,
    confirm_import,
    parse_dashboard,
    probe_candidates,
)
from app.services.metric_budget import MAX_PROBES_PER_IMPORT, MAX_SERIES_PER_QUERY
from app.sources.thanos import ThanosClient

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "src_test"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        active.add(EventSource(id=SOURCE_ID, name="src"))
        active.flush()
        yield active


@pytest.fixture
def candidates() -> list:
    with open(FIXTURES / "grafana_dashboard_sample.json", encoding="utf-8") as handle:
        return parse_dashboard(json.load(handle))


def _vector(value: str = "0.83") -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"instance": "10.0.0.5:9104"}, "value": [1700000000, value]}
            ],
        },
    }


_EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}


def _thanos(handler) -> ThanosClient:
    return ThanosClient(
        base_url="http://thanos.test",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def _simple_candidates(count: int, expression: str = "up"):
    dashboard = {
        "title": "T",
        "panels": [
            {
                "id": index + 1,
                "type": "timeseries",
                "title": f"P{index}",
                "targets": [{"refId": "A", "expr": expression}],
            }
            for index in range(count)
        ],
    }
    return parse_dashboard(dashboard)


class TestProbeWithoutAHistoryAddress:
    @pytest.mark.asyncio
    async def test_everything_is_unverified_when_there_is_no_thanos(
        self, candidates
    ) -> None:
        """**Reverse-validated.** §14 R-8.

        Grafana and Thanos are both optional per source, so having one without
        the other is an ordinary configuration, and import still works. What it
        must not do is call anything "ready" — nothing checked it. Presenting
        unchecked candidates as usable is exactly what the probe exists to stop,
        so getting this branch wrong removes the feature's whole point while
        leaving it looking finished.
        """

        probed = await probe_candidates(candidates, None)

        checkable = [
            item
            for item in probed
            if item.status is not CandidateStatus.UNSUPPORTED
        ]
        assert checkable
        assert all(item.status is CandidateStatus.UNVERIFIED for item in checkable)
        assert all("历史地址" in item.probe_note for item in checkable)
        assert not [
            item for item in probed if item.status is CandidateStatus.READY
        ]

    @pytest.mark.asyncio
    async def test_an_unconfigured_client_counts_as_no_thanos(
        self, candidates
    ) -> None:
        probed = await probe_candidates(candidates, ThanosClient(base_url=""))

        assert not [
            item for item in probed if item.status is CandidateStatus.READY
        ]

    @pytest.mark.asyncio
    async def test_unsupported_candidates_keep_their_own_reason(
        self, candidates
    ) -> None:
        """A Loki panel does not become "unverified" — it was never a candidate
        for verification, and its real reason is the useful one."""

        probed = await probe_candidates(candidates, None)
        loki = next(item for item in probed if item.panel_id == 6)

        assert loki.status is CandidateStatus.UNSUPPORTED
        assert "数据源类型" in loki.reason


class TestProbeResults:
    @pytest.mark.asyncio
    async def test_a_query_with_data_is_ready_and_carries_the_value(self) -> None:
        """The observed value makes the live validation transparent."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/v1/query" in str(request.url)
            assert "query_range" not in str(request.url)
            return httpx.Response(200, json=_vector("0.83"))

        probed = await probe_candidates(_simple_candidates(1), _thanos(handler))

        assert probed[0].status is CandidateStatus.READY
        assert probed[0].probe_value == pytest.approx(0.83)

    @pytest.mark.asyncio
    async def test_an_instant_query_is_used_rather_than_a_range(self) -> None:
        """"Does this run and return anything" needs one evaluation. A range
        query costs an order of magnitude more for the same answer, and an
        import fires dozens at once."""

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.path))
            return httpx.Response(200, json=_vector())

        await probe_candidates(_simple_candidates(3), _thanos(handler))

        assert seen == ["/api/v1/query"] * 3

    @pytest.mark.asyncio
    async def test_a_query_that_runs_but_returns_nothing_is_still_ready(
        self,
    ) -> None:
        """Two different facts: "this query is broken" and "this query is fine
        and the thing it measures is quiet right now"."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_EMPTY)

        probed = await probe_candidates(_simple_candidates(1), _thanos(handler))

        assert probed[0].status is CandidateStatus.READY
        assert probed[0].probe_value is None
        assert "没有数据" in probed[0].probe_note

    @pytest.mark.asyncio
    async def test_a_rejected_query_becomes_unsupported_with_the_reason(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"status": "error", "error": "parse error"})

        probed = await probe_candidates(_simple_candidates(1), _thanos(handler))

        assert probed[0].status is CandidateStatus.UNSUPPORTED
        assert "HTTP_400" in probed[0].reason

    @pytest.mark.asyncio
    async def test_one_bad_candidate_does_not_take_the_batch_down(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 2:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json=_vector())

        probed = await probe_candidates(_simple_candidates(4), _thanos(handler))

        assert sum(1 for item in probed if item.status is CandidateStatus.READY) == 3
        assert sum(1 for item in probed if item.status is CandidateStatus.UNSUPPORTED) == 1

    @pytest.mark.asyncio
    async def test_a_failure_never_carries_the_address_back_out(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connect to 10.9.9.9:9090 failed", request=request)

        probed = await probe_candidates(_simple_candidates(1), _thanos(handler))

        assert "10.9.9.9" not in probed[0].reason
        assert "thanos.test" not in probed[0].reason


class TestProbeBudget:
    @pytest.mark.asyncio
    async def test_query_scope_is_rejected_before_request(self) -> None:
        """Import probes are not a cheaper side door around the shared query
        scope guard. The rejection has to happen before any network request."""

        sent = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            sent["n"] += 1
            return httpx.Response(200, json=_vector())

        probed = await probe_candidates(
            _simple_candidates(1, expression="rate(up[30d])"), _thanos(handler)
        )

        assert sent["n"] == 0
        assert probed[0].status is CandidateStatus.UNSUPPORTED
        assert "预算" in probed[0].reason or "范围" in probed[0].reason

    @pytest.mark.asyncio
    async def test_series_limit_is_enforced_without_silent_truncation(self) -> None:
        """The probe asks Thanos for one sentinel row beyond the cap and
        rejects the complete candidate instead of presenting a sliced answer."""

        seen_limits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_limits.append(request.url.params.get("limit", ""))
            result = [
                {"metric": {"instance": str(index)}, "value": [1700000000, "1"]}
                for index in range(MAX_SERIES_PER_QUERY + 1)
            ]
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": result},
                },
            )

        probed = await probe_candidates(_simple_candidates(1), _thanos(handler))

        assert seen_limits == [str(MAX_SERIES_PER_QUERY + 1)]
        assert probed[0].status is CandidateStatus.UNSUPPORTED
        assert "序列" in probed[0].reason or "预算" in probed[0].reason

    @pytest.mark.asyncio
    async def test_candidates_past_the_limit_are_unverified_not_rejected(
        self,
    ) -> None:
        """Over budget says nothing about the query. Calling it "unusable" would
        be a false accusation; calling it "ready" would be an unearned promise."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_vector())

        probed = await probe_candidates(
            _simple_candidates(MAX_PROBES_PER_IMPORT + 3), _thanos(handler)
        )

        assert sum(1 for item in probed if item.status is CandidateStatus.READY) == (
            MAX_PROBES_PER_IMPORT
        )
        over = [item for item in probed if item.status is CandidateStatus.UNVERIFIED]
        assert len(over) == 3
        assert "预算" in over[0].probe_note

    @pytest.mark.asyncio
    async def test_the_budget_is_spent_before_the_requests_not_after(self) -> None:
        """The guard runs before anything leaves, like every other budget in
        this repository — trimming results afterwards would make an overrun look
        like a normal answer."""

        sent = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            sent["n"] += 1
            return httpx.Response(200, json=_vector())

        await probe_candidates(
            _simple_candidates(MAX_PROBES_PER_IMPORT + 10), _thanos(handler)
        )

        assert sent["n"] == MAX_PROBES_PER_IMPORT

    @pytest.mark.asyncio
    async def test_a_slow_upstream_is_cut_off_per_candidate(self) -> None:
        """One unresponsive query must not hold the whole preview open."""

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)
            return httpx.Response(200, json=_vector())

        client = ThanosClient(
            base_url="http://thanos.test",
            timeout=30.0,
            transport=httpx.MockTransport(handler),
        )

        probed = await asyncio.wait_for(
            probe_candidates(_simple_candidates(2), client, timeout_seconds=0.05),
            timeout=10,
        )

        assert all(item.status is CandidateStatus.UNSUPPORTED for item in probed)
        assert all("超时" in item.reason for item in probed)


class TestProbingCandidatesThatStillNeedDecisions:
    @pytest.mark.asyncio
    async def test_a_pending_variable_is_smoke_tested_with_its_sample(
        self, candidates
    ) -> None:
        """`{{instance}}` is not valid PromQL, so the dashboard's own current
        value stands in. The note says so — an unqualified "verified" would
        claim more than was done."""

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("query", ""))
            return httpx.Response(200, json=_vector())

        probed = await probe_candidates(candidates, _thanos(handler))
        connections = next(item for item in probed if item.panel_id == 3)

        assert any("10.0.0.5:9104" in query for query in seen)
        assert "样例值" in connections.probe_note

    @pytest.mark.asyncio
    async def test_a_successful_probe_does_not_remove_the_decision(
        self, candidates
    ) -> None:
        """A candidate with undecided variables is not "directly usable" however
        well the smoke test went — the user still has to choose bind or pin for
        each one. Promoting it to READY would let it be ticked with a literal
        `$cluster` still in the query."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_vector())

        probed = await probe_candidates(candidates, _thanos(handler))
        connections = next(item for item in probed if item.panel_id == 3)

        assert connections.status is CandidateStatus.NEEDS_DECISION
        assert connections.probe_value == pytest.approx(0.83)

    @pytest.mark.asyncio
    async def test_without_a_sample_only_the_metric_name_is_checked(self) -> None:
        """And it says exactly that. Claiming the query was verified when only
        its metric name was looked up is the kind of quiet overstatement this
        whole step exists to remove."""

        dashboard = {
            "title": "T",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "P",
                    "targets": [
                        {"refId": "A", "expr": 'mysql_up{cluster="$nowhere"}'}
                    ],
                }
            ],
        }
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("query", ""))
            return httpx.Response(200, json=_vector())

        probed = await probe_candidates(parse_dashboard(dashboard), _thanos(handler))

        assert seen == ["mysql_up"]
        assert "指标名" in probed[0].probe_note
        assert "样例值" not in probed[0].probe_note


class TestConfirmWritesOnlyWhatWasTicked:
    def test_a_ticked_candidate_becomes_a_template_with_its_origin(
        self, session
    ) -> None:
        result = confirm_import(session, SOURCE_ID, [_item()])
        session.flush()

        template = session.exec(select(MetricQueryTemplate)).one()
        origin = session.exec(select(MetricTemplateOrigin)).one()
        assert result.created_template_ids == [template.id]
        assert template.promql == "mysql_up"
        assert template.builtin_key is None
        assert template.user_modified is False
        assert origin.template_id == template.id
        assert origin.dashboard_uid == "mysql-overview"

    def test_nothing_unticked_leaves_a_trace(self, session) -> None:
        confirm_import(session, SOURCE_ID, [])
        session.flush()

        assert session.exec(select(MetricQueryTemplate)).all() == []
        assert session.exec(select(MetricTemplateOrigin)).all() == []

    def test_an_imported_template_arrives_switched_off(self, session) -> None:
        """Same trade-off as the shipped catalogue: an unverified query drawing
        nothing reads as a broken feature, not as a wrong metric name. Import
        multiplies that by the number of panels."""

        confirm_import(session, SOURCE_ID, [_item()])
        session.flush()

        assert session.exec(select(MetricQueryTemplate)).one().enabled is False

    def test_the_origin_keeps_the_query_that_was_imported(self, session) -> None:
        confirm_import(session, SOURCE_ID, [_item(final_promql="mysql_up{a=\"b\"}")])
        session.flush()

        origin = session.exec(select(MetricTemplateOrigin)).one()
        assert origin.imported_promql == 'mysql_up{a="b"}'

    def test_the_unit_and_priority_land_in_the_extras_table(self, session) -> None:
        confirm_import(session, SOURCE_ID, [_item(display_unit="reqps")])
        session.flush()

        extras = session.exec(select(MetricTemplateExtras)).one()
        assert extras.display_unit == "reqps"

    def test_priority_starts_above_everything_already_there(self, session) -> None:
        """Imported templates should not interleave with a curated set the user
        already ordered."""

        existing = MetricQueryTemplate(name="existing", promql="up")
        session.add(existing)
        session.flush()
        metric_templates.set_template_extras(session, int(existing.id), priority=250)

        confirm_import(
            session,
            SOURCE_ID,
            [_item(order=0, ref_id="A"), _item(order=1, ref_id="B", panel_id=3)],
        )
        session.flush()

        priorities = sorted(
            row.priority
            for row in session.exec(select(MetricTemplateExtras)).all()
            if row.template_id != existing.id
        )
        assert priorities == [260, 261]

    def test_the_dashboard_order_becomes_the_priority_order(self, session) -> None:
        confirm_import(
            session,
            SOURCE_ID,
            [
                _item(order=5, ref_id="B", panel_id=3, name="second"),
                _item(order=1, ref_id="A", panel_id=2, name="first"),
            ],
        )
        session.flush()

        rows = {
            row.name: metric_templates.extras_for(session, row.id).priority
            for row in session.exec(select(MetricQueryTemplate)).all()
        }
        assert rows["first"] < rows["second"]

    def test_a_duplicate_name_is_suffixed_against_the_whole_catalogue(
        self, session
    ) -> None:
        """Compared against every template, not just this batch: panel titles
        repeat across dashboards, and the collision usually comes from an import
        done weeks ago."""

        session.add(MetricQueryTemplate(name="MySQL · QPS", promql="up"))
        session.flush()

        confirm_import(
            session,
            SOURCE_ID,
            [
                _item(name="MySQL · QPS", ref_id="A", panel_id=2),
                _item(name="MySQL · QPS", ref_id="B", panel_id=3),
            ],
        )
        session.flush()

        names = {row.name for row in session.exec(select(MetricQueryTemplate)).all()}
        assert names == {"MySQL · QPS", "MySQL · QPS (2)", "MySQL · QPS (3)"}

    def test_required_labels_are_stored_for_matching(self, session) -> None:
        confirm_import(
            session,
            SOURCE_ID,
            [_item(final_promql='mysql_up{instance="{{instance}}"}', required_labels=["instance"])],
        )
        session.flush()

        template = session.exec(select(MetricQueryTemplate)).one()
        assert metric_templates.required_labels(template) == ("instance",)


class TestConfirmValidation:
    def test_an_empty_query_is_refused(self, session) -> None:
        with pytest.raises(ValueError):
            confirm_import(session, SOURCE_ID, [_item(final_promql="   ")])

    def test_an_oversized_query_is_refused(self, session) -> None:
        with pytest.raises(ValueError):
            confirm_import(session, SOURCE_ID, [_item(final_promql="a" * 4097)])

    def test_a_query_outside_the_scope_guard_is_refused(self, session) -> None:
        """The same check a hand-written template gets when saved. Import is
        another door onto the same table, and a door without the check is how
        the guard stops meaning anything."""

        with pytest.raises(ValueError):
            confirm_import(session, SOURCE_ID, [_item(final_promql="rate(up[30d])")])

    def test_a_missing_dashboard_identity_is_refused(self, session) -> None:
        """Without it, re-import cannot align and the deep link cannot be built
        — the two reasons the origin row exists at all."""

        with pytest.raises(ValueError):
            confirm_import(session, SOURCE_ID, [_item(dashboard_uid="")])

    def test_an_unknown_source_is_refused(self, session) -> None:
        with pytest.raises(ValueError):
            confirm_import(session, "src_nope", [_item()])

    def test_an_update_cannot_claim_a_hand_written_template(self, session) -> None:
        manual = MetricQueryTemplate(
            name="manual", promql="up", required_labels_json="[]"
        )
        session.add(manual)
        session.commit()

        with pytest.raises(ValueError):
            confirm_import(
                session,
                SOURCE_ID,
                [_item(final_promql="mysql_up", template_id=manual.id)],
            )

        session.refresh(manual)
        assert manual.promql == "up"

    def test_an_update_must_match_the_stored_panel_target(self, session) -> None:
        confirm_import(session, SOURCE_ID, [_item(panel_id=1, ref_id="A")])
        session.commit()
        template = session.exec(select(MetricQueryTemplate)).one()

        with pytest.raises(ValueError):
            confirm_import(
                session,
                SOURCE_ID,
                [_item(panel_id=2, ref_id="B", template_id=template.id)],
            )

        session.refresh(template)
        assert template.promql == "mysql_up"


class TestWhatTheOriginRecords:
    """Found by running the real stack, not by a unit test (design §14 ledger).

    The origin has to remember **Grafana's** text, which is no longer the same
    string as the template's query once a variable has been bound. Storing the
    literal made every bound candidate compare unequal on the next re-import.
    """

    def test_it_stores_grafana_text_not_the_bound_literal(self, session) -> None:
        confirm_import(
            session,
            SOURCE_ID,
            [
                _item(
                    final_promql='mysql_up{instance="{{instance}}"}',
                    imported_promql='mysql_up{instance="$instance"}',
                )
            ],
        )
        session.flush()

        template = session.exec(select(MetricQueryTemplate)).one()
        origin = session.exec(select(MetricTemplateOrigin)).one()
        assert template.promql == 'mysql_up{instance="{{instance}}"}'
        assert origin.imported_promql == 'mysql_up{instance="$instance"}'

    def test_re_importing_an_unchanged_bound_candidate_is_unchanged(
        self, session
    ) -> None:
        """The defect in one assertion.

        Import a candidate with a variable, bind it, then diff the very same
        dashboard against what was stored. Before the fix this came back
        `UPSTREAM_CHANGED` — and `CONFLICT` for any template the user had also
        edited, which is a conflict nobody created, reappearing on every
        re-import.
        """

        dashboard = {
            "title": "T",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "P",
                    "targets": [
                        {"refId": "A", "expr": 'mysql_up{instance="$instance"}'}
                    ],
                }
            ],
        }
        parsed = parse_dashboard(dashboard)
        confirm_import(
            session,
            SOURCE_ID,
            [
                _item(
                    panel_id=1,
                    final_promql='mysql_up{instance="{{instance}}"}',
                    imported_promql=parsed[0].resolved_promql,
                )
            ],
        )
        session.flush()

        entries = diff_candidates(
            parsed,
            session.exec(select(MetricTemplateOrigin)).all(),
            session.exec(select(MetricQueryTemplate)).all(),
        )

        assert entries == []

    def test_a_real_upstream_edit_is_still_detected(self, session) -> None:
        """The fix must not achieve quiet by never reporting anything."""

        confirm_import(
            session,
            SOURCE_ID,
            [
                _item(
                    panel_id=1,
                    final_promql='mysql_up{instance="{{instance}}"}',
                    imported_promql='mysql_up{instance="$instance"}',
                )
            ],
        )
        session.flush()

        changed = parse_dashboard(
            {
                "title": "T",
                "panels": [
                    {
                        "id": 1,
                        "type": "timeseries",
                        "title": "P",
                        "targets": [
                            {
                                "refId": "A",
                                "expr": 'mysql_up{instance="$instance",new="yes"}',
                            }
                        ],
                    }
                ],
            }
        )
        entries = diff_candidates(
            changed,
            session.exec(select(MetricTemplateOrigin)).all(),
            session.exec(select(MetricQueryTemplate)).all(),
        )

        assert [entry.kind.value for entry in entries] == ["UPSTREAM_CHANGED"]

    def test_it_falls_back_to_the_query_when_nothing_else_is_given(
        self, session
    ) -> None:
        """Correct for the many candidates that have no variables at all."""

        confirm_import(session, SOURCE_ID, [_item(final_promql="mysql_up")])
        session.flush()

        assert (
            session.exec(select(MetricTemplateOrigin)).one().imported_promql
            == "mysql_up"
        )


class TestConfirmIsIdempotent:
    def test_confirming_the_same_panel_target_twice_collides(self, session) -> None:
        """**Reverse-validated.** A double click, or two tabs open at once, must
        not produce two templates for one panel. The constraint is in the
        database because a pre-check cannot see the other transaction."""

        from sqlalchemy.exc import IntegrityError

        confirm_import(session, SOURCE_ID, [_item()])
        session.commit()

        with pytest.raises(IntegrityError):
            confirm_import(session, SOURCE_ID, [_item(name="different name")])
            session.commit()
        session.rollback()

        assert len(session.exec(select(MetricTemplateOrigin)).all()) == 1


class TestAcceptingAnUpstreamChange:
    def test_it_updates_the_template_and_the_recorded_original(
        self, session
    ) -> None:
        confirm_import(session, SOURCE_ID, [_item(final_promql="mysql_up")])
        session.commit()
        template_id = session.exec(select(MetricQueryTemplate)).one().id

        confirm_import(
            session,
            SOURCE_ID,
            [_item(final_promql="mysql_up{new=\"yes\"}", template_id=template_id)],
        )
        session.commit()

        template = session.exec(select(MetricQueryTemplate)).one()
        origin = session.exec(select(MetricTemplateOrigin)).one()
        assert template.promql == 'mysql_up{new="yes"}'
        assert origin.imported_promql == 'mysql_up{new="yes"}'

    def test_an_update_does_not_claim_the_template_was_hand_edited(
        self, session
    ) -> None:
        """`user_modified` means "the user changed this". Accepting Grafana's
        version is the opposite, and setting the flag would make the next
        re-import report a conflict that nobody created."""

        confirm_import(session, SOURCE_ID, [_item()])
        session.commit()
        template_id = session.exec(select(MetricQueryTemplate)).one().id

        confirm_import(
            session, SOURCE_ID, [_item(template_id=template_id, final_promql="mysql_up{x=\"1\"}")]
        )
        session.commit()

        assert session.exec(select(MetricQueryTemplate)).one().user_modified is False


def _item(
    *,
    final_promql: str = "mysql_up",
    imported_promql: str = "",
    name: str = "MySQL Overview · Up",
    required_labels: list[str] | None = None,
    enabled: bool = False,
    display_unit: str = "",
    order: int = 0,
    dashboard_uid: str = "mysql-overview",
    panel_id: int = 2,
    ref_id: str = "A",
    template_id: int | None = None,
) -> ConfirmItem:
    return ConfirmItem(
        final_promql=final_promql,
        imported_promql=imported_promql,
        name=name,
        required_labels=tuple(required_labels or ()),
        enabled=enabled,
        display_unit=display_unit,
        order=order,
        origin=ConfirmOrigin(
            dashboard_uid=dashboard_uid,
            dashboard_title="MySQL Overview",
            panel_id=panel_id,
            panel_title="Up",
            ref_id=ref_id,
        ),
        template_id=template_id,
    )
