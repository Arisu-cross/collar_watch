# server.py — MCP server exposing the health snapshot + detail query as tools.
#
# Runs over stdio (how Claude Desktop / Claude Code connect). In your mcp.json:
#
#   {
#     "mcpServers": {
#       "health-collar": {
#         "command": "python",
#         "args": ["/absolute/path/to/mcp_server/server.py"],
#         "env": {"HEALTH_DATA_DIR": "/absolute/path/to/data/health"}
#       }
#     }
#   }
#
# Both tools read the same file store the ingest side writes to; point them at it
# with HEALTH_DATA_DIR (see .env.example).

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import health_store as hs  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("health-collar")


@mcp.tool()
def health_now() -> dict:
    """A one-glance snapshot of the latest health metrics — each as value +
    freshness. Covers heart rate, resting HR, HRV, respiratory rate, last night's
    sleep (stages, period vitals, wrist temperature), and today's activity totals.
    Use this first; reach for health_detail only when you need to go deeper."""
    return hs.health_now()


@mcp.tool()
async def health_detail(metric: str = "heart_rate",
                        start: str = "", end: str = "", date: str = "") -> str:
    """Go deeper than health_now on a single metric.

    metric = heart_rate / heart_rate_variability / respiratory_rate:
        every sample plus min/max/avg over a window (capped at 2h). Pass ISO
        times as start / end; defaults to the last 2h.
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


if __name__ == "__main__":
    mcp.run()
