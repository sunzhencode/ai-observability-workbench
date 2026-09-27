"""Pure Service Catalog and deterministic Service Mapping decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Mapping
from urllib.parse import urlsplit

from app.domains.alerting.models import Matcher, MatcherOperator


class ServiceCriticality(StrEnum):
    TIER_0 = "TIER_0"
    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"
    TIER_3 = "TIER_3"

    @property
    def ack_sla_seconds(self) -> int:
        return {
            ServiceCriticality.TIER_0: 5 * 60,
            ServiceCriticality.TIER_1: 15 * 60,
            ServiceCriticality.TIER_2: 30 * 60,
            ServiceCriticality.TIER_3: 60 * 60,
        }[self]


class ServiceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ServiceAssignmentState(StrEnum):
    UNMAPPED = "UNMAPPED"
    MAPPED = "MAPPED"
    SERVICE_AMBIGUOUS = "SERVICE_AMBIGUOUS"
    SERVICE_ARCHIVED = "SERVICE_ARCHIVED"


@dataclass(frozen=True, slots=True)
class ServiceDraft:
    name: str
    slug: str
    criticality: ServiceCriticality
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = self.name.strip()
        slug = self.slug.strip()
        links = tuple(link.strip() for link in self.links)
        if not 1 <= len(name) <= 120:
            raise ValueError("SERVICE_NAME_INVALID")
        if re.fullmatch(r"[a-z][a-z0-9-]{0,62}", slug) is None:
            raise ValueError("SERVICE_SLUG_INVALID")
        if len(links) > 10 or len(set(links)) != len(links):
            raise ValueError("SERVICE_LINKS_INVALID")
        for link in links:
            parts = urlsplit(link)
            if (
                len(link) > 2048
                or parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
            ):
                raise ValueError("SERVICE_LINK_HTTPS_REQUIRED")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "links", links)


@dataclass(frozen=True, slots=True)
class ServiceMappingRuleDraft:
    name: str
    priority: int
    service_id: int
    enabled: bool
    source_ids: tuple[str, ...]
    matchers: tuple[Matcher, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.name.strip()) <= 120:
            raise ValueError("SERVICE_MAPPING_NAME_INVALID")
        if not 0 <= self.priority <= 1_000_000:
            raise ValueError("SERVICE_MAPPING_PRIORITY_INVALID")
        if self.service_id < 1:
            raise ValueError("SERVICE_MAPPING_TARGET_INVALID")
        if len(self.source_ids) > 20 or len(set(self.source_ids)) != len(
            self.source_ids
        ):
            raise ValueError("SERVICE_MAPPING_SOURCE_SCOPE_INVALID")
        if len(self.matchers) > 20:
            raise ValueError("SERVICE_MAPPING_MATCHERS_INVALID")
        label_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_:.-]{0,127}")
        for matcher in self.matchers:
            if (
                matcher.label == "environment"
                or label_pattern.fullmatch(matcher.label) is None
            ):
                raise ValueError("SERVICE_MAPPING_MATCHER_LABEL_INVALID")
            if matcher.operator in {
                MatcherOperator.REGEX,
                MatcherOperator.NOT_REGEX,
            }:
                re.compile(matcher.value)


@dataclass(frozen=True, slots=True)
class PublishedServiceMappingRule:
    id: int
    priority: int
    service_id: int
    enabled: bool
    source_ids: tuple[str, ...]
    matchers: tuple[Matcher, ...]

    def matches(self, *, source_id: str, labels: Mapping[str, str]) -> bool:
        return (
            self.enabled
            and (not self.source_ids or source_id in self.source_ids)
            and all(matcher.matches(labels) for matcher in self.matchers)
        )


@dataclass(frozen=True, slots=True)
class ServiceMappingMember:
    source_id: str
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ServiceAssignmentDecision:
    service_id: int | None
    state: ServiceAssignmentState


def choose_service_assignment(
    members: tuple[ServiceMappingMember, ...],
    rules: tuple[PublishedServiceMappingRule, ...],
) -> ServiceAssignmentDecision:
    ordered = sorted(rules, key=lambda item: (item.priority, item.id))
    service_ids = {
        rule.service_id
        for member in members
        if (
            rule := next(
                (
                    candidate
                    for candidate in ordered
                    if candidate.matches(
                        source_id=member.source_id, labels=member.labels
                    )
                ),
                None,
            )
        )
        is not None
    }
    if not service_ids:
        return ServiceAssignmentDecision(None, ServiceAssignmentState.UNMAPPED)
    if len(service_ids) > 1:
        return ServiceAssignmentDecision(
            None, ServiceAssignmentState.SERVICE_AMBIGUOUS
        )
    return ServiceAssignmentDecision(
        next(iter(service_ids)), ServiceAssignmentState.MAPPED
    )
