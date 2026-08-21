# app.py — the HTTP shell that health_store.py deliberately leaves out.
#
# One process, one data directory, four device routes plus the MCP endpoint:
#
#   GET  /health          unauthenticated liveness probe
#   POST /api/health      ingest — CollarWatch payload *or* Health Auto Export
#                         REST payload (normalize_payload handles both shapes)
#   GET  /command         CollarWatch picks up a pending measurement command
#   POST /command/result  CollarWatch reports the measurement back
#   GET  /debug           ingest diagnostics (see below)
#   /mcp                  MCP streamable-http: health_now / health_detail /
#                         measure_heart_rate
#
# Two separate credentials, because two very different callers:
#
#   INGEST_TOKEN  devices (watch / Health Auto Export). Sent as X-Health-Token,
#                 which is what the watch app hard-codes; Authorization: Bearer
#                 and ?token= are accepted too, since not every exporter lets
#                 you set an arbitrary header.
#   MCP_TOKEN     the agent side. Authorization: Bearer, or X-Token — both are
#                 accepted on purpose so a client that guessed the other one
#                 still works instead of failing as an opaque "not logged in".
#
# Both fail closed: with no token configured every authenticated route returns
# 401. Set COLLAR_ALLOW_NO_AUTH=1 to run without auth locally.

from __future__ import annotations

import importlib.util
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))

import health_store as hs  # noqa: E402


def _load_tool_module():
    """Load mcp_server/server.py under an explicit name.

    It cannot simply be imported: this file already lives in a directory called
    `server`, so a plain `import server` is ambiguous (the sibling directory is
    a namespace package). Loading by path sidesteps the collision entirely.
    """
    path = _ROOT / "mcp_server" / "server.py"
    spec = importlib.util.spec_from_file_location("collar_mcp_tools", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load MCP tool module at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_tools = _load_tool_module()

INGEST_TOKEN = os.environ.get("HEALTH_INGEST_TOKEN", "").strip()
MCP_TOKEN = os.environ.get("COLLAR_MCP_TOKEN", "").strip()
ALLOW_NO_AUTH = os.environ.get("COLLAR_ALLOW_NO_AUTH", "").strip() == "1"


# ------------------------------------------------------------
# Metric name aliases
#
# Health Auto Export does not use exactly the same metric names as the watch
# app, so a few samples would otherwise be dropped silently by the allow-list.
# `active_energy` vs `active_energy_burned` is the one that bites in practice.
# Extend without a code change via HEALTH_TYPE_ALIASES="from=to,from=to".
# ------------------------------------------------------------

_DEFAULT_ALIASES: dict[str, str] = {
    "active_energy": "active_energy_burned",
    "heart_rate_variability_sdnn": "heart_rate_variability",
    "oxygen_saturation": "blood_oxygen_saturation",
    "sleep_stage": "sleep_analysis",
    "walking_running_distance_km": "walking_running_distance",
}


def _build_aliases() -> dict[str, str]:
    aliases = dict(_DEFAULT_ALIASES)
    raw = os.environ.get("HEALTH_TYPE_ALIASES", "").strip()
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        src, _, dst = pair.partition("=")
        src, dst = src.strip().lower(), dst.strip().lower()
        if src and dst:
            aliases[src] = dst
    return aliases


TYPE_ALIASES = _build_aliases()


def _apply_aliases(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for s in samples:
        mapped = TYPE_ALIASES.get(s.get("type"))
        if mapped:
            s["type"] = mapped
    return samples


# ------------------------------------------------------------
# Diagnostics
#
# The point of /debug is calibration against real exporter output: which metric
# names actually arrived, which ones the allow-list threw away, and what units
# they carried. Guessing at an exporter's naming from documentation is how you
# end up silently missing half the metrics.
# ------------------------------------------------------------

_STATS: dict[str, Any] = {
    "last_ingest_at": None,
    "last_ingest_source": None,
    "ingest_requests": 0,
    "samples_stored": 0,
    "samples_deduped": 0,
    "accepted_types": Counter(),
    "dropped_types": Counter(),
    "units_seen": {},
    "last_error": None,
}
_MAX_TRACKED_TYPES = 100


def _note_types(samples: list[dict[str, Any]], allowed: set[str]) -> None:
    for s in samples:
        stype = s.get("type") or "?"
        bucket = "accepted_types" if stype in allowed else "dropped_types"
        counter: Counter = _STATS[bucket]
        if stype in counter or len(counter) < _MAX_TRACKED_TYPES:
            counter[stype] += 1
        unit = s.get("unit")
        if unit and len(_STATS["units_seen"]) < _MAX_TRACKED_TYPES:
            _STATS["units_seen"].setdefault(stype, unit)


# ------------------------------------------------------------
# Auth
# ------------------------------------------------------------

def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _device_authed(request: Request) -> bool:
    if ALLOW_NO_AUTH:
        return True
    if not INGEST_TOKEN:
        return False
    presented = (request.headers.get("x-health-token", "").strip()
                 or _bearer(request)
                 or request.query_params.get("token", "").strip())
    return bool(presented) and presented == INGEST_TOKEN


def _unauthorized() -> JSONResponse:
    detail = ("server has no HEALTH_INGEST_TOKEN configured"
              if not INGEST_TOKEN and not ALLOW_NO_AUTH else "bad token")
    return JSONResponse({"error": "unauthorized", "detail": detail}, status_code=401)


class MCPAuthMiddleware:
    """Gate the MCP mount. Pure ASGI so it can sit in front of the mounted app
    without Starlette re-reading the request body."""

    def __init__(self, app, prefix: str = "/mcp"):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        if not self._authed(scope):
            await JSONResponse(
                {"error": "unauthorized",
                 "detail": ("server has no COLLAR_MCP_TOKEN configured"
                            if not MCP_TOKEN else "bad token")},
                status_code=401,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _authed(scope) -> bool:
        if ALLOW_NO_AUTH:
            return True
        if not MCP_TOKEN:
            return False
        presented = ""
        for raw_key, raw_val in scope.get("headers") or []:
            key = raw_key.decode("latin-1").lower()
            val = raw_val.decode("latin-1").strip()
            if key == "authorization" and val.lower().startswith("bearer "):
                presented = val[7:].strip()
                break
            if key == "x-token" and val:
                presented = val
        return bool(presented) and presented == MCP_TOKEN


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def liveness(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def ingest(request: Request) -> JSONResponse:
    if not _device_authed(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    try:
        samples = _apply_aliases(hs.normalize_payload(body))
        allowed = set(hs.ALLOWED_TYPES)
        _note_types(samples, allowed)
        keep = [s for s in samples if s["type"] in allowed]
        stored, deduped = await run_in_threadpool(hs.store_samples, keep)
    except Exception as exc:  # keep the device's retry loop informed, not crashed
        _STATS["last_error"] = f"{type(exc).__name__}: {exc}"
        return JSONResponse({"error": "store failed"}, status_code=500)

    _STATS["ingest_requests"] += 1
    _STATS["samples_stored"] += stored
    _STATS["samples_deduped"] += deduped
    _STATS["last_ingest_at"] = _now_iso()
    if isinstance(body, dict) and body.get("source"):
        _STATS["last_ingest_source"] = str(body["source"])[:40]
    elif samples:
        _STATS["last_ingest_source"] = samples[0].get("source")

    # The watch commits its HealthKit anchor on any 2xx, so the response only
    # needs to confirm receipt — the counts are for humans tailing logs.
    return JSONResponse({"received": len(samples), "kept": len(keep),
                         "stored": stored, "deduped": deduped})


async def get_command(request: Request) -> JSONResponse:
    if not _device_authed(request):
        return _unauthorized()
    cmd = await run_in_threadpool(hs.fetch_pending_command)
    if not cmd:
        return JSONResponse({"command": None})
    return JSONResponse({
        "command": cmd.get("command"),
        "command_id": cmd.get("command_id"),
        "requested_at": cmd.get("requested_at"),
        "duration_seconds": cmd.get("duration_seconds"),
    })


async def post_command_result(request: Request) -> JSONResponse:
    if not _device_authed(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    command_id = str((body or {}).get("command_id") or "")
    result = (body or {}).get("result")
    if not command_id or not isinstance(result, dict):
        return JSONResponse({"error": "command_id and result required"},
                            status_code=400)
    ok = await run_in_threadpool(hs.complete_command, command_id, result)
    # A late receipt for a superseded command is not an error worth retrying.
    return JSONResponse({"accepted": ok}, status_code=200 if ok else 409)


async def debug(request: Request) -> JSONResponse:
    if not _device_authed(request):
        return _unauthorized()
    data_dir: Path = hs._DATA_DIR
    files: list[str] = []
    if data_dir.exists():
        files = sorted(p.name for p in data_dir.iterdir() if p.is_file())
    return JSONResponse({
        "now": _now_iso(),
        "data_dir": str(data_dir),
        "files": files,
        "tz_offset_hours": hs._TZ_OFFSET_HOURS,
        "allowed_types": hs.ALLOWED_TYPES,
        "type_aliases": TYPE_ALIASES,
        "auth": {"ingest": bool(INGEST_TOKEN), "mcp": bool(MCP_TOKEN),
                 "open": ALLOW_NO_AUTH},
        "ingest": {
            "requests": _STATS["ingest_requests"],
            "last_at": _STATS["last_ingest_at"],
            "last_source": _STATS["last_ingest_source"],
            "stored": _STATS["samples_stored"],
            "deduped": _STATS["samples_deduped"],
            "last_error": _STATS["last_error"],
        },
        # The calibration payload: compare these against what you expect the
        # exporter to send, then fill any gap with HEALTH_TYPE_ALIASES.
        "accepted_types": dict(_STATS["accepted_types"]),
        "dropped_types": dict(_STATS["dropped_types"]),
        "units_seen": _STATS["units_seen"],
        "command": hs.get_command_state(),
    })


# ------------------------------------------------------------
# App
#
# stateless_http=True is deliberate: a stateless MCP endpoint survives a
# service restart without the agent having to reconnect. A stateful one leaves
# the client's session dangling until it opens a new one.
# ------------------------------------------------------------

ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get("COLLAR_ALLOWED_HOSTS", "").split(",") if h.strip()]

mcp = _tools.build_mcp(stateless=True, allowed_hosts=ALLOWED_HOSTS or None)
_mcp_app = mcp.streamable_http_app()

routes = [
    Route("/health", liveness, methods=["GET"]),
    Route("/api/health", ingest, methods=["POST"]),
    Route("/command", get_command, methods=["GET"]),
    Route("/command/result", post_command_result, methods=["POST"]),
    Route("/debug", debug, methods=["GET"]),
    # Mounted last: it owns /mcp and nothing else.
    Mount("/", app=_mcp_app),
]

# The mounted app's lifespan does not run on its own, and the streamable-http
# session manager needs it — without this every /mcp call fails at runtime.
app = MCPAuthMiddleware(
    Starlette(routes=routes, lifespan=_mcp_app.router.lifespan_context)
)


def main() -> None:
    import uvicorn

    # Hosting platforms inject their own PORT and route to it; binding anything
    # else is the classic "service is up but every request 502s".
    port = int(os.environ.get("PORT", "8080"))
    if not (INGEST_TOKEN or ALLOW_NO_AUTH):
        print("[collar] WARNING: HEALTH_INGEST_TOKEN unset — ingest returns 401",
              file=sys.stderr)
    if not (MCP_TOKEN or ALLOW_NO_AUTH):
        print("[collar] WARNING: COLLAR_MCP_TOKEN unset — /mcp returns 401",
              file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
