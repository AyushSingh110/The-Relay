"""The web UI's static files (plan V2-06; docs/ARCHITECTURE_V2.md D10).

``web/`` is built by ``relay ui build`` into ``STATIC_DIR``. The API serves it on the same
origin, so the UI needs no CORS:

    GET /                         index.html
    GET /status, /eval,           index.html (the UI's own routes; it routes in the browser)
        /collections/...
    GET /static/...               the built assets (Vite ``base`` is ``/static/``)

Only these paths are claimed: ``/v1``, ``/ask`` and ``/healthz`` stay the API's, and an
unknown path is still a 404 rather than the UI. ``index.html`` is sent with
``cache-control: no-cache`` so a new build is picked up; the assets carry content hashes.
When the UI has not been built, the UI paths answer 503 with the command that builds it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

STATIC_DIR: Final = Path(__file__).resolve().parent.parent / "static"
UI_PATHS: Final = ("/", "/status", "/eval", "/collections/{rest:path}")
NOT_BUILT: Final = (
    "<!doctype html><html lang=en><head><meta charset=utf-8><title>The Relay</title></head>"
    "<body><h1>The web UI is not built</h1>"
    "<p>Build it with <code>relay ui build</code> (needs Node.js and "
    "<code>npm ci</code> in <code>web/</code>), then restart <code>relay serve</code>.</p>"
    "<p>The API is running: <a href=/healthz>/healthz</a>.</p></body></html>")


class _BuiltFiles(StaticFiles):
    """StaticFiles that answers 404 (not a 500) while the UI has not been built yet."""

    async def check_config(self) -> None:
        if self.directory is not None and Path(self.directory).is_dir():
            await super().check_config()


def mount_ui(app: FastAPI, static_dir: Path = STATIC_DIR) -> None:
    """Serve the built UI from ``static_dir`` (checked per request, so a build made while
    the server runs is served without a restart)."""
    index = static_dir / "index.html"

    async def page() -> Response:
        if not index.is_file():
            return HTMLResponse(NOT_BUILT, status_code=503)
        return FileResponse(index, media_type="text/html",
                            headers={"cache-control": "no-cache"})

    for path in UI_PATHS:
        app.add_api_route(path, page, methods=["GET"], include_in_schema=False)
    app.mount("/static", _BuiltFiles(directory=static_dir, check_dir=False), name="static")


__all__ = ["STATIC_DIR", "mount_ui"]
