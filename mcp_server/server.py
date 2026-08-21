# server.py — MCP server exposing the health snapshot + detail query as tools.
#
# Two ways to run it:
#
# 1. stdio — a local agent launches this file directly (Claude Desktop, Claude
#    Code). In your mcp.json:
#
#      {
#        "mcpServers": {
#          "health-collar": {
#            "command": "python",
#            "args": ["/absolute/path/to/mcp_server/server.py"],
#            "env": {"HEALTH_DATA_DIR": "/absolute/path/to/data/health"}
#          }
#        }
#      }
#
# 2. streamable-http — `server/app.py` calls build_mcp() and serves these tools
#    at /mcp alongside the ingest routes, so a remote agent connects over the
#    network with no local process. That is the deployed shape; see the README.
#
# The tools read the same file store the ingest side writes to; point them at it
# with HEALTH_DATA_DIR (see .env.example).

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import health_store as hs  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402


def _disabled_tools() -> set[str]:
    """Tool names to leave unregistered, from HEALTH_DISABLED_TOOLS.

    Why this exists: a tool whose backend has been taken away does not vanish
    on its own. The agent keeps reaching for it and keeps getting a soft
    failure — and a soft failure reads as "I can't see your data", not as
    "that path is gone". Worse, it is a silent one: measure_heart_rate returns
    status=pending after 90s, which sounds like "wait a bit longer".
    (2026-08-21: exactly this cost a real debugging session. The wearer had
    deleted the Shortcut driving the measurement; the tool stayed on the
    agent's list, queued commands nobody would ever collect, and the agent
    concluded it could not see her health data at all — while the passive
    feed had been healthy the whole time.)

    So: keep the capability in the code, switch it off where it has no
    backend. A tool the agent cannot use is worse than no tool — every window
    pays for its schema, and the agent trusts what its tool list implies.
    """
    raw = os.environ.get("HEALTH_DISABLED_TOOLS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def build_mcp(stateless: bool = False,
              allowed_hosts: Optional[list[str]] = None) -> FastMCP:
    """Create the server with all three tools registered.

    stateless=True is for the HTTP deployment: each request stands alone, so
    restarting the service does not strand an agent holding a dead session.

    allowed_hosts controls the SDK's DNS-rebinding protection. That protection
    defaults to on with an *empty* allow-list, which rejects every Host header
    with a 421 — including the real domain the service is deployed under. Pass
    the hostnames you serve (":*" wildcards the port) to enable it; pass None
    to switch it off. Off is the default here because the endpoint is already
    behind a bearer token, which a rebinding attacker in a browser cannot set
    on a cross-origin request anyway.
    """
    disabled = _disabled_tools()
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts or [],
        allowed_origins=allowed_hosts or [],
    )
    mcp = FastMCP("health-collar", stateless_http=stateless,
                  json_response=stateless, transport_security=security)
    for fn in (health_now, health_detail, measure_heart_rate):
        if fn.__name__ in disabled:
            continue
        mcp.tool()(fn)
    return mcp


def health_now() -> dict:
    """A one-glance snapshot of the latest health metrics — each as value +
    freshness. Covers heart rate, resting HR, HRV, respiratory rate, last night's
    sleep (stages, period vitals, wrist temperature), and today's activity totals.
    Use this first; reach for health_detail only when you need to go deeper."""
    return hs.health_now()


async def health_detail(metric: str = "heart_rate",
                        start: str = "", end: str = "", date: str = "") -> str:
    """Go deeper than health_now on a single metric.

    metric = heart_rate / heart_rate_variability / respiratory_rate:
        every sample plus min/max/avg over a window (capped at 2h). Pass ISO
        times as start / end; defaults to the last 2h.
    metric = blood_oxygen_saturation / time_in_daylight / step_count /
             active_energy_burned / flights_climbed / walking_running_distance /
             apple_exercise_time / resting_heart_rate /
             environmental_audio_exposure / headphone_audio_exposure:
        same, but these are logged only a few times a day, so the window
        defaults to the last 24h (capped at 48h).
    metric = sleep:
        one night's stage-by-stage timeline, sleep-period vitals and wrist
        temperature. Pass date = YYYY-MM-DD; defaults to the latest night.

    Raw samples are only kept for 48h."""
    args: dict = {"metric": metric}
    if start:
        args["from"] = start
    if end:
        args["to"] = end
    if date:
        args["date"] = date
    return await hs.execute_health_detail(args)


async def measure_heart_rate() -> str:
    """Ask the watch for a FRESH heart-rate reading right now.

    Drops a command into the file store; the CollarWatch app picks it up
    (instantly while open — it polls every 15s in the foreground — or on its
    next background wake) and runs a short workout-session measurement
    (default 30s, HEALTH_MEASURE_DURATION_S). Waits up to 90 seconds.

    If nobody opens the watch in time, returns status=pending — the command
    stays valid for 30 minutes and the result lands in health_now once
    executed. Notifying the wearer is up to your own ingest side; this tool
    only queues the command."""
    return await hs.execute_measure_heart_rate()


if __name__ == "__main__":
    build_mcp().run()
