"""Tables created by **versioned migrations**, on their own MetaData.

Two model modules is deliberate, and this is the line between them: `models.py`
(M1 / F17) lives on `SQLModel.metadata` and its tables were created by
`create_all` back in migrations 1-4, whose checksums can never change now.
Everything added since gets an explicit create in a numbered migration, and
needs a separate MetaData so a stray `create_all` cannot bring it into
existence early.

**This file was `f20_models.py` until 2026-07-31.** That name dated from when
F20 was the only thing in it; it now holds tables from F20, F21, F22, F26 and
F27, so it was telling every new reader something false. `app/f20_models.py`
survives as a re-export shim **that only `migrations.py` may import**: the
`from app.registry_models import ...` lines inside applied migration functions are
covered by those migrations' checksums, so rewriting them would stop every
existing database from starting. `tests/test_model_module_boundary.py` fails
the build if anything else reaches for the old path.

For the same reason the base class is **still called `F20Model`** and has to
stay that way: it appears in the source text of every model a migration lists in
`checksum_dependencies` (`class EventSource(F20Model, table=True)`), so renaming
it breaks identically. The name is wrong and it is not fixable — that is part of
what "additive only" costs, and it is cheaper than a broken upgrade path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    UniqueConstraint,
    text as sa_text,
)
from sqlmodel import Field, SQLModel

from app.models import utcnow


class F20Model(SQLModel):
    metadata = MetaData()


class EventSource(F20Model, table=True):
    id: str = Field(primary_key=True)
    type: str = Field(default="ALERTMANAGER", index=True)
    name: str = Field(index=True)
    lifecycle_state: str = Field(default="DISABLED", index=True)
    active_revision_id: Optional[int] = Field(
        default=None, foreign_key="eventsourcerevision.id"
    )
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    archived_at: Optional[datetime] = None
    version: int = Field(default=1)


class EventSourceRevision(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint("source_id", "revision_no", name="uq_event_source_revision"),
        Index(
            "uq_event_source_active_revision",
            "source_id",
            unique=True,
            sqlite_where=sa_text("internal_state = 'ACTIVE'"),
        ),
        Index(
            "uq_event_source_pending_revision",
            "source_id",
            unique=True,
            sqlite_where=sa_text("internal_state = 'PENDING'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    revision_no: int
    internal_state: str = Field(default="PENDING", index=True)
    poll_interval_seconds: int = 30
    resolution_grace_seconds: int = 60
    max_parallel_endpoints: int = 4
    watchdog_enabled: bool = False
    watchdog_alertname: str = "Watchdog"
    watchdog_identity_label: str = "cluster"
    watchdog_missing_after_seconds: int = 90
    thanos_binding_id: Optional[int] = Field(
        default=None, foreign_key="sourceevidencebinding.id"
    )
    last_test_status: Optional[str] = None
    tested_at: Optional[datetime] = None
    safe_error_code: Optional[str] = None
    legacy_connection_profile_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    activated_at: Optional[datetime] = None


class AlertmanagerEndpointRevision(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint(
            "source_revision_id", "position", name="uq_endpoint_revision_position"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_revision_id: int = Field(
        foreign_key="eventsourcerevision.id", index=True
    )
    position: int = 0
    canonical_url: str
    enabled: bool = True
    auth_kind: str = "NONE"
    username: str = ""
    secret_envelope_json: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )


class SourcePollRun(F20Model, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    source_revision_id: int = Field(
        foreign_key="eventsourcerevision.id", index=True
    )
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: Optional[datetime] = None
    completeness: str = Field(default="FAILED", index=True)
    endpoint_total: int = 0
    endpoint_succeeded: int = 0
    endpoint_failed: int = 0
    normalized_alert_count: int = 0
    safe_error_codes: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class EndpointPollResult(F20Model, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    poll_run_id: int = Field(foreign_key="sourcepollrun.id", index=True)
    endpoint_revision_id: int = Field(
        foreign_key="alertmanagerendpointrevision.id", index=True
    )
    status: str = Field(index=True)
    alert_count: int = 0
    duration_ms: int = 0
    safe_error_code: Optional[str] = None


class AlertEndpointObservation(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint(
            "alert_id", "endpoint_revision_id", name="uq_alert_endpoint_observation"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # Alert lives in the immutable pre-F20 metadata; the relationship is logical
    # until a future table rebuild can add a physical SQLite foreign key safely.
    alert_id: int = Field(index=True)
    endpoint_revision_id: int = Field(
        foreign_key="alertmanagerendpointrevision.id", index=True
    )
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class MonitoredCluster(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint("source_id", "identity_value", name="uq_monitored_cluster"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    identity_value: str
    inventory_state: str = Field(default="DISCOVERED", index=True)
    first_discovered_at: datetime = Field(default_factory=utcnow)
    monitoring_started_at: datetime = Field(default_factory=utcnow)
    last_seen_at: Optional[datetime] = None
    ignored_at: Optional[datetime] = None
    ignored_reason: Optional[str] = None
    version: int = 1


class AggregationRuleSource(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint("rule_id", "source_id", name="uq_aggregation_rule_source"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(index=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)


class NotificationPolicySource(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint(
            "policy_revision_id", "source_id", name="uq_notification_policy_source"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    policy_revision_id: int = Field(index=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)


class HistoricalDataSource(F20Model, table=True):
    id: str = Field(primary_key=True)
    type: str = Field(default="THANOS", index=True)
    name: str = Field(index=True)
    lifecycle_state: str = Field(default="DISABLED", index=True)
    active_revision_id: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    archived_at: Optional[datetime] = None
    version: int = 1


class HistoricalDataSourceRevision(F20Model, table=True):
    """F21: the connection a Historical Data Source actually uses.

    Before F21 a ``HistoricalDataSource`` was only a name; the live Thanos
    address came from an F17 ``ConnectionProfile`` or ``.env`` and could not be
    seen or edited in the UI.  This revision carries it, mirroring
    ``EventSourceRevision`` so both source kinds share one configuration
    contract.  Thanos has a single address: multi-endpoint HA semantics
    deliberately do not sink down here.
    """

    __table_args__ = (
        UniqueConstraint(
            "historical_source_id", "revision_no", name="uq_historical_revision"
        ),
        Index(
            "uq_historical_active_revision",
            "historical_source_id",
            unique=True,
            sqlite_where=sa_text("internal_state = 'ACTIVE'"),
        ),
        Index(
            "uq_historical_pending_revision",
            "historical_source_id",
            unique=True,
            sqlite_where=sa_text("internal_state = 'PENDING'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    historical_source_id: str = Field(
        foreign_key="historicaldatasource.id", index=True
    )
    revision_no: int
    internal_state: str = Field(default="PENDING", index=True)
    canonical_url: str
    auth_kind: str = "NONE"
    username: str = ""
    secret_envelope_json: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    timeout_seconds: int = 15
    last_test_status: Optional[str] = None
    tested_at: Optional[datetime] = None
    safe_error_code: Optional[str] = None
    legacy_connection_profile_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    activated_at: Optional[datetime] = None


class SourceThanosConfig(F20Model, table=True):
    """F22: the optional history address that belongs to one Event Source.

    Thanos used to be a first-class resource reached through a binding with a
    scope mode and matchers.  It does exactly one thing in this product --
    bounded history backfill at startup -- so it collapses into an optional
    part of the source's configuration: at most one per source, empty URL
    meaning "do not backfill".

    It is a separate table rather than five more columns on
    ``EventSourceRevision`` because migration 5 lists that model among its
    checksum dependencies: editing the class would change an applied
    migration's checksum and every existing database would refuse to start.
    """

    __table_args__ = (
        UniqueConstraint("source_id", name="uq_source_thanos_config"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    canonical_url: str = ""
    auth_kind: str = "NONE"
    username: str = ""
    secret_envelope_json: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    timeout_seconds: int = 15
    last_test_status: Optional[str] = None
    tested_at: Optional[datetime] = None
    safe_error_code: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)


class SourceEvidenceBinding(F20Model, table=True):
    __table_args__ = (
        UniqueConstraint(
            "source_id", "historical_source_id", name="uq_source_evidence_binding"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    historical_source_id: str = Field(
        foreign_key="historicaldatasource.id", index=True
    )
    scope_mode: str = "UNSCOPED"
    matchers_json: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    enabled: bool = False
    version: int = 1
    last_preview_summary: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )


class IncidentOccurrence(F20Model, table=True):
    """F26: one immutable record per *ended* occurrence of an Incident group.

    Before this table the repository had no history object at all.
    ``Incident.occurrence_no`` is a counter on the live row, a recurrence
    overwrites ``occurrence_started_at``, and ``IncidentChange`` is an in-memory
    dataclass -- so every source-state transition was computed and then thrown
    away. "What happened last month?" was unanswerable by construction.

    Append-only (ADR 0008). A record is never rewritten by a later recurrence,
    regroup, source disable/archive or handling change; the human conclusion is
    only mutable *within* the occurrence it belongs to. That immutability is the
    whole value: a list that can be emptied retroactively is not a history.

    **Deliberately no foreign keys.** ``Incident`` lives on the other metadata so
    a real FK is impossible, but the missing constraint is also the point: these
    records are kept far longer than the runtime data (365 vs 30 days), so the
    Incident, its Alerts, the source and the rule will all be deleted from under
    them. Everything needed to *read* a record is therefore denormalised into it,
    and ``incident_id`` is only a jump hint that may already be stale -- SQLite
    reuses rowids, so it must never be treated as identity.
    """

    __table_args__ = (
        # One seal per occurrence. Repeated polls, a restart mid-transaction or a
        # replay must not be able to write a second row for the same occurrence.
        UniqueConstraint(
            "incident_id", "occurrence_no", name="uq_incident_occurrence"
        ),
        # The list is always "newest first, by source"; the cursor is the id.
        Index("ix_incident_occurrence_recovered", "recovered_at", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(index=True)
    occurrence_no: int = 1

    # --- denormalised so the record survives everything it points at ---
    source_id: str = Field(index=True)
    source_name: str = ""
    group_key: str = Field(index=True)
    title: str = ""
    aggregation_rule_id: Optional[int] = None
    aggregation_rule_name: Optional[str] = None

    started_at: datetime = Field(default_factory=utcnow)
    # No `index=True` here: `ix_incident_occurrence_recovered` above already
    # leads with this column, so a single-column index would be dead weight.
    recovered_at: datetime = Field(default_factory=utcnow)
    member_count: int = 0
    # The highest severity among the members *at seal time*, NOT a running peak
    # over the occurrence: a member that escalated and then de-escalated only
    # leaves its final value behind. Named for what it is on purpose -- calling
    # it `peak_severity` would be a lie in a column name. A true peak would need
    # a column on Incident maintained every poll, which is a lot of machinery for
    # a field nobody has asked to sort by yet (design D10).
    member_max_severity: str = "info"

    # The human's verdict on this occurrence. This is the one field that is not
    # frozen at seal time, and the exception is deliberate: recovery never settles
    # handling (CAP-04.9), so at the moment of sealing this is almost always
    # "NEW". Freezing it here would make the conclusion filter -- the criterion
    # the user actually asked to slice history by -- answer "unsettled" for every
    # record forever.
    #
    # The mutable window is derived, not stored: a record may be updated only
    # while the Incident is still on this same `occurrence_no`. Once the group
    # fires again the counter advances and this record is closed for good; once
    # the Incident is deleted nothing can reach it at all. So the facts of the
    # occurrence (times, counts, severity) are immutable, the row can never be
    # deleted or moved out of history, and "append-only" still holds where it
    # matters -- a history that could be retroactively emptied was the whole thing
    # ADR 0008 set out to prevent.
    handling_conclusion: str = "NEW"


# ---------------------------------------------------------------------------
# F27: alert evidence and AI investigation (ADR 0009 / 0010 / 0011)
# ---------------------------------------------------------------------------


class MetricQueryTemplate(F20Model, table=True):
    """A parameterised, user-selectable query that produces an auxiliary curve.

    Auxiliary curves answer "what else is off around this alert"; the primary
    curve, derived from the alert's own expression, needs no configuration at all
    (ADR 0009). Templates are therefore deliberately secondary: the user's own
    alerts turned out to span exporters well outside kube-prometheus, so a
    built-in catalogue can never have the coverage the primary curve does.

    **The PromQL here is written whole by its author and is never rewritten.**
    The counter/gauge auto-`rate()` rule applies only to the tier-2 path where
    the system composes a query from a bare metric name. Wrapping a template
    again would turn `1 - avg(rate(...))` into a syntax error, and
    `kube_pod_container_status_restarts_total` needs `increase()` rather than
    `rate()` to stay legible at all (design §5.1).
    """

    __table_args__ = (
        UniqueConstraint("builtin_key", name="uq_metric_template_builtin"),
        Index("ix_metric_template_enabled", "enabled", "name"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    promql: str = ""
    # JSON array. A template is only a candidate when the alert carries *every*
    # label listed here, so an under-specified template silently matching the
    # wrong series is not possible.
    required_labels_json: str = "[]"
    # Written for the model to read (stage 2): "what question does this answer".
    description: str = ""

    # Identifies a shipped template so re-seeding is idempotent. NULL for
    # user-created rows, and unique so a key can never be seeded twice.
    builtin_key: Optional[str] = Field(default=None, index=True)
    # Once the user edits a shipped template, later versions stop overwriting it.
    # Same trade-off as the F21 one-time adoption: what the user changed wins.
    user_modified: bool = False
    # Shipped templates arrive switched off. None of them has been verified
    # against this user's cluster, and an unverified query that draws nothing is
    # worse than one the user turned on deliberately.
    enabled: bool = Field(default=False, index=True)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ModelChannel(F20Model, table=True):
    """A configured outbound model service in the compatibility registry.

    Parallel to `NotificationChannel` rather than part of it: both are typed
    outbound targets with immutable kinds, but their activation semantics differ.
    Folding it into the notification kind enum would drag
    `SUPPORTED_KINDS` and `RouteSnapshot.provider` along with it, where "route an
    Incident to a model" is meaningless (ADR 0010).
    """

    __table_args__ = (Index("ix_model_channel_enabled", "enabled", "name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    # Chosen at creation and immutable afterwards: changing it means a different
    # destination, which is a new channel. A future local-model kind carries its
    # own egress rules rather than adding a switch to the existing guard.
    kind: str = Field(default="OPENAI_COMPATIBLE", index=True)
    enabled: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ModelChannelRevision(F20Model, table=True):
    """Config + encrypted secret for an internally versioned model channel.

    Saving atomically promotes the new revision to ACTIVE and retires the old
    revision. A synthetic test records diagnostics but does not change state.
    """

    __table_args__ = (
        Index("ix_model_channel_revision_state", "channel_id", "state"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(index=True)
    state: str = Field(default="DRAFT", index=True)  # DRAFT / ACTIVE / RETIRED
    # Per-kind pydantic shape (base_url, model, temperature...). No secret here.
    config_envelope_json: str = "{}"
    # Fernet-encrypted. Never returned by any API, never rendered into the DOM.
    secret_envelope_json: str = ""
    tested_ok_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Investigation(F20Model, table=True):
    """One user-triggered, read-only model analysis of one alert group.

    Only the final product is stored (design D14): hypotheses, their verdicts,
    the evidence each one cites, and a snapshot of what that evidence was. The
    intermediate tool calls are not kept -- the deterministic data they read is
    already the source of truth, and storing a second copy would let the two
    disagree.

    Denormalised and free of foreign keys for the same reason as
    `IncidentOccurrence`: retention here (90 days) outlives the runtime data
    (30 days), so the Incident it points at will be deleted first.
    """

    __table_args__ = (
        Index("ix_investigation_incident", "incident_id", "created_at"),
        Index("ix_investigation_created", "created_at", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(index=True)
    occurrence_no: int = 1
    group_key: str = Field(index=True)
    source_id: str = Field(index=True)
    source_name: str = ""

    model_channel_id: Optional[int] = None
    model_name: str = ""

    # False when the model could not produce the required hypothesis structure
    # after a retry. The raw text is then shown with an explicit "no structured
    # conclusion" label rather than being silently accepted (ADR 0011).
    structured: bool = True
    result_json: str = "{}"
    # curve_id / fact_id -> frozen description. NOTE: this column predates the
    # design review, which found that a title/query/window triple is not enough
    # to audit what the model actually saw. The table is frozen by v11's
    # checksum, so stage 2 adds what it needs in a new table rather than here.
    evidence_snapshot_json: str = "{}"
    raw_text: str = ""

    created_at: datetime = Field(default_factory=utcnow, index=True)


class MetricTemplateExtras(F20Model, table=True):
    """Ordering and display for a metric template.

    **This exists as its own table only because `MetricQueryTemplate` is frozen.**
    Migration 11 lists that class in its ``checksum_dependencies``, so adding a
    field to it would change an applied migration's checksum and make every
    existing database refuse to start. Do not "tidy this up" by merging the two.

    It carries what the design review added after v11 shipped: an explicit
    priority (name order silently decided which auxiliary curves survived the
    cap) and a display unit (a ratio charted without one reads as a raw count).
    A template with no row here simply takes the defaults.
    """

    __table_args__ = (
        UniqueConstraint("template_id", name="uq_metric_template_extras"),
        Index("ix_metric_template_extras_priority", "priority", "template_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    template_id: int = Field(index=True)
    # Lower runs first, ties broken by id. Mirrors how aggregation rules and
    # notification policies already order themselves, so there is one convention.
    priority: int = 100
    display_unit: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ModelCallLog(F20Model, table=True):
    """One outbound model call: what it cost, and what it produced.

    Added in v13 because a billed call was leaving no trace at all. Two distinct
    needs, and one row serves both:

    - **Audit.** "How many times did I press this, on which alerts, and what did
      it cost me" was unanswerable. Every other outbound path in this repository
      keeps a record (`ConfigAudit`, `NotificationDelivery`); the one that spends
      money did not.
    - **Idempotency.** A double-click was two billed calls, held back only by a
      disabled button in the browser. Storing the result lets an immediate repeat
      return the first answer instead of paying twice (ADR 0011 §7's
      `request_key` idea, applied to the synchronous path).

    Denormalised and free of foreign keys for the same reason as
    `IncidentOccurrence`: the Alert it names is deleted at 30 days while this is
    kept for the model-run retention window.
    """

    __table_args__ = (
        Index("ix_model_call_log_alert", "alert_id", "created_at"),
        Index("ix_model_call_log_created", "created_at", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: What was asked for. Only one purpose today; investigation adds another.
    purpose: str = Field(default="SUGGEST_QUERIES", index=True)
    alert_id: Optional[int] = Field(default=None, index=True)
    #: `source_id@version`, not the bare id. A stored run is only reusable while
    #: the configuration it was checked against is still the one in use (D38):
    #: proposals were dry-run against a particular store, and repointing the
    #: history address makes them claims about somewhere else.
    source_scope: str = ""

    model_channel_id: Optional[int] = None
    model_name: str = ""

    ok: bool = False
    code: str = "OK"
    detail: str = ""

    #: Reported by the service. Recorded for the bill, never trusted as truth
    #: about anything else -- the model does not get to tell us what it cost.
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    #: The rendered result, so an immediate repeat is answered from here rather
    #: than from a second paid call.
    result_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow, index=True)


# ---------------------------------------------------------------------------
# F28: judgment standards imported from Grafana (ADR 0013 / 0014)
#
# Four new tables and not one new column anywhere else. `MetricQueryTemplate` is
# frozen by v11 and `MetricTemplateExtras` by v12, so the reference baseline and
# the import origin — both of which are obviously *about* a template — have to
# live beside it instead of on it. Same cost as v12 paid, for the same reason.
# ---------------------------------------------------------------------------


class SourceGrafanaConfig(F20Model, table=True):
    """F28: the optional Grafana address that belongs to one Event Source.

    Shaped after `SourceThanosConfig` deliberately: Grafana is the third
    **read-only monitoring source**, not a third outbound target (ADR 0013).
    It is at most one per source, an empty URL means "no import here", and a
    private address is its normal form — the egress guard that rejects loopback
    and private ranges belongs to `providers/`, and applying it here would make
    the feature unusable on the day it shipped.

    **No `auth_kind`, no `username`.** Grafana is Bearer-or-anonymous (design
    §4): internal instances are routinely configured for anonymous read, so
    requiring a credential would lock some deployments out entirely. A null
    envelope *is* anonymous; there is no third case to name.
    """

    __table_args__ = (
        UniqueConstraint("source_id", name="uq_source_grafana_config"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    base_url: str = ""
    #: Fernet envelope, same handling as every other stored credential: encrypted
    #: at rest, never returned by an API, never rendered into the DOM.
    secret_envelope_json: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    timeout_seconds: int = 15
    #: The one thing borrowed from the outbound domain: an explicit successful
    #: test is the gate on importing. Borrowed because there is a credential
    #: here, not because the address is dangerous — without it the first failure
    #: would surface halfway through an import.
    last_test_status: Optional[str] = None
    tested_at: Optional[datetime] = None
    safe_error_code: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)


class MetricTemplateBaseline(F20Model, table=True):
    """F28: the user's "how much is too much" for one auxiliary curve.

    A constant the user declares, not a percentile anything computes — and not a
    threshold either. Thresholds belong to alert rules, sit on the primary curve
    and may not be overridden by hand (ADR 0014); this sits on a template and
    only ever applies to auxiliary curves, which have no alert rule and
    therefore nothing in the system saying what "high" means.

    **The most protected field in the capability.** It exists nowhere else — not
    in Grafana, not upstream, only in the user's head — so no automatic flow
    (re-import, version upgrade, built-in refresh) may overwrite or clear it.

    Direction is stored, never inferred: Grafana's threshold steps carry a
    colour and no direction semantics, and `*_available` / `*_free` / `*_idle`
    are common enough that a guessed direction would read "nearly out of memory"
    as "plenty of memory".
    """

    __table_args__ = (
        UniqueConstraint("template_id", name="uq_metric_template_baseline"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: A real foreign key, unlike the history tables: this row is meaningless
    #: without its template and should go when it does. Both live on this same
    #: MetaData, so the constraint is expressible here.
    template_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("metricquerytemplate.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    #: The raw number the query returns, not the percentage in the reader's
    #: head: `1 - avg(rate(...))` answers `0.85` where a person thinks `85`.
    #: Typing 85 raises no error and simply makes the verdict say "not breached"
    #: forever, so the UI shows the live value beside this input (design §7.3).
    value: float = 0.0
    #: `HIGH_IS_BAD` / `LOW_IS_BAD`. Validated in the service layer rather than
    #: by a database enum, matching how every other enum in this schema is held.
    direction: str = "HIGH_IS_BAD"
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MetricTemplateOrigin(F20Model, table=True):
    """F28: which Grafana panel a template was imported from.

    Two uses, and the table earns its place on either one: re-importing the same
    dashboard has to diff against what was imported last time, and the "open the
    full picture in Grafana" deep link is *assembled* from `dashboard_uid` and
    `panel_id` rather than taken from the alert's own annotations (ADR 0009's
    principle extended — that text is written by the monitored system).

    Its presence is also what makes a template "from Grafana" in the list; a
    template with no row here was written by hand.
    """

    __table_args__ = (
        UniqueConstraint("template_id", name="uq_metric_template_origin"),
        # Idempotency lives here, not in a pre-check: confirming twice, or from
        # two browser tabs at once, must collide instead of producing a second
        # template for the same panel target.
        UniqueConstraint(
            "source_id",
            "dashboard_uid",
            "panel_id",
            "ref_id",
            name="uq_metric_template_origin_panel",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    template_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("metricquerytemplate.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    source_id: str = Field(foreign_key="eventsource.id", index=True)
    dashboard_uid: str = ""
    #: Kept for a reason that pays off later: `required_labels` only checks that
    #: a label *exists*, not that it means the same thing (ADR 0012), and import
    #: multiplies that weakness by the number of templates. "This one came from
    #: the MySQL dashboard" is a free semantic signal for tightening the match
    #: one day — unused today, and unrecoverable if not recorded now.
    dashboard_title: str = ""
    panel_id: int = 0
    panel_title: str = ""
    #: **Not nullable**, defaulting to the empty string. SQLite treats NULLs as
    #: distinct inside a UNIQUE index, so a nullable column here would let the
    #: constraint above exist and never once fire (review Q-补).
    ref_id: str = ""
    #: The query **as imported, in full** — not a digest. The conflict branch of
    #: re-import has to show three versions side by side (what was imported,
    #: what you changed it to, what Grafana has now), and a hash can display
    #: none of them. PromQL is under 2 KB; comparison is a string compare.
    imported_promql: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class DeliveryEvidenceSnapshot(F20Model, table=True):
    """F28 stage 3: the evidence summary carried by one occurrence's cards.

    Computed once per occurrence, in the `DeliveryWorker` **after** the
    transaction commits — never in the Planner, which runs inside the poll's
    write transaction with `poll_lock` held, where one upstream timeout would
    stall a whole polling round (design §14 R-2).

    Reminder and recovery cards read the row back rather than querying again: a
    4-hourly repeat must not become a 4-hourly scan of the history store.

    Created here in v14 with the other three even though no stage-1 code touches
    it. Waiting would fork the schema — a fresh database gets it from migration
    5's argument-less `create_all`, an upgraded one would wait for v15.
    """

    __table_args__ = (
        UniqueConstraint(
            "incident_id", "occurrence_no", name="uq_delivery_evidence"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: A plain int, not a foreign key: `Incident` lives on the other MetaData so
    #: the constraint is not expressible. Precedent: `IncidentOccurrence`.
    incident_id: int = Field(index=True)
    occurrence_no: int = 1
    #: A JSON envelope rather than columns, so the summary's shape can evolve
    #: after v14 freezes this class — the same move F24 made for channel config.
    summary_json: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    computed_at: datetime = Field(default_factory=utcnow)


class MetricTemplateSourceScope(F20Model, table=True):
    """F28 stage 2: existing Source Scope semantics for a frozen template.

    `MetricQueryTemplate` (v11) and `MetricTemplateExtras` (v12) cannot gain a
    field.  This side row therefore carries the scope mode and selected source
    ids together.  Absence means ALL, matching every other Source Scope default.
    Origin identity is deliberately not reused: where a template came from and
    where it is allowed to run are independently user-editable facts.
    """

    __table_args__ = (
        UniqueConstraint("template_id", name="uq_metric_template_source_scope"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    template_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("metricquerytemplate.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    scope_mode: str = "ALL"
    source_ids_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
