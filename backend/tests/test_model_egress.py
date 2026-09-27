"""The model guard must be as hard as the notification one, separately.

ADR 0010 wants a second guard so a future local-model kind can permit loopback
without that permission ever being reachable from the notification path. A
second guard is only worth having if it is actually enforced, so every rejection
the notification guard makes is asserted here too -- against *this* module.
"""

from __future__ import annotations

import pytest

from app.providers.model.egress import (
    MODEL_KIND_OPENAI_COMPATIBLE,
    EgressRejected,
    assert_model_egress,
)

PUBLIC = "https://api.openai.com/v1/chat/completions"


def _resolves_to(*addresses: str):
    return lambda host: list(addresses)


def _assert(url: str, *, kind: str = MODEL_KIND_OPENAI_COMPATIBLE, addresses=("93.184.216.34",)):
    return assert_model_egress(url, kind=kind, resolver=_resolves_to(*addresses))


def test_a_public_https_endpoint_is_allowed() -> None:
    target = _assert(PUBLIC)
    assert target.host == "api.openai.com"
    assert target.port == 443


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",  # plaintext
        "https://user:pw@api.openai.com/v1",  # credentials land in logs
        "https://api.openai.com:8443/v1",  # off-allowlist port
        "https:///v1",  # no host
        "",
    ],
)
def test_the_shape_is_rejected_before_any_lookup(url: str) -> None:
    with pytest.raises(EgressRejected):
        _assert(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        # The one an attacker actually wants: cloud instance metadata.
        "169.254.169.254",
        "::1",
        # Loopback wearing an IPv6 costume -- the bypass a hand-copied guard
        # forgets, which is why the classification primitive is shared.
        "::ffff:127.0.0.1",
        "0.0.0.0",
    ],
)
def test_a_private_answer_is_refused_whatever_the_name_says(address: str) -> None:
    with pytest.raises(EgressRejected) as excinfo:
        _assert(PUBLIC, addresses=(address,))
    assert excinfo.value.code == "EGRESS_PRIVATE_ADDRESS"


def test_one_public_and_one_private_answer_is_still_refused() -> None:
    """Which address gets connected to is not ours to decide."""
    with pytest.raises(EgressRejected):
        _assert(PUBLIC, addresses=("93.184.216.34", "127.0.0.1"))


def test_a_name_that_resolves_to_nothing_is_refused() -> None:
    with pytest.raises(EgressRejected) as excinfo:
        _assert(PUBLIC, addresses=())
    assert excinfo.value.code == "EGRESS_DNS"


def test_an_unknown_kind_is_refused_rather_than_assumed_fine() -> None:
    """Adding a kind must force a decision about what it may reach."""
    with pytest.raises(EgressRejected) as excinfo:
        _assert(PUBLIC, kind="LOCAL_LLAMA")
    assert excinfo.value.code == "EGRESS_KIND"


def test_the_rejection_never_carries_the_address() -> None:
    """`str(exc)` on an httpx error embeds the URL; that reached the API once."""
    with pytest.raises(EgressRejected) as excinfo:
        _assert("https://internal-secret-host.corp/v1", addresses=("10.0.0.5",))
    text = str(excinfo.value) + repr(excinfo.value)
    assert "internal-secret-host" not in text
    assert "10.0.0.5" not in text


def test_there_is_no_way_to_turn_the_guard_off() -> None:
    import inspect

    from app.providers.model import egress

    source = inspect.getsource(egress)
    for switch in ("allow_private", "skip_egress", "disable", "ALLOW_INSECURE"):
        assert switch not in source, f"{switch} 是一个开关，护栏不该有开关"


def test_model_guard_does_not_import_notification_url_policy() -> None:
    import inspect

    from app.providers.model import egress

    source = inspect.getsource(egress)
    assert "assert_https_shape" not in source


def test_a_billed_failure_is_never_marked_safe_to_retry() -> None:
    """D45: a timeout may already have been paid for."""
    from app.providers.model.base import NEVER_BILLED, ModelCallError, ModelFailureKind

    assert ModelFailureKind.TIMEOUT not in NEVER_BILLED
    assert ModelFailureKind.RATE_LIMITED not in NEVER_BILLED
    assert ModelFailureKind.SERVICE_ERROR not in NEVER_BILLED
    assert ModelCallError(ModelFailureKind.TIMEOUT).possibly_billed is True
    assert ModelCallError(ModelFailureKind.EGRESS_REJECTED).possibly_billed is False
