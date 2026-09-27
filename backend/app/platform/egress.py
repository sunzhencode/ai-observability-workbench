"""Canonical public-egress guard shared by outbound adapters.

User-named HTTPS and SMTP targets must not turn the workbench into an SSRF
probe.  The guard therefore validates the resolved addresses before every
send, not only the hostname or the save-time shape.  Fixed safe codes never
carry the target value.

This module is intentionally below both the legacy provider compatibility
surface and the current platform adapters.  There is one implementation of the guard, while
each caller remains responsible for translating its domain-specific safe code.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

HTTPS_PORTS = frozenset({443})
SMTP_PORTS = frozenset({465, 587})

# Injectable for tests: host -> resolved addresses.
Resolver = Callable[[str], Iterable[str]]


class EgressRejected(ValueError):
    """A target the guard refuses. ``code`` is safe to surface."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EgressTarget:
    host: str
    port: int
    addresses: tuple[str, ...]


def default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EgressRejected("EGRESS_DNS") from exc
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return not (
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _assert_public_host(
    host: str,
    port: int,
    resolver: Resolver | None,
) -> EgressTarget:
    if not host:
        raise EgressRejected("EGRESS_NO_HOST")
    resolve = resolver or default_resolver
    addresses = tuple(dict.fromkeys(str(item) for item in resolve(host)))
    if not addresses:
        raise EgressRejected("EGRESS_DNS")
    if not all(_is_public(address) for address in addresses):
        raise EgressRejected("EGRESS_PRIVATE_ADDRESS")
    return EgressTarget(host=host, port=port, addresses=addresses)


def assert_https_shape(url: str) -> tuple[str, int]:
    """Validate scheme, userinfo, host and port without resolving DNS."""

    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "https":
        raise EgressRejected("EGRESS_SCHEME")
    if parts.username is not None or parts.password is not None:
        raise EgressRejected("EGRESS_USERINFO")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise EgressRejected("EGRESS_PORT") from exc
    if port not in HTTPS_PORTS:
        raise EgressRejected("EGRESS_PORT")
    host = parts.hostname or ""
    if not host:
        raise EgressRejected("EGRESS_NO_HOST")
    return host, port


def assert_public_https(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> EgressTarget:
    """Validate HTTPS shape and every resolved address."""

    host, port = assert_https_shape(url)
    return _assert_public_host(host, port, resolver)


def assert_smtp_shape(host: str, port: int) -> tuple[str, int]:
    """Validate the SMTP submission port and host without resolving DNS."""

    if port not in SMTP_PORTS:
        raise EgressRejected("EGRESS_PORT")
    clean = str(host or "").strip()
    if not clean:
        raise EgressRejected("EGRESS_NO_HOST")
    return clean, port


def assert_public_smtp(
    host: str,
    port: int,
    *,
    resolver: Resolver | None = None,
) -> EgressTarget:
    """Validate SMTP shape and every resolved address."""

    clean, checked_port = assert_smtp_shape(host, port)
    return _assert_public_host(clean, checked_port, resolver)
