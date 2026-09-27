"""Where the workbench is allowed to send a model request.

A second guard rather than a second caller of the notification one, because the
two have to be able to *diverge* (ADR 0010): the day a local-model kind is
added, it must permit loopback -- and that permission must be impossible to
reach from the notification path, where "post an Incident to 127.0.0.1" is only
ever an SSRF probe. A shared function with a `kind` argument would put both
policies one boolean apart, which is exactly the shape this repository keeps
deleting (`platform/egress.py`: "There is no opt-out").

**What is deliberately *not* duplicated is the address classification.**
`_is_public` handles IPv4-mapped IPv6, link-local, reserved and unspecified
ranges; a second copy of that is how one copy ends up missing the `::ffff:`
unwrap and quietly allowing `::ffff:127.0.0.1`. So: the *policy* lives here and
is per-kind, the *primitive* is shared and has no policy in it.

Like the notification guard, this one has no off switch and runs before every
send -- not once at save time. DNS answers change, and a save-time-only check
is a check that was true once.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.platform.egress import (
    EgressRejected,
    EgressTarget,
    Resolver,
    _is_public,
    default_resolver,
)

__all__ = [
    "EgressRejected",
    "EgressTarget",
    "MODEL_KIND_OPENAI_COMPATIBLE",
    "Resolver",
    "assert_model_egress",
]

#: The only kind the first version implements (D17). A future local-model kind
#: is added as a new constant with its own branch below, never as a flag on this
#: one -- a kind is written in the data and visible in the UI, a flag is not.
MODEL_KIND_OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"


def _parse_model_https_target(url: str) -> tuple[str, int]:
    """Apply the model endpoint's own URL-shape policy."""

    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "https":
        raise EgressRejected("EGRESS_SCHEME")
    if parts.username is not None or parts.password is not None:
        raise EgressRejected("EGRESS_USERINFO")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise EgressRejected("EGRESS_PORT") from exc
    if port != 443:
        raise EgressRejected("EGRESS_PORT")
    host = parts.hostname or ""
    if not host:
        raise EgressRejected("EGRESS_NO_HOST")
    return host, port


def assert_model_egress(
    url: str, *, kind: str, resolver: Resolver | None = None
) -> EgressTarget:
    """Shape plus resolved addresses for one model endpoint.

    Raises `EgressRejected` with a code that is safe to surface. The address is
    never in the message: `str(exc)` on an httpx error embeds the full URL, and
    that is how an internal hostname reached the API once already.
    """

    if kind != MODEL_KIND_OPENAI_COMPATIBLE:
        # An unknown kind is not "probably fine". Refusing here means adding a
        # kind forces a deliberate decision about what it may reach.
        raise EgressRejected("EGRESS_KIND")

    host, port = _parse_model_https_target(url)

    resolve = resolver or default_resolver
    addresses = tuple(dict.fromkeys(str(item) for item in resolve(host)))
    if not addresses:
        raise EgressRejected("EGRESS_DNS")
    # Every answer must be public. One public and one private answer is still a
    # way in -- which address gets connected to is not ours to decide.
    if not all(_is_public(address) for address in addresses):
        raise EgressRejected("EGRESS_PRIVATE_ADDRESS")
    return EgressTarget(host=host, port=port, addresses=addresses)
