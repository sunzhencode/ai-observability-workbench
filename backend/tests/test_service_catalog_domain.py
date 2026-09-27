from __future__ import annotations

import pytest

from app.domains.alerting.models import Matcher, MatcherOperator
from app.domains.catalog.models import (
    PublishedServiceMappingRule,
    ServiceAssignmentState,
    ServiceCriticality,
    ServiceDraft,
    ServiceMappingMember,
    choose_service_assignment,
)


def _rule(
    rule_id: int,
    service_id: int,
    *,
    priority: int,
    alertname: str,
) -> PublishedServiceMappingRule:
    return PublishedServiceMappingRule(
        id=rule_id,
        priority=priority,
        service_id=service_id,
        enabled=True,
        source_ids=(),
        matchers=(Matcher("alertname", MatcherOperator.EQUALS, alertname),),
    )


def test_service_tiers_have_the_frozen_ack_sla_defaults() -> None:
    assert [item.ack_sla_seconds for item in ServiceCriticality] == [
        300,
        900,
        1800,
        3600,
    ]


def test_service_links_are_https_display_links_without_credentials() -> None:
    draft = ServiceDraft(
        " Checkout ",
        "checkout ",
        ServiceCriticality.TIER_0,
        (" https://runbooks.example/a ",),
    )
    assert draft.name == "Checkout"
    assert draft.slug == "checkout"
    assert draft.links == ("https://runbooks.example/a",)

    with pytest.raises(ValueError, match="SERVICE_LINK_HTTPS_REQUIRED"):
        ServiceDraft("Checkout", "checkout", ServiceCriticality.TIER_0, ("http://runbooks.example/a",))
    with pytest.raises(ValueError, match="SERVICE_LINK_HTTPS_REQUIRED"):
        ServiceDraft("Checkout", "checkout", ServiceCriticality.TIER_0, ("https://user:secret@example/a",))


def test_mapping_uses_priority_then_stable_rule_id_for_each_member() -> None:
    member = ServiceMappingMember("source-a", {"alertname": "HighErrors"})
    decision = choose_service_assignment(
        (member,),
        (
            _rule(9, 90, priority=20, alertname="HighErrors"),
            _rule(4, 40, priority=10, alertname="HighErrors"),
            _rule(3, 30, priority=10, alertname="HighErrors"),
        ),
    )

    assert decision.service_id == 30
    assert decision.state is ServiceAssignmentState.MAPPED


def test_different_member_services_are_ambiguous_not_split() -> None:
    decision = choose_service_assignment(
        (
            ServiceMappingMember("source-a", {"alertname": "HighErrors"}),
            ServiceMappingMember("source-a", {"alertname": "QueueLag"}),
        ),
        (
            _rule(1, 10, priority=10, alertname="HighErrors"),
            _rule(2, 20, priority=10, alertname="QueueLag"),
        ),
    )

    assert decision.service_id is None
    assert decision.state is ServiceAssignmentState.SERVICE_AMBIGUOUS


def test_no_member_match_is_unmapped() -> None:
    decision = choose_service_assignment(
        (ServiceMappingMember("source-a", {"alertname": "Other"}),),
        (_rule(1, 10, priority=10, alertname="HighErrors"),),
    )
    assert decision.service_id is None
    assert decision.state is ServiceAssignmentState.UNMAPPED
