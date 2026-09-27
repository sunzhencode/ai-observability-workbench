"""Pure notification policy, lifecycle, retry and payload decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import re
from typing import Mapping, Sequence
from urllib.parse import urlsplit


MAX_POLICY_MATCHERS = 20
MAX_POLICY_CHANNELS = 20
MAX_PAYLOAD_MEMBERS = 5
RETRY_DELAYS_SECONDS = (60, 300, 900, 3600)
MAX_DELIVERY_ATTEMPTS = 5
MAX_NOTIFICATION_SECRET_LENGTH = 8_192


class ProviderKind(str, Enum):
    FEISHU_CUSTOM_BOT = "FEISHU_CUSTOM_BOT"
    SMTP = "SMTP"
    GENERIC_WEBHOOK = "GENERIC_WEBHOOK"


def validate_channel_configuration(
    provider: str, config: Mapping[str, object]
) -> dict[str, object]:
    """Validate save-time provider shape without DNS or opening secrets."""
    kind = ProviderKind(provider)
    clean = dict(config)
    if kind is ProviderKind.FEISHU_CUSTOM_BOT:
        mode = str(clean.get("mention_mode") or "NONE").upper()
        if mode not in {"NONE", "USERS", "ALL"}:
            raise ValueError("NOTIFICATION_MENTION_MODE_INVALID")
        users = clean.get("mention_users") or []
        if not isinstance(users, list) or len(users) > 50:
            raise ValueError("NOTIFICATION_MENTION_USERS_INVALID")
        for item in users:
            if not isinstance(item, Mapping) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", str(item.get("open_id") or "")
            ):
                raise ValueError("NOTIFICATION_OPEN_ID_INVALID")
        mention_on = clean.get("mention_on") or {}
        if not isinstance(mention_on, Mapping) or any(
            str(key) not in {item.value for item in DeliveryEvent}
            or not isinstance(value, bool)
            for key, value in mention_on.items()
        ):
            raise ValueError("NOTIFICATION_MENTION_EVENTS_INVALID")
        clean["mention_mode"] = mode
    elif kind is ProviderKind.SMTP:
        host = str(clean.get("host") or "").strip()
        port = int(str(clean.get("port") or 0))
        tls_mode = str(clean.get("tls_mode") or "STARTTLS").upper()
        if not host or (port, tls_mode) not in {(465, "TLS"), (587, "STARTTLS")}:
            raise ValueError("NOTIFICATION_SMTP_TARGET_INVALID")
        addresses = [str(clean.get("from_addr") or "")]
        recipients = clean.get("to_addrs") or []
        if not isinstance(recipients, list) or not recipients or len(recipients) > 50:
            raise ValueError("NOTIFICATION_SMTP_RECIPIENTS_INVALID")
        addresses.extend(str(item) for item in recipients)
        if any("@" not in item or item.strip() != item for item in addresses):
            raise ValueError("NOTIFICATION_SMTP_ADDRESS_INVALID")
        clean["host"] = host
        clean["port"] = port
        clean["tls_mode"] = tls_mode
    else:
        url = str(clean.get("url") or "").strip()
        parts = urlsplit(url)
        try:
            port = parts.port or 443
        except ValueError as exc:
            raise ValueError("NOTIFICATION_WEBHOOK_TARGET_INVALID") from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or port != 443
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("NOTIFICATION_WEBHOOK_TARGET_INVALID")
        timeout = float(str(clean.get("timeout_seconds") or 8.0))
        if not 1.0 <= timeout <= 30.0:
            raise ValueError("NOTIFICATION_WEBHOOK_TIMEOUT_INVALID")
        clean["url"] = url
        clean["timeout_seconds"] = timeout
    return clean


def validate_replacement_secret(provider: str, name: str, value: str) -> None:
    """Validate secret shape before encryption without resolving a target."""
    kind = ProviderKind(provider)
    allowed = {
        ProviderKind.FEISHU_CUSTOM_BOT: {"webhook", "signing_secret"},
        ProviderKind.SMTP: {"password"},
        ProviderKind.GENERIC_WEBHOOK: {"headers", "signing_secret"},
    }[kind]
    if name not in allowed or not value or len(value) > MAX_NOTIFICATION_SECRET_LENGTH:
        raise ValueError("NOTIFICATION_SECRET_INVALID")
    if kind is ProviderKind.FEISHU_CUSTOM_BOT and name == "webhook":
        parts = urlsplit(value)
        prefix = "/open-apis/bot/v2/hook/"
        token = parts.path.removeprefix(prefix)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"open.feishu.cn", "open.larksuite.com"}
            or (parts.port or 443) != 443
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or not parts.path.startswith(prefix)
            or not token
            or "/" in token
            or len(token) > 256
            or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
        ):
            raise ValueError("NOTIFICATION_FEISHU_WEBHOOK_INVALID")
    if kind is ProviderKind.GENERIC_WEBHOOK and name == "headers":
        try:
            import json

            headers = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("NOTIFICATION_WEBHOOK_HEADERS_INVALID") from exc
        if (
            not isinstance(headers, dict)
            or len(headers) > 50
            or any(
                not isinstance(key, str)
                or not isinstance(item, str)
                or not key
                or len(key) > 128
                or len(item) > 1_024
                or "\n" in key
                or "\r" in key
                or "\n" in item
                or "\r" in item
                for key, item in headers.items()
            )
        ):
            raise ValueError("NOTIFICATION_WEBHOOK_HEADERS_INVALID")


class DeliveryEvent(str, Enum):
    FIRING_OPENED = "FIRING_OPENED"
    SEVERITY_ESCALATED = "SEVERITY_ESCALATED"
    REMINDER = "REMINDER"
    RECOVERED = "RECOVERED"


class DeliveryState(str, Enum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    PERMANENTLY_FAILED = "PERMANENTLY_FAILED"
    SUPPRESSED = "SUPPRESSED"
    CANCELED = "CANCELED"


@dataclass(frozen=True, slots=True)
class NotificationMetricEvidence:
    """A bounded L1 metric fact that already exists in the product database."""

    metric_id: str
    display_name: str
    unit: str
    latest: float | int | str | None
    minimum: float | int | str | None
    maximum: float | int | str | None

    def payload(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id[:256],
            "display_name": self.display_name[:160],
            "unit": self.unit[:64],
            "latest": self.latest,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class NotificationDeepLink:
    label: str
    url: str

    def payload(self) -> dict[str, object]:
        return {"label": self.label[:256], "url": self.url[:2048]}


@dataclass(frozen=True, slots=True)
class NotificationEnrichment:
    """Best-effort projection; it is never a prerequisite for a Delivery."""

    occurrence_id: int | None
    metric_evidence: tuple[NotificationMetricEvidence, ...] = ()
    deep_links: tuple[NotificationDeepLink, ...] = ()
    evidence_status: str = "NOT_AVAILABLE"
    safe_code: str | None = None

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "evidence_status": self.evidence_status[:32],
            "metric_evidence": [item.payload() for item in self.metric_evidence[:2]],
            "deep_links": [item.payload() for item in self.deep_links[:2]],
        }
        if self.occurrence_id is not None:
            result["operational_occurrence_id"] = self.occurrence_id
        if self.safe_code is not None:
            result["evidence_safe_code"] = self.safe_code[:96]
        return result


@dataclass(frozen=True, slots=True)
class ResponseNotificationPolicy:
    """The only Response-to-platform-notification decision table.

    Providers receive already-planned deliveries and never interpret Incident
    response state themselves. Unknown stored states fail closed so a damaged
    or future projection cannot silently resume outbound collaboration.
    """

    allowed_events: frozenset[DeliveryEvent]
    terminated: bool = False

    def allows(self, event: DeliveryEvent) -> bool:
        return event in self.allowed_events


def response_notification_policy(response_state: str) -> ResponseNotificationPolicy:
    state = response_state.upper()
    if state == "UNACKNOWLEDGED":
        return ResponseNotificationPolicy(frozenset(DeliveryEvent))
    if state == "IN_PROGRESS":
        return ResponseNotificationPolicy(
            frozenset(
                {
                    DeliveryEvent.FIRING_OPENED,
                    DeliveryEvent.SEVERITY_ESCALATED,
                    DeliveryEvent.RECOVERED,
                }
            )
        )
    return ResponseNotificationPolicy(frozenset(), terminated=True)


class MatcherOperator(str, Enum):
    EQUAL = "="
    NOT_EQUAL = "!="
    REGEX = "=~"
    NOT_REGEX = "!~"


_GROUP_FIELD = re.compile(r"\Agroup\.[A-Za-z_][A-Za-z0-9_.-]{0,126}\Z")
_STABLE_FIELDS = frozenset({"aggregation_rule_id", "severity"})


@dataclass(frozen=True, slots=True)
class Matcher:
    field: str
    operator: MatcherOperator
    value: str

    def __post_init__(self) -> None:
        if self.field not in _STABLE_FIELDS and not _GROUP_FIELD.fullmatch(self.field):
            raise ValueError("NOTIFICATION_MATCHER_FIELD_INVALID")
        if len(self.value) > 512:
            raise ValueError("NOTIFICATION_MATCHER_VALUE_TOO_LONG")
        if self.operator in {MatcherOperator.REGEX, MatcherOperator.NOT_REGEX}:
            try:
                re.compile(self.value)
            except re.error as exc:
                raise ValueError("NOTIFICATION_MATCHER_REGEX_INVALID") from exc

    def matches(self, context: Mapping[str, object]) -> bool:
        if self.field.startswith("group."):
            group = context.get("group")
            actual = (
                str(group.get(self.field[6:], ""))
                if isinstance(group, Mapping)
                else ""
            )
        else:
            actual = str(context.get(self.field, ""))
        if self.operator is MatcherOperator.EQUAL:
            return actual == self.value
        if self.operator is MatcherOperator.NOT_EQUAL:
            return actual != self.value
        matched = re.fullmatch(self.value, actual) is not None
        return matched if self.operator is MatcherOperator.REGEX else not matched


@dataclass(frozen=True, slots=True)
class IncidentNotificationFact:
    incident_id: int
    occurrence_no: int
    source_id: str
    source_name: str
    title: str
    severity: str
    source_state: str
    freshness_state: str
    aggregation_rule_id: int | None
    group_labels: Mapping[str, str]
    member_count: int = 0
    members: tuple[Mapping[str, str], ...] = ()

    def route_context(self) -> dict[str, object]:
        return {
            "aggregation_rule_id": (
                "" if self.aggregation_rule_id is None else str(self.aggregation_rule_id)
            ),
            "source_id": self.source_id,
            "severity": self.severity,
            "group": dict(self.group_labels),
        }

    def payload_snapshot(self, *, change_version: int) -> dict[str, object]:
        members = tuple(_safe_member(item) for item in self.members[:MAX_PAYLOAD_MEMBERS])
        return {
            "incident_id": self.incident_id,
            "occurrence_no": self.occurrence_no,
            "title": self.title[:512],
            "severity": self.severity[:32],
            "source_state": self.source_state[:32],
            "change_version": change_version,
            "source_id": self.source_id[:128],
            "source_name": self.source_name[:160],
            "group_labels": {
                str(key)[:128]: str(value)[:256]
                for key, value in sorted(self.group_labels.items())
            },
            "member_count": max(self.member_count, len(self.members)),
            "members": list(members),
            "members_truncated": max(0, len(self.members) - len(members)),
        }


def _safe_member(value: Mapping[str, str]) -> dict[str, str]:
    allowed = (
        "alertname",
        "severity",
        "source_state",
        "cluster",
        "namespace",
        "service",
        "job",
        "workload",
        "summary",
    )
    limits = {"alertname": 160, "severity": 32, "source_state": 32, "summary": 240}
    result: dict[str, str] = {}
    for key in allowed:
        if any(part in key.lower() for part in ("token", "password", "secret", "webhook")):
            continue
        result[key] = str(value.get(key, ""))[: limits.get(key, 160)]
    return result


@dataclass(frozen=True, slots=True)
class NotificationChange:
    previous_source_state: str | None
    current_source_state: str
    previous_severity: str | None
    current_severity: str
    previous_handling_state: str | None
    current_handling_state: str
    change_version: int
    origin: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    revision_id: int
    logical_id: str
    name: str
    version: int
    priority: int
    matchers: tuple[Matcher, ...]
    scope_mode: str
    source_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    repeat_interval_seconds: int

    def __post_init__(self) -> None:
        if len(self.matchers) > MAX_POLICY_MATCHERS:
            raise ValueError("NOTIFICATION_POLICY_TOO_MANY_MATCHERS")
        if not self.channel_ids or len(self.channel_ids) > MAX_POLICY_CHANNELS:
            raise ValueError("NOTIFICATION_POLICY_CHANNELS_INVALID")
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise ValueError("NOTIFICATION_POLICY_CHANNELS_DUPLICATED")
        if self.scope_mode not in {"ALL", "SELECTED"}:
            raise ValueError("NOTIFICATION_POLICY_SCOPE_INVALID")
        if len(self.source_ids) > MAX_POLICY_CHANNELS:
            raise ValueError("NOTIFICATION_POLICY_SOURCES_INVALID")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("NOTIFICATION_POLICY_SOURCES_DUPLICATED")
        if self.scope_mode == "ALL" and self.source_ids:
            raise ValueError("NOTIFICATION_POLICY_SCOPE_IDS_FORBIDDEN")
        if self.scope_mode == "SELECTED" and not self.source_ids:
            raise ValueError("NOTIFICATION_POLICY_SCOPE_EMPTY")
        if self.repeat_interval_seconds != 0 and self.repeat_interval_seconds < 300:
            raise ValueError("NOTIFICATION_REPEAT_INTERVAL_TOO_SHORT")
        if self.repeat_interval_seconds > 2_592_000:
            raise ValueError("NOTIFICATION_REPEAT_INTERVAL_TOO_LONG")

    def matches(self, fact: IncidentNotificationFact) -> bool:
        if self.scope_mode == "SELECTED" and fact.source_id not in self.source_ids:
            return False
        context = fact.route_context()
        return all(matcher.matches(context) for matcher in self.matchers)


def choose_policy(
    fact: IncidentNotificationFact, policies: Sequence[PolicyCandidate]
) -> PolicyCandidate | None:
    ordered = sorted(policies, key=lambda item: (item.priority, item.revision_id))
    return next((item for item in ordered if item.matches(fact)), None)


def severity_rank(value: str) -> int:
    return {
        "info": 0,
        "warning": 1,
        "critical": 2,
    }.get(value.lower(), 0)


def is_escalation(change: NotificationChange) -> bool:
    return (
        change.previous_severity is not None
        and severity_rank(change.current_severity)
        > severity_rank(change.previous_severity)
    )


def should_create_route(
    fact: IncidentNotificationFact, change: NotificationChange
) -> bool:
    return (
        change.current_source_state.upper() == "FIRING"
        and change.origin in {"LIVE_POLL", "EXPLICIT_ACTIVATION"}
        and change.current_handling_state not in {"CLOSED", "FALSE_POSITIVE"}
        and fact.freshness_state.upper() != "STALE"
    )


def delivery_event_key(
    *,
    incident_id: int,
    route_id: int,
    target_id: int,
    event_type: DeliveryEvent,
    change_version: int,
    repeat_slot: int | None = None,
) -> str:
    raw = "|".join(
        str(item)
        for item in (
            incident_id,
            route_id,
            target_id,
            event_type.value,
            change_version,
            "" if repeat_slot is None else repeat_slot,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def retry_delay_seconds(event_key: str, attempt_count: int) -> int:
    index = min(max(attempt_count - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
    base = RETRY_DELAYS_SECONDS[index]
    jitter_window = max(1, base // 10)
    jitter = int(hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:8], 16)
    return base + jitter % jitter_window


def next_rate_limit_permit(
    attempt_times: Sequence[datetime], *, now: datetime
) -> datetime:
    minute = sorted(item for item in attempt_times if item > now - timedelta(seconds=60))
    second = [item for item in minute if item > now - timedelta(seconds=1)]
    candidates = [now]
    if len(second) >= 4:
        candidates.append(second[-4] + timedelta(seconds=1))
    if len(minute) >= 90:
        candidates.append(minute[-90] + timedelta(seconds=60))
    return max(candidates)
