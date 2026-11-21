"""``relay`` command line (plan V2-01).

  relay serve  [--config relay.toml] [--host H] [--port P]
  relay doctor [--config relay.toml]
  relay ingest                         (stub until V2-03)
  relay ui build [--web-dir web]       build the web UI (npm run build) into relay/app/static
  relay keys create --tenant T [--role admin|member] [--name N] [--daily-limit N]
  relay keys list --tenant T           key ids, names, roles, hash prefixes (never the key)
  relay keys revoke --id K
  relay tenants create --name N | relay tenants list

Settings: ``relay.toml`` if present, else built-in defaults (see ``relay.example.toml``),
then ``RELAY_<SECTION>__<KEY>`` environment overrides. Secrets (``GROQ_API_KEY``,
``MAX_REAL_CALLS``) come only from the environment or ``.env`` and are never printed.

``doctor`` applies the same rules as ``serve`` (it calls the composition root's helpers),
so a configuration that passes ``doctor`` does not fail ``serve`` for a settings reason.

Exit codes: 0 ok; 1 a check failed or the service could not start; 2 usage error or a
command that is not available yet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from relay.app import compose
from relay.app.routes.ui import STATIC_DIR
from relay.app.settings import (
    DEFAULT_CONFIG_FILE,
    RelaySettings,
    load_relay_settings,
    with_overrides,
)
from relay.config import Settings, load_settings
from relay.errors import ConfigurationError, RelayError, sanitize_error
from relay.ingestion.arxiv_fetcher import MANIFEST_FILE, load_corpus_manifest
from relay.retrieval.hybrid_index import INDEX_MANIFEST, RETRIEVAL_SETTINGS
from relay.store import open_store

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_UNAVAILABLE: Final = 2
DEFAULT_WEB_DIR: Final = Path(__file__).resolve().parents[2] / "web"  # the repository's web/
Out = Callable[[str], None]
Level = Literal["ok", "info", "warn", "fail"]


def _load(config: Path | None, env_file: Path | None) -> tuple[RelaySettings, Settings, str]:
    settings = load_relay_settings(config)
    env = load_settings(env_file)
    used = config or (DEFAULT_CONFIG_FILE if DEFAULT_CONFIG_FILE.is_file() else None)
    source = str(used) if used else "built-in defaults (no relay.toml found)"
    return settings, env, source


# --- doctor ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    level: Level
    name: str
    detail: str


def _corpus_check(settings: RelaySettings) -> Check:
    path = settings.paths.papers_dir / MANIFEST_FILE
    if not path.is_file():
        return Check("fail", "corpus manifest", f"{path} not found; run scripts/ingest_papers.py")
    try:
        papers = load_corpus_manifest(settings.paths.papers_dir).papers
    except RelayError as exc:
        return Check("fail", "corpus manifest", f"{path} is not a valid manifest "
                                                f"({type(exc).__name__})")
    return Check("ok" if papers else "fail", "corpus manifest", f"{path} ({len(papers)} papers)")


def _index_check(settings: RelaySettings) -> Check:
    index_dir = settings.paths.index_dir
    if not index_dir.is_dir():
        return Check("fail", "index directory",
                     f"{index_dir} not found; run scripts/build_indexes.py")
    presets = sorted({RETRIEVAL_SETTINGS[s].chunk_preset for s in settings.retrieval.settings})
    missing = [p for p in presets if not (index_dir / p / INDEX_MANIFEST).is_file()]
    detail = f"{index_dir} ({len(presets) - len(missing)}/{len(presets)} presets"
    detail += f"; missing {missing}; run scripts/build_indexes.py)" if missing else ")"
    return Check("fail" if missing else "ok", "index directory", detail)


def _models_check(settings: RelaySettings) -> Check:
    models = settings.paths.models_dir
    cached = models.is_dir() and any(p.name.startswith("models--") for p in models.iterdir())
    hint = "" if cached else " has no cached models; run scripts/build_indexes.py " \
                             "--download-models"
    return Check("ok" if cached else "fail", "embedding models", f"{models}{hint}")


def _key_checks(settings: RelaySettings, env: Settings) -> list[Check]:
    key_set = env.groq_api_key is not None and bool(env.groq_api_key.get_secret_value().strip())
    if not any(lane.kind == "groq" for lane in settings.lanes):
        return [Check("info", "GROQ_API_KEY", ("set" if key_set else "not set")
                      + " (not needed: no groq lane is configured)")]
    return [
        Check("ok" if key_set else "fail", "GROQ_API_KEY",
              "set" if key_set else "not set; a groq lane is configured"),
        Check("ok" if env.max_real_calls >= 1 else "fail", "MAX_REAL_CALLS",
              f"{env.max_real_calls} (hard cap on real calls per process)"),
    ]


def _lane_check(lane_index: int, settings: RelaySettings) -> Check:
    lane = settings.lanes[lane_index]
    name = f"lane {lane.name}"
    if lane.kind != "standin" or not lane.embedded:
        return Check("info", name, f"{lane.kind}, model {lane.model}"
                     + (f", {lane.base_url}" if lane.base_url else ""))
    try:
        profile = compose.standin_profile(lane)
    except ConfigurationError as exc:
        return Check("fail", name, str(exc))
    level: Level = "ok" if profile.label == "calibrated" else "warn"
    run = profile.calibration_run_id or "no calibration run"
    return Check(level, name,
                 f"embedded stand-in, model {lane.model}, profile {profile.label} ({run})")


async def _probe_store(path: Path) -> tuple[int, str, bool]:
    """(schema version, journal mode, whether this check created a new store file)."""
    created = str(path) != ":memory:" and not path.exists()
    async with open_store(path) as store:
        return store.schema_version, store.journal_mode, created


def _store_check(settings: RelaySettings) -> Check:
    """In sqlite mode, open the store and apply migrations (creates the file if missing).

    A newly created store is a WARN, so a mistyped ``paths.store_path`` is visible instead
    of passing quietly with an empty database.
    """
    if settings.cache.mode != "sqlite":
        return Check("info", "store", f"not used (cache mode {settings.cache.mode})")
    path = settings.paths.store_path
    try:
        version, journal, created = asyncio.run(_probe_store(path))
    except RelayError as exc:  # StoreOpenError, MigrationError
        return Check("fail", "store", str(exc))
    detail = (f"{path} opened, migrations applied (schema v{version}, journal {journal})")
    if created:
        return Check("warn", "store", f"{detail}; created a new, empty store: check "
                                      "paths.store_path if you expected existing data")
    return Check("ok", "store", detail)


def _rule_check(name: str, rule: Callable[[], str]) -> Check:
    try:
        return Check("ok", name, rule())
    except ConfigurationError as exc:
        return Check("fail", name, str(exc))


def run_checks(settings: RelaySettings, env: Settings) -> list[Check]:
    """Files and settings only: fast, offline, and nothing secret is reported."""
    checks = [_corpus_check(settings), _index_check(settings), _models_check(settings),
              *_key_checks(settings, env)]
    checks += [_lane_check(i, settings) for i in range(len(settings.lanes))]

    def cache_detail() -> str:
        compose.cache_mode(settings)  # raises for an unknown mode
        mode = settings.cache.mode
        if mode == "sqlite":
            return "sqlite (answers cached in memory; request log in the store)"
        if mode == "memory":
            return "memory (answers cached in memory)"
        return "off (no answer cache)"

    checks.append(_rule_check("cache mode", cache_detail))

    def capacity() -> str:
        derived = compose.capacity_derivation(settings)
        return (f"{derived.capacity} (bound {derived.binding_bound}, "
                f"{derived.service_rate_per_s * 60:.1f}/min)")

    checks.append(_rule_check("admission capacity", capacity))
    checks.append(_store_check(settings))
    return checks


def doctor(settings: RelaySettings, env: Settings, source: str, out: Out = print) -> int:
    out(f"config: {source}")
    checks = run_checks(settings, env)
    for check in checks:
        out(f"[{check.level.upper():>4}] {check.name}: {check.detail}")
    failed = sum(c.level == "fail" for c in checks)
    out("doctor: all required checks passed" if not failed
        else f"doctor: {failed} check(s) failed")
    return EXIT_FAILED if failed else EXIT_OK


# --- serve -----------------------------------------------------------------------------


def _configure_logging() -> None:
    """Send the service's own ``relay.*`` log records to the console (uvicorn only
    configures its own loggers), so warnings and the shutdown summary are visible."""
    root = logging.getLogger("relay")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _install_break_handler(server: Any) -> Any:
    """On Windows, make Ctrl+Break stop the server gracefully, like Ctrl+C.

    uvicorn handles SIGINT and SIGTERM; Ctrl+Break (SIGBREAK) would otherwise kill the
    process, skipping the shutdown that flushes the request log and closes the store.
    Returns the previous handler (``None`` where SIGBREAK does not exist).
    """
    if not hasattr(signal, "SIGBREAK"):
        return None

    def stop(signum: int, frame: object) -> None:
        server.should_exit = True

    return signal.signal(signal.SIGBREAK, stop)


async def serve(settings: RelaySettings, env: Settings, source: str, out: Out = print) -> int:
    import uvicorn

    _configure_logging()
    out(f"config: {source}")
    out("loading indexes and models ...")
    async with compose.open_runtime(settings, env) as runtime:
        for line in runtime.describe():
            out(line)
        host, port = settings.server.host, settings.server.port
        out(f"serving the API and the web UI on http://{host}:{port}  (Ctrl+C to stop)")
        server = uvicorn.Server(uvicorn.Config(runtime.app, host=host, port=port,
                                               log_level="info", lifespan="off"))
        previous = _install_break_handler(server)
        try:
            await server.serve()
        finally:
            if previous is not None:
                signal.signal(signal.SIGBREAK, previous)
    return EXIT_OK


# --- ingest ----------------------------------------------------------------------------


def ingest(out: Out = print) -> int:
    out("relay ingest is not available yet: collections and ingestion arrive in V2-03.")
    out("The v1 corpus can be rebuilt with scripts/ingest_papers.py and "
        "scripts/build_indexes.py.")
    return EXIT_UNAVAILABLE


# --- ui build --------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def _remove(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)  # raises: a half-removed folder is reported, not hidden


def _swap_in(dist: Path, static_dir: Path) -> None:
    """Copy ``dist`` to ``static_dir`` so that any failure leaves the old files in place:
    copy to ``.new``, move the old folder to ``.old``, move ``.new`` in, then drop ``.old``.
    Raises OSError after undoing what it can."""
    staging = static_dir.with_name(static_dir.name + ".new")
    previous = static_dir.with_name(static_dir.name + ".old")
    _remove(staging)
    _remove(previous)
    shutil.copytree(dist, staging)
    had_previous = static_dir.exists()
    try:
        if had_previous:
            static_dir.rename(previous)
        try:
            staging.rename(static_dir)
        except OSError:
            if had_previous:
                previous.rename(static_dir)  # put the old build back
            raise
    finally:
        _remove(staging)
    _remove(previous)


def ui_build(web_dir: Path, static_dir: Path = STATIC_DIR, *, run: Runner = subprocess.run,
             which: Callable[[str], str | None] = shutil.which, out: Out = print) -> int:
    """``npm run build`` in ``web_dir``, then replace ``static_dir`` with its ``dist/``.

    The previous static files are kept unless the build succeeded and produced an
    ``index.html``. npm is looked up on PATH (``npm.cmd`` on Windows)."""
    if not (web_dir / "package.json").is_file():
        out(f"relay ui build: no package.json in {web_dir} (pass --web-dir)")
        return EXIT_FAILED
    if not (web_dir / "node_modules").is_dir():
        out(f"relay ui build: dependencies are not installed; run `npm ci` in {web_dir}")
        return EXIT_FAILED
    npm = which("npm")
    if npm is None:
        out("relay ui build: npm was not found on PATH (install Node.js 20 or newer)")
        return EXIT_FAILED
    out(f"building the web UI in {web_dir} ...")
    try:
        result = run([npm, "run", "build"], cwd=web_dir, check=False)
    except OSError as exc:
        out(f"relay ui build: npm could not be started: {sanitize_error(exc)}")
        return EXIT_FAILED
    if result.returncode != 0:
        out(f"relay ui build: npm run build failed (exit {result.returncode}); "
            "the previous UI is unchanged")
        return EXIT_FAILED
    dist = web_dir / "dist"
    if not (dist / "index.html").is_file():
        out(f"relay ui build: the build wrote no {dist / 'index.html'}; nothing was copied")
        return EXIT_FAILED
    try:
        _swap_in(dist, static_dir)
    except OSError as exc:
        out(f"relay ui build: the new UI could not be installed in {static_dir}: "
            f"{sanitize_error(exc)}; the previous UI is unchanged")
        return EXIT_FAILED
    files = sum(1 for path in static_dir.rglob("*") if path.is_file())
    out(f"copied {files} files to {static_dir}; `relay serve` serves the UI at /")
    return EXIT_OK


# --- keys and tenants (V2-10) ----------------------------------------------------------


async def keys_command(args: argparse.Namespace, settings: RelaySettings,
                       out: Out = print) -> int:
    """API keys in the store at ``paths.store_path``. A raw key is printed once, at
    creation; only its SHA-256 is stored, so it can never be shown again."""
    from relay.app.bootstrap import ensure_dev_tenant
    from relay.store.repositories import ForeignKeyViolationError, InvalidValueError

    async with open_store(settings.paths.store_path) as store:
        await ensure_dev_tenant(store)
        if args.keys_command == "create":
            limit = None if args.daily_limit == 0 else args.daily_limit
            try:
                raw, record = await store.api_keys.create(args.tenant, args.role, limit,
                                                          name=args.name)
            except ForeignKeyViolationError:
                out(f"relay keys create: no tenant {args.tenant!r} (relay tenants list)")
                return EXIT_FAILED
            except InvalidValueError as exc:
                out(f"relay keys create: {exc}")
                return EXIT_FAILED
            out(raw)
            out(f"key id {record.id}, tenant {record.tenant_id}, role {record.role}, daily "
                f"limit {record.daily_limit or 'none'}")
            out("Store this key now: it is shown only once and cannot be recovered.")
            return EXIT_OK
        if args.keys_command == "list":
            records = await store.api_keys.list_for_tenant(args.tenant)
            if not records:
                out(f"no keys for tenant {args.tenant!r}")
            for r in records:
                state = "revoked" if r.revoked else "active"
                out(f"{r.id}  {r.name or '-':<16}  {r.role:<6}  hash {r.key_hash[:12]}…  "
                    f"{state:<7}  created {r.created_at or '-'}")
            return EXIT_OK
        if await store.api_keys.revoke(args.id):
            out(f"key {args.id} revoked; requests with it now get HTTP 401")
            return EXIT_OK
        known = await store.api_keys.get_by_id(args.id)
        out(f"relay keys revoke: key {args.id} " + ("was already revoked" if known
                                                    else "does not exist"))
        return EXIT_FAILED if known is None else EXIT_OK


async def tenants_command(args: argparse.Namespace, settings: RelaySettings,
                          out: Out = print) -> int:
    from relay.app.bootstrap import ensure_dev_tenant

    async with open_store(settings.paths.store_path) as store:
        await ensure_dev_tenant(store)
        if args.tenants_command == "create":
            if not args.name.strip():
                out("relay tenants create: the name is blank")
                return EXIT_FAILED
            tenant = await store.tenants.create(args.name.strip())
            out(f"tenant {tenant.id} ({tenant.name}); make a key: relay keys create "
                f"--tenant {tenant.id}")
            return EXIT_OK
        for tenant in await store.tenants.list_all():
            out(f"{tenant.id}  {tenant.name}  created {tenant.created_at}")
        return EXIT_OK


# --- entry point -----------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="relay", description="The Relay service.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("serve", "start the service"),
                       ("doctor", "check settings, files and keys (never prints secrets)"),
                       ("ingest", "ingest documents (available from V2-03)")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--config", type=Path, default=None,
                             help="settings file (default: relay.toml if present)")
        command.add_argument("--env-file", type=Path, default=Path(".env"),
                             help="environment file for secrets (default: .env)")
        if name == "serve":
            command.add_argument("--host", default=None)
            command.add_argument("--port", type=int, default=None)
    ui = sub.add_parser("ui", help="the web UI")
    ui_sub = ui.add_subparsers(dest="ui_command", required=True)
    build = ui_sub.add_parser("build", help="npm run build in web/, copied to the API's static")
    build.add_argument("--web-dir", type=Path, default=DEFAULT_WEB_DIR,
                       help="the web project (default: the repository's web/)")
    keys = sub.add_parser("keys", help="API keys (stored hashed; shown once)")
    keys.add_argument("--config", type=Path, default=None)
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    create = keys_sub.add_parser("create", help="make a key; prints it ONCE")
    create.add_argument("--tenant", required=True)
    create.add_argument("--role", choices=("admin", "member"), default="member")
    create.add_argument("--name", default=None)
    create.add_argument("--daily-limit", type=int, default=1000,
                        help="/v1/ask requests per UTC day (0: no limit; default 1000)")
    listing = keys_sub.add_parser("list", help="a tenant's keys (never the keys themselves)")
    listing.add_argument("--tenant", required=True)
    revoke = keys_sub.add_parser("revoke", help="revoke a key (its requests get 401)")
    revoke.add_argument("--id", required=True)
    tenants = sub.add_parser("tenants", help="tenants")
    tenants.add_argument("--config", type=Path, default=None)
    tenants_sub = tenants.add_subparsers(dest="tenants_command", required=True)
    tenants_sub.add_parser("create", help="add a tenant").add_argument("--name", required=True)
    tenants_sub.add_parser("list", help="list the tenants")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "ingest":
        return ingest()
    if args.command == "ui":
        return ui_build(args.web_dir)
    if args.command in ("keys", "tenants"):
        try:
            settings = load_relay_settings(args.config)
            run = keys_command if args.command == "keys" else tenants_command
            return asyncio.run(run(args, settings))
        except (ConfigurationError, RelayError) as exc:
            print(f"relay {args.command}: {sanitize_error(exc)}", file=sys.stderr)
            return EXIT_FAILED
    try:
        settings, env, source = _load(args.config, args.env_file)
        if args.command == "doctor":
            return doctor(settings, env, source)
        server = {k: v for k, v in (("host", args.host), ("port", args.port)) if v is not None}
        if server:
            settings = with_overrides(settings, {"server": server})
        return asyncio.run(serve(settings, env, source))
    except ConfigurationError as exc:
        print(f"relay {args.command}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except RelayError as exc:
        print(f"relay {args.command} failed: {sanitize_error(exc)}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
