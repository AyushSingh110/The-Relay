"""V2-06 backend support for the web UI: document counts on the collection list, static
serving of the built UI, and ``relay ui build``. Offline: no npm, no network."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from relay import cli
from relay.app import compose
from relay.app.routes.ui import mount_ui
from tests.test_compose_collections import env, settings_for
from tests.test_index_registry import FakeEmbedder
from tests.test_ingestion_routes import AUTH, Api, api, pdf  # noqa: F401  (fixture)
from tests.test_pipeline_and_verifier import PASSAGES, StubRetriever

INDEX = "<!doctype html><html><body><div id=root></div></body></html>"


# --- document counts -----------------------------------------------------------------
async def test_the_collection_list_counts_each_collections_documents(api: Api) -> None:  # noqa: F811
    empty = await api.collection("empty")
    full = await api.collection("full")
    for page in ("Rocks one", "Rocks two"):
        assert (await api.upload(full, pdf(api.tmp, page))).status_code == 202
    listed = (await api.client.get("/v1/collections", headers=AUTH)).json()["collections"]
    counts = {c["id"]: c["document_count"] for c in listed}
    assert counts == {empty: 0, full: 2}


# --- static serving --------------------------------------------------------------------
def built(tmp: Path) -> Path:
    static = tmp / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX, encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return static


def client_for(static: Path) -> httpx.AsyncClient:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/collections")
    async def collections() -> dict[str, list[str]]:
        return {"collections": []}

    mount_ui(app, static)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_built_ui_is_served_at_root_and_at_every_app_route(tmp_path: Path) -> None:
    async with client_for(built(tmp_path)) as client:
        for path in ("/", "/status", "/eval", "/collections/abc/chat",
                     "/collections/abc/upload"):
            reply = await client.get(path)
            assert reply.status_code == 200, path
            assert reply.text == INDEX and reply.headers["content-type"].startswith("text/html")
            assert reply.headers["cache-control"] == "no-cache"  # a new build is picked up
        asset = await client.get("/static/assets/app.js")
        assert asset.status_code == 200 and asset.text == "console.log(1)"


def test_every_top_level_page_of_the_ui_router_is_served() -> None:
    """A page added to web/src/router.tsx must also be served by the API, or opening or
    reloading its URL gives a JSON 404 (found for /eval in the v2 release check)."""
    import re

    from relay.app.routes.ui import UI_PATHS

    router = Path(__file__).resolve().parent.parent / "web" / "src" / "router.tsx"
    pages = set(re.findall(r'trimmed === "(/[^"]*)"', router.read_text(encoding="utf-8")))
    assert pages, "no page paths found in router.tsx; update this test's pattern"
    assert pages <= set(UI_PATHS), sorted(pages - set(UI_PATHS))


async def test_the_ui_does_not_shadow_the_api(tmp_path: Path) -> None:
    async with client_for(built(tmp_path)) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/v1/collections")).json() == {"collections": []}
        assert (await client.get("/v1/nothing")).status_code == 404  # not index.html
        assert (await client.get("/static/missing.js")).status_code == 404


async def raw_get(app: FastAPI, path: str) -> tuple[int, bytes]:
    """GET ``path`` exactly as given (httpx would normalise ".." before sending it)."""
    sent: list[Any] = []

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        sent.append(message)

    await app({"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
               "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80),
               "root_path": "", "http_version": "1.1"}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


async def test_static_files_cannot_escape_the_build_directory(tmp_path: Path) -> None:
    static = built(tmp_path)
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    app = FastAPI()
    mount_ui(app, static)
    for raw in ("/static/../secret.txt", "/static/%2e%2e/secret.txt",
                "/static/assets/../../secret.txt"):
        status, body = await raw_get(app, raw)
        assert status == 404 and b"outside" not in body, raw


async def test_an_unbuilt_ui_says_how_to_build_it(tmp_path: Path) -> None:
    async with client_for(tmp_path / "not-built") as client:
        reply = await client.get("/")
        assert reply.status_code == 503
        assert "relay ui build" in reply.text
        assert (await client.get("/static/assets/app.js")).status_code == 404  # not a 500
        assert (await client.get("/healthz")).status_code == 200  # the API still works


async def test_relay_serve_mounts_the_ui(tmp_path: Path) -> None:
    async with compose.open_runtime(
            settings_for(tmp_path, index_dir=tmp_path / "none"), env(),
            retrievers={"small_fast": StubRetriever(PASSAGES)},
            collection_embedder=FakeEmbedder()) as runtime:
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code in (200, 503)  # mounted, not 404


# --- relay ui build -------------------------------------------------------------------
class FakeNpm:
    """Stands in for ``subprocess.run``: records the command, writes a ``dist/``."""

    def __init__(self, returncode: int = 0, write: bool = True) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.returncode = returncode
        self.write = write

    def __call__(self, command: Sequence[str], *, cwd: Path, **kwargs: Any
                 ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), cwd))
        if self.write:
            (cwd / "dist" / "assets").mkdir(parents=True, exist_ok=True)
            (cwd / "dist" / "index.html").write_text(INDEX, encoding="utf-8")
            (cwd / "dist" / "assets" / "app.js").write_text("new", encoding="utf-8")
        return subprocess.CompletedProcess(list(command), self.returncode)


def NPM(name: str) -> str:  # noqa: N802  (stands in for shutil.which)
    return name


def web_project(tmp: Path) -> Path:
    web = tmp / "web"
    (web / "node_modules").mkdir(parents=True)
    (web / "package.json").write_text("{}", encoding="utf-8")
    return web


def test_ui_build_runs_npm_and_replaces_the_static_files(tmp_path: Path) -> None:
    web, static = web_project(tmp_path), tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "assets" / "stale.js").write_text("old", encoding="utf-8")
    npm, lines = FakeNpm(), []
    code = cli.ui_build(web, static, run=npm, which=NPM, out=lines.append)
    assert code == cli.EXIT_OK, lines
    assert npm.calls == [(["npm", "run", "build"], web)]
    assert (static / "index.html").read_text(encoding="utf-8") == INDEX
    assert (static / "assets" / "app.js").read_text(encoding="utf-8") == "new"
    assert not (static / "assets" / "stale.js").exists()  # an old build leaves nothing


def test_a_failed_build_keeps_the_previous_static_files(tmp_path: Path) -> None:
    web, static = web_project(tmp_path), tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("previous", encoding="utf-8")
    lines: list[str] = []
    code = cli.ui_build(web, static, run=FakeNpm(returncode=1), which=NPM, out=lines.append)
    assert code == cli.EXIT_FAILED and "npm run build failed" in "\n".join(lines)
    assert (static / "index.html").read_text(encoding="utf-8") == "previous"


def test_a_build_without_index_html_is_refused(tmp_path: Path) -> None:
    web, static = web_project(tmp_path), tmp_path / "static"
    lines: list[str] = []
    code = cli.ui_build(web, static, run=FakeNpm(write=False), which=NPM, out=lines.append)
    assert code == cli.EXIT_FAILED and "index.html" in "\n".join(lines)
    assert not static.exists()


def test_a_failed_swap_keeps_the_previous_static_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web, static = web_project(tmp_path), tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("previous", encoding="utf-8")
    real_rename = Path.rename

    def rename(self: Path, target: Any) -> Path:
        if self.name == "static.new":  # the new build cannot be moved into place
            raise PermissionError("locked")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    lines: list[str] = []
    code = cli.ui_build(web, static, run=FakeNpm(), which=NPM, out=lines.append)
    assert code == cli.EXIT_FAILED and "could not be installed" in " ".join(lines)
    assert (static / "index.html").read_text(encoding="utf-8") == "previous"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["static", "web"]  # no leftovers


@pytest.mark.parametrize(("setup", "message"), [
    ("no-web", "package.json"), ("no-modules", "npm ci"), ("no-npm", "npm was not found")])
def test_ui_build_explains_what_is_missing(tmp_path: Path, setup: str, message: str) -> None:
    web = web_project(tmp_path)
    if setup == "no-web":
        web = tmp_path / "elsewhere"
    if setup == "no-modules":
        (web / "node_modules").rmdir()
    npm, lines = FakeNpm(), []
    code = cli.ui_build(web, tmp_path / "static", run=npm,
                        which=(lambda _: None) if setup == "no-npm" else NPM, out=lines.append)
    assert code == cli.EXIT_FAILED and message in "\n".join(lines)
    assert npm.calls == []


def test_relay_ui_build_is_a_cli_command(tmp_path: Path) -> None:
    args = cli.parse_args(["ui", "build", "--web-dir", str(tmp_path)])
    assert (args.command, args.ui_command, args.web_dir) == ("ui", "build", tmp_path)
    assert cli.parse_args(["ui", "build"]).web_dir == cli.DEFAULT_WEB_DIR


# --- sources carry what the source panel shows -----------------------------------------
def test_sources_carry_the_papers_short_name_and_the_whole_passage() -> None:
    from relay.api.routes.ask_v1 import EXCERPT_CHARS, sources_payload
    from tests.test_pipeline_and_verifier import PASSAGES as RETRIEVED

    [first, *_] = sources_payload(tuple(RETRIEVED))["passages"]
    passage = RETRIEVED[0]
    assert first["short_name"] == passage.short_name
    assert first["text"] == passage.text  # the panel highlights within the whole passage
    assert first["text_excerpt"] == passage.text[:EXCERPT_CHARS]  # unchanged for V2-05


async def test_documents_are_counted_per_collection_in_one_query(api: Api) -> None:  # noqa: F811
    a, b, c = [await api.collection(n) for n in ("a", "b", "c")]
    for page in ("one", "two"):
        await api.upload(a, pdf(api.tmp, page))
    await api.upload(b, pdf(api.tmp, "three"))
    counts = await api.store.documents.counts_by_collection([a, b, c])
    assert counts == {a: 2, b: 1}  # a collection without documents is absent


def test_every_tag_in_a_multi_tag_bracket_is_a_citation() -> None:
    """V2-06 review: buffered and cached answers derive citations with ``tags_in``; the
    verifier accepts "[P1, P2]", so both tags must become citations (the UI shows chips)."""
    from relay.api.routes.ask_v1 import tags_in

    assert tags_in("Both hold [P1, P2]. Again [P2; P3] and [P1].") == ["P1", "P2", "P3"]
    assert tags_in("Spaced [ P 4 , P5 ].") == ["P4", "P5"]
