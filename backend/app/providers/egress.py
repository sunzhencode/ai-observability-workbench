"""Compatibility imports for the canonical platform egress guard."""

from __future__ import annotations

from app.platform import egress as _canonical
from app.platform.egress import (
    HTTPS_PORTS,
    SMTP_PORTS,
    EgressRejected,
    EgressTarget,
    Resolver,
    assert_https_shape,
    assert_smtp_shape,
    default_resolver,
)


def assert_public_https(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> EgressTarget:
    """Compatibility wrapper preserving the injectable legacy resolver."""

    return _canonical.assert_public_https(
        url,
        resolver=resolver or default_resolver,
    )


def assert_public_smtp(
    host: str,
    port: int,
    *,
    resolver: Resolver | None = None,
) -> EgressTarget:
    """Compatibility wrapper preserving the injectable legacy resolver."""

    return _canonical.assert_public_smtp(
        host,
        port,
        resolver=resolver or default_resolver,
    )

__all__ = [
    "HTTPS_PORTS",
    "SMTP_PORTS",
    "EgressRejected",
    "EgressTarget",
    "Resolver",
    "assert_https_shape",
    "assert_public_https",
    "assert_public_smtp",
    "assert_smtp_shape",
    "default_resolver",
]
