"""Read-only Grafana client: search dashboards, fetch one by uid. Nothing else.

Grafana is a **read-only monitoring source**, the third one alongside
Alertmanager and Thanos, and it is emphatically *not* an outbound target
(ADR 0013). That distinction decides the guard:

- monitoring source — GET only, one thin method per endpoint, bounded response,
  timeout, no redirect following. **A private address is its normal form.**
- outbound target — all of the above *plus* rejecting loopback, private and
  link-local addresses on the resolved IP (`providers/egress.py`).

Wiring the outbound guard into this module would make the capability unusable
the day it shipped, because the user's Grafana is on the internal network. The
only thing borrowed from the outbound side is the enable gate — credential
encrypted and never echoed back, explicit successful test before use — and that
is borrowed because there is a credential here, not because the address is
dangerous.

Grafana is used for two things and never for a third: reading a dashboard
definition into metric templates, and assembling a deep link. **It is not a data
path.** Metrics come from Thanos directly; routing them through Grafana would be
a second fetch path for something already reachable, which is the exact defect
class this repository has removed twice (ADR 0004 / ADR 0013).

The name deliberately differs from the `sources/grafana.py` deleted in 2026-07
(commit `8ea4ac8`): that was a panel/evidence runtime dependency, and none of it
may be restored.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

#: Dashboard JSON is legitimately large — a busy dashboard with 40 panels and
#: inline thresholds runs to a few hundred KB — but an unbounded read is a hole
#: in local memory. 8 MB is far above any real dashboard and far below trouble.
MAX_GRAFANA_RESPONSE_BYTES = 8 * 1024 * 1024

#: The uid is interpolated into the URL path, so it is checked against a
#: whitelist before the request rather than escaped during it. Grafana's own
#: uids are generated from this alphabet.
_UID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_AUTH_REQUIRED_MESSAGE = (
    "这个 Grafana 需要认证。请在 Grafana 里创建一个 Viewer 权限的 "
    "service account token，填在下面的凭证栏里。"
)


class GrafanaError(Exception):
    """A classified failure. Carries a code and a message safe to show a user.

    Every failure gets its own code because each one leads somewhere different:
    a 401 means "make a token", a 404 means "that dashboard is gone", a redirect
    means "your address has the wrong scheme". Collapsing them into one "could
    not load" would leave the reader with no next action (CAP-12.7a).

    Neither the code nor the message ever contains the address or the remote
    body. `str(exc)` on an httpx error carries the full URL, and that must not
    reach an API response (SYSTEM_SPEC §7).
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


def _classify_transport_error(exc: Exception) -> GrafanaError:
    if isinstance(exc, httpx.TimeoutException):
        return GrafanaError("GRAFANA_TIMEOUT", "连接 Grafana 超时，请检查地址与网络。")
    return GrafanaError(
        "GRAFANA_UNREACHABLE", "无法连接到 Grafana，请检查地址、端口与网络。"
    )


def _classify_status(status_code: int) -> GrafanaError | None:
    if 300 <= status_code < 400:
        # Checked before `raise_for_status`, which only covers 4xx/5xx. An
        # ingress redirecting http to https otherwise fails every read
        # identically and surfaces as a vague parse error, hiding the one fix
        # the reader could actually apply.
        return GrafanaError(
            "GRAFANA_REDIRECT",
            "Grafana 返回了重定向，本客户端不跟随。请确认地址使用的协议"
            "（http / https）与实际服务一致。",
        )
    if status_code in (401, 403):
        return GrafanaError("GRAFANA_AUTH_REQUIRED", _AUTH_REQUIRED_MESSAGE)
    if status_code == 404:
        return GrafanaError(
            "GRAFANA_NOT_FOUND",
            "Grafana 上找不到这个对象，它可能已被删除或 uid 已变化。",
        )
    if status_code >= 400:
        return GrafanaError(
            "GRAFANA_HTTP_ERROR", f"Grafana 返回了 HTTP {status_code}。"
        )
    return None


class GrafanaDashboardClient:
    """Two methods. There is no third, and no "pass any path" method.

    A general-purpose request method would make the read-only boundary
    impossible to audit — you could no longer tell by reading this class what
    the backend is allowed to ask for. Adding an endpoint means adding a method
    here, deliberately a visible act (the same rule `ThanosClient` follows).
    """

    def __init__(
        self,
        base_url: str = "",
        token: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout_seconds or 15.0)
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    # -- the two endpoints ------------------------------------------------

    async def search_dashboards(
        self, query: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List dashboards matching a title fragment, so the user can pick one.

        An empty query lists what is there, which doubles as the connectivity
        test: read-only, cheap, and available on every Grafana version.
        """

        payload = await self._get(
            "/api/search",
            {
                "type": "dash-db",
                "query": str(query or ""),
                "limit": max(1, min(int(limit), 500)),
            },
        )
        if not isinstance(payload, list):
            raise GrafanaError(
                "GRAFANA_NOT_JSON",
                "Grafana 返回的内容不是预期的搜索结果，可能这个地址前面还有一层"
                "网关或登录页。",
            )
        return [row for row in payload if isinstance(row, dict)]

    async def get_dashboard(self, uid: str) -> dict[str, Any]:
        """Fetch one dashboard definition, unwrapped from Grafana's envelope.

        The response is `{"dashboard": ..., "meta": ...}`; callers want the
        definition. Unwrapping here spares every parser downstream from knowing
        about the envelope.
        """

        if not _UID_PATTERN.match(str(uid or "")):
            raise GrafanaError(
                "GRAFANA_INVALID_UID",
                "dashboard 标识不合法，只允许字母、数字、下划线和连字符。",
            )
        payload = await self._get(f"/api/dashboards/uid/{uid}", {})
        dashboard = payload.get("dashboard") if isinstance(payload, dict) else None
        if not isinstance(dashboard, dict):
            raise GrafanaError(
                "GRAFANA_NOT_JSON",
                "Grafana 返回的内容里没有 dashboard 定义，可能这个地址前面还有一层"
                "网关或登录页。",
            )
        return dashboard

    # -- the single request path ------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """The only place this module talks to the network. Private on purpose.

        Bounded, timed out, and never following a redirect — the three
        properties that make "read-only" mean something in practice rather than
        just describing the verb.
        """

        if not self.configured:
            raise GrafanaError(
                "GRAFANA_NOT_CONFIGURED", "这个来源还没有配置 Grafana 地址。"
            )
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET", f"{self.base_url}{path}", headers=headers, params=params
                ) as response:
                    failure = _classify_status(response.status_code)
                    if failure is not None:
                        raise failure
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > MAX_GRAFANA_RESPONSE_BYTES:
                        raise _too_large()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_GRAFANA_RESPONSE_BYTES:
                            # Refused entirely rather than truncated: half a
                            # dashboard parses into a plausible candidate list
                            # that is simply missing panels, which is worse than
                            # a failure because nothing looks wrong.
                            raise _too_large()
        except httpx.HTTPError as exc:
            raise _classify_transport_error(exc) from None

        try:
            return json.loads(content)
        except ValueError:
            raise GrafanaError(
                "GRAFANA_NOT_JSON",
                "Grafana 返回的不是 JSON。如果这个地址前面有登录页或网关，"
                "请改用能直接访问 Grafana API 的地址。",
            ) from None


def _too_large() -> GrafanaError:
    return GrafanaError(
        "GRAFANA_RESPONSE_TOO_LARGE",
        "Grafana 返回的内容超过了读取上限（8 MB），已整体拒绝。",
    )
