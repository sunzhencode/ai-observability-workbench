"""Single-origin delivery of the compiled operator interface."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response

_UI_ROUTES = ("incidents", "alerts", "rules", "notifications", "history", "settings")
_RESERVED_PREFIXES = ("api", "assets", "health", "metrics")


def mount_operator_interface(
    app: FastAPI,
    distribution: Path,
    *,
    csrf_token: str,
) -> None:
    """Mount immutable assets and explicit SPA routes after all API routers."""
    root = distribution.expanduser().resolve()
    index = root / "index.html"
    assets = root / "assets"
    if not index.is_file() or not assets.is_dir():
        raise RuntimeError("compiled operator interface is incomplete")
    index_html = index.read_text(encoding="utf-8")
    marker = "</head>"
    if marker not in index_html:
        raise RuntimeError("compiled operator interface has no document head")
    index_html = index_html.replace(
        marker,
        f'<meta name="csrf-token" content="{csrf_token}" />{marker}',
        1,
    )

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def frontend_asset(asset_path: str) -> Response:
        candidate = (assets / asset_path).resolve()
        if assets not in candidate.parents or not candidate.is_file():
            return Response(status_code=404)
        return FileResponse(
            candidate,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    async def frontend_index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    app.add_api_route("/", frontend_index, methods=["GET"], include_in_schema=False)
    for file_name in ("favicon.svg",):
        file_path = root / file_name
        if file_path.is_file():
            async def frontend_file(path: Path = file_path) -> FileResponse:
                return FileResponse(
                    path,
                    headers={"Cache-Control": "public, max-age=3600"},
                )

            app.add_api_route(
                f"/{file_name}",
                frontend_file,
                methods=["GET"],
                include_in_schema=False,
            )
    for route in _UI_ROUTES:
        app.add_api_route(
            f"/{route}", frontend_index, methods=["GET"], include_in_schema=False
        )
        app.add_api_route(
            f"/{route}/{{nested_path:path}}",
            frontend_index,
            methods=["GET"],
            include_in_schema=False,
        )

    @app.get("/{frontend_path:path}", include_in_schema=False)
    async def frontend_fallback(frontend_path: str) -> Response:
        first_segment = frontend_path.split("/", 1)[0]
        if first_segment in _RESERVED_PREFIXES or "." in first_segment:
            return Response(status_code=404)
        return await frontend_index()
