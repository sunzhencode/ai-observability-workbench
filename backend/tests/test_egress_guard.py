"""The guard that keeps a user-named target from becoming an SSRF probe.

Everything here runs offline: the resolver is injected, so no test needs DNS and
no test can accidentally reach a real host.
"""

from __future__ import annotations

import pytest

from app.providers.egress import (
    EgressRejected,
    assert_public_https,
    assert_public_smtp,
)


def resolving_to(*addresses: str):
    return lambda host: list(addresses)


PUBLIC = resolving_to("93.184.216.34")


class TestScheme:
    def test_https_is_required(self) -> None:
        for url in ["http://example.com/hook", "ftp://example.com", "example.com"]:
            with pytest.raises(EgressRejected) as caught:
                assert_public_https(url, resolver=PUBLIC)
            assert caught.value.code == "EGRESS_SCHEME"

    def test_credentials_in_the_url_are_refused(self) -> None:
        with pytest.raises(EgressRejected) as caught:
            assert_public_https("https://user:pw@example.com/hook", resolver=PUBLIC)
        assert caught.value.code == "EGRESS_USERINFO"


class TestPorts:
    def test_only_443(self) -> None:
        assert assert_public_https("https://example.com/hook", resolver=PUBLIC).port == 443
        for url in ["https://example.com:8443/h", "https://example.com:80/h"]:
            with pytest.raises(EgressRejected) as caught:
                assert_public_https(url, resolver=PUBLIC)
            assert caught.value.code == "EGRESS_PORT"

    def test_smtp_submission_ports_only(self) -> None:
        for port in (465, 587):
            assert assert_public_smtp("smtp.example.com", port, resolver=PUBLIC).port == port
        for port in (25, 2525, 143):
            with pytest.raises(EgressRejected) as caught:
                assert_public_smtp("smtp.example.com", port, resolver=PUBLIC)
            assert caught.value.code == "EGRESS_PORT"


class TestResolvedAddress:
    """The point of the guard: the name is not the check, the answer is."""

    def test_a_public_name_resolving_to_metadata_is_refused(self) -> None:
        # The whole attack in one line: a perfectly ordinary hostname whose A
        # record points at the cloud metadata service.
        with pytest.raises(EgressRejected) as caught:
            assert_public_https(
                "https://webhook.example.com/hook",
                resolver=resolving_to("169.254.169.254"),
            )
        assert caught.value.code == "EGRESS_PRIVATE_ADDRESS"

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",  # itself
            "0.0.0.0",
            "10.1.2.3",  # RFC1918
            "172.16.5.4",
            "192.168.1.10",
            "169.254.169.254",  # link-local / metadata
            "::1",  # IPv6 loopback
            "fd00::1",  # IPv6 unique-local
            "fe80::1",  # IPv6 link-local
            "::ffff:127.0.0.1",  # loopback wearing an IPv6 costume
            "224.0.0.1",  # multicast
            "240.0.0.1",  # reserved
        ],
    )
    def test_every_non_public_range_is_refused(self, address: str) -> None:
        with pytest.raises(EgressRejected) as caught:
            assert_public_https("https://target.example/hook", resolver=resolving_to(address))
        assert caught.value.code == "EGRESS_PRIVATE_ADDRESS"

    def test_one_private_answer_poisons_the_whole_name(self) -> None:
        # Which address gets connected to is not ours to choose, so a mixed
        # answer is a way in.
        with pytest.raises(EgressRejected) as caught:
            assert_public_https(
                "https://target.example/hook",
                resolver=resolving_to("93.184.216.34", "127.0.0.1"),
            )
        assert caught.value.code == "EGRESS_PRIVATE_ADDRESS"

    def test_a_public_answer_passes_and_is_reported(self) -> None:
        target = assert_public_https(
            "https://hooks.example.com/services/abc", resolver=resolving_to("93.184.216.34")
        )
        assert target.host == "hooks.example.com"
        assert target.addresses == ("93.184.216.34",)

    def test_an_ip_literal_is_checked_the_same_way(self) -> None:
        with pytest.raises(EgressRejected) as caught:
            assert_public_https(
                "https://127.0.0.1/hook", resolver=resolving_to("127.0.0.1")
            )
        assert caught.value.code == "EGRESS_PRIVATE_ADDRESS"

    def test_a_name_with_no_answer_is_refused(self) -> None:
        with pytest.raises(EgressRejected) as caught:
            assert_public_https("https://nowhere.example/hook", resolver=resolving_to())
        assert caught.value.code == "EGRESS_DNS"


class TestSafeErrors:
    def test_the_code_never_carries_the_target(self) -> None:
        # Same lesson as sources/thanos.py: an exception that stringifies to the
        # URL leaks internal addresses through the API.
        with pytest.raises(EgressRejected) as caught:
            assert_public_https(
                "https://secret-internal-host.corp/hook", resolver=resolving_to("10.0.0.5")
            )
        assert "secret-internal-host" not in str(caught.value)
        assert "10.0.0.5" not in str(caught.value)
        assert str(caught.value) == caught.value.code


class TestShapeChecksDoNotResolve:
    """Save-time validation must not touch the network."""

    def test_https_shape_is_offline(self, monkeypatch) -> None:
        from app.providers import egress

        def explode(_host):  # pragma: no cover - must never run
            raise AssertionError("shape validation performed a DNS lookup")

        monkeypatch.setattr(egress, "default_resolver", explode)
        assert egress.assert_https_shape("https://hooks.example.com/x") == (
            "hooks.example.com",
            443,
        )

    def test_smtp_shape_is_offline(self, monkeypatch) -> None:
        from app.providers import egress

        def explode(_host):  # pragma: no cover - must never run
            raise AssertionError("shape validation performed a DNS lookup")

        monkeypatch.setattr(egress, "default_resolver", explode)
        assert egress.assert_smtp_shape("smtp.example.com", 587) == (
            "smtp.example.com",
            587,
        )

    def test_a_generic_webhook_config_validates_without_dns(self, monkeypatch) -> None:
        from app.providers import egress
        from app.providers.configs import GenericWebhookChannelConfig

        def explode(_host):  # pragma: no cover - must never run
            raise AssertionError("config validation performed a DNS lookup")

        monkeypatch.setattr(egress, "default_resolver", explode)
        config = GenericWebhookChannelConfig(url="https://hooks.example.com/services/x")
        assert config.url.endswith("/services/x")
