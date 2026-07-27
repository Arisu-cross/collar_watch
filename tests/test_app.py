"""End-to-end tests for the HTTP shell.

Run with:  python -m pytest tests/ -q

Every test gets its own temp data directory, so the module has to be imported
*after* HEALTH_DATA_DIR is set — health_store resolves its paths at import time.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

INGEST_TOKEN = "ingest-token-for-tests"
MCP_TOKEN = "mcp-token-for-tests"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DATA_DIR", str(tmp_path / "health"))
    monkeypatch.setenv("HEALTH_TZ_OFFSET_HOURS", "8")
    monkeypatch.setenv("HEALTH_INGEST_TOKEN", INGEST_TOKEN)
    monkeypatch.setenv("COLLAR_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.delenv("COLLAR_ALLOW_NO_AUTH", raising=False)

    for name in ("health_store", "collar_mcp_tools", "app"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(ROOT / "server"))
    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)

    with TestClient(app_module.app) as c:
        c.app_module = app_module
        yield c


def _auth(token=INGEST_TOKEN):
    return {"X-Health-Token": token}


# ------------------------------------------------------------
# Auth
# ------------------------------------------------------------

def test_liveness_needs_no_token(client):
    assert client.get("/health").status_code == 200


def test_ingest_rejects_missing_token(client):
    r = client.post("/api/health", json={"samples": []})
    assert r.status_code == 401


def test_ingest_rejects_wrong_token(client):
    r = client.post("/api/health", json={"samples": []},
                    headers=_auth("nope"))
    assert r.status_code == 401


def test_ingest_accepts_query_param_token(client):
    r = client.post(f"/api/health?token={INGEST_TOKEN}", json={"samples": []})
    assert r.status_code == 200


def test_ingest_accepts_bearer(client):
    r = client.post("/api/health", json={"samples": []},
                    headers={"Authorization": f"Bearer {INGEST_TOKEN}"})
    assert r.status_code == 200


# ------------------------------------------------------------
# Ingest — both payload shapes
# ------------------------------------------------------------

WATCH_PAYLOAD = {
    "source": "watch",
    "samples": [
        {"type": "heart_rate", "value": 72, "unit": "count/min",
         "at": "2026-07-27T10:00:00+08:00"},
        {"type": "heart_rate", "value": 75, "unit": "count/min",
         "at": "2026-07-27T10:01:00+08:00"},
    ],
}


def test_watch_payload_is_stored(client):
    r = client.post("/api/health", json=WATCH_PAYLOAD, headers=_auth())
    assert r.status_code == 200
    assert r.json()["stored"] == 2


def test_ingest_is_idempotent(client):
    client.post("/api/health", json=WATCH_PAYLOAD, headers=_auth())
    r = client.post("/api/health", json=WATCH_PAYLOAD, headers=_auth())
    # Same (type, timestamp) pairs — the watch replays these after a failed
    # upload, so the second round must add nothing.
    assert r.json()["stored"] == 0
    assert r.json()["deduped"] == 2


def test_health_auto_export_payload_is_stored(client):
    hae = {
        "data": {
            "metrics": [
                {"name": "heart_rate", "units": "count/min",
                 "data": [{"date": "2026-07-27 09:00:00 +0800", "Avg": 68}]},
                {"name": "step_count", "units": "count",
                 "data": [{"date": "2026-07-27 09:00:00 +0800", "qty": 430}]},
            ]
        }
    }
    r = client.post("/api/health", json=hae, headers=_auth())
    assert r.status_code == 200
    assert r.json()["stored"] == 2


def test_active_energy_alias_is_applied(client):
    """Health Auto Export says `active_energy`; the allow-list says
    `active_energy_burned`. Without the alias this sample vanishes silently."""
    hae = {
        "data": {
            "metrics": [
                {"name": "active_energy", "units": "kcal",
                 "data": [{"date": "2026-07-27 09:00:00 +0800", "qty": 210}]},
            ]
        }
    }
    r = client.post("/api/health", json=hae, headers=_auth())
    assert r.json()["stored"] == 1

    debug = client.get("/debug", headers=_auth()).json()
    assert "active_energy_burned" in debug["accepted_types"]
    assert debug["dropped_types"] == {}


def test_unknown_types_are_dropped_but_reported(client):
    payload = {
        "source": "watch",
        "samples": [{"type": "mindful_minutes", "value": 5, "unit": "min",
                     "at": "2026-07-27T10:00:00+08:00"}],
    }
    r = client.post("/api/health", json=payload, headers=_auth())
    assert r.json()["kept"] == 0

    debug = client.get("/debug", headers=_auth()).json()
    # Surfaced rather than swallowed — this is how you find naming mismatches.
    assert debug["dropped_types"]["mindful_minutes"] == 1


def test_custom_alias_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DATA_DIR", str(tmp_path / "health"))
    monkeypatch.setenv("HEALTH_INGEST_TOKEN", INGEST_TOKEN)
    monkeypatch.setenv("COLLAR_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setenv("HEALTH_TYPE_ALIASES", "pulse=heart_rate")
    for name in ("health_store", "collar_mcp_tools", "app"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(ROOT / "server"))
    app_module = importlib.reload(importlib.import_module("app"))

    with TestClient(app_module.app) as c:
        r = c.post("/api/health", headers=_auth(), json={
            "source": "watch",
            "samples": [{"type": "pulse", "value": 61, "unit": "count/min",
                         "at": "2026-07-27T10:00:00+08:00"}],
        })
        assert r.json()["stored"] == 1


def test_invalid_json_is_rejected(client):
    r = client.post("/api/health", content=b"not json", headers=_auth())
    assert r.status_code == 400


# ------------------------------------------------------------
# Command channel
# ------------------------------------------------------------

def test_command_empty_when_none_queued(client):
    r = client.get("/command", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"command": None}


def test_command_roundtrip(client):
    hs = sys.modules["health_store"]
    created = hs.create_command("measure_heart_rate")

    fetched = client.get("/command", headers=_auth()).json()
    assert fetched["command"] == "measure_heart_rate"
    assert fetched["command_id"] == created["command_id"]
    assert fetched["duration_seconds"] == 30

    r = client.post("/command/result", headers=_auth(), json={
        "command_id": created["command_id"],
        "result": {"heart_rate_average": 83, "heart_rate_minimum": 79,
                   "heart_rate_maximum": 91, "sample_count": 12},
    })
    assert r.status_code == 200 and r.json()["accepted"] is True

    # Completed commands must not be handed out again, or the watch re-measures.
    assert client.get("/command", headers=_auth()).json() == {"command": None}


def test_result_for_unknown_command_is_conflict(client):
    r = client.post("/command/result", headers=_auth(), json={
        "command_id": "cmd_doesnotexist", "result": {"sample_count": 0}})
    assert r.status_code == 409


def test_result_requires_fields(client):
    r = client.post("/command/result", headers=_auth(), json={"command_id": "x"})
    assert r.status_code == 400


def test_command_requires_token(client):
    assert client.get("/command").status_code == 401
    assert client.post("/command/result", json={}).status_code == 401


# ------------------------------------------------------------
# MCP endpoint
# ------------------------------------------------------------

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _mcp_call(client, payload, token=MCP_TOKEN, header="Authorization"):
    headers = dict(MCP_HEADERS)
    if token:
        headers[header] = f"Bearer {token}" if header == "Authorization" else token
    return client.post("/mcp", headers=headers, json=payload)


def test_mcp_rejects_missing_token(client):
    r = _mcp_call(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  token=None)
    assert r.status_code == 401


def test_mcp_rejects_ingest_token(client):
    """The two credentials are not interchangeable — a device token must not
    unlock the read side."""
    r = _mcp_call(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  token=INGEST_TOKEN)
    assert r.status_code == 401


def test_mcp_accepts_x_token_header(client):
    r = _mcp_call(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  header="X-Token")
    assert r.status_code == 200


def test_mcp_lists_tools_without_a_handshake(client):
    """Stateless transport: a bare tools/list answers, no initialize needed.
    A stateful server would reject this with 'Missing session ID'."""
    r = _mcp_call(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"health_now", "health_detail", "measure_heart_rate"}


def test_allowed_hosts_gate(tmp_path, monkeypatch):
    """COLLAR_ALLOWED_HOSTS turns on the SDK's DNS-rebinding protection.

    Regression guard: the SDK enables that protection by default with an empty
    allow-list, which answers 421 to *every* Host — the real deployment domain
    included. Leaving the variable unset must therefore leave it off.
    """
    monkeypatch.setenv("HEALTH_DATA_DIR", str(tmp_path / "health"))
    monkeypatch.setenv("HEALTH_INGEST_TOKEN", INGEST_TOKEN)
    monkeypatch.setenv("COLLAR_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setenv("COLLAR_ALLOWED_HOSTS", "collar.example.com,testserver")
    for name in ("health_store", "collar_mcp_tools", "app"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(ROOT / "server"))
    app_module = importlib.reload(importlib.import_module("app"))

    listed = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(app_module.app) as c:
        assert _mcp_call(c, listed).status_code == 200
        r = c.post("/mcp", json=listed,
                   headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TOKEN}",
                            "Host": "evil.example.com"})
        assert r.status_code == 421


def test_mcp_health_now_reads_ingested_data(client):
    client.post("/api/health", json=WATCH_PAYLOAD, headers=_auth())
    r = _mcp_call(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "health_now", "arguments": {}}})
    assert r.status_code == 200
    body = json.dumps(r.json())
    assert "heart_rate" in body
    assert "75" in body  # the newer of the two samples


# ------------------------------------------------------------
# Unit conversion
#
# The watch app and Health Auto Export disagree on units for exactly two
# metrics. Reading the source's own unit is the only thing standing between a
# correct figure and one that is wrong by a constant factor.
# ------------------------------------------------------------

def _totals(client, metric, unit, values, at="2026-07-27T10:00:00+08:00"):
    client.post("/api/health", headers=_auth(), json={
        "source": "watch",
        "samples": [{"type": metric, "value": v, "unit": unit, "at": at}
                    for v in values]})
    hs = sys.modules["health_store"]
    return hs.health_now()


def test_energy_in_kilojoules_is_converted(client, monkeypatch):
    monkeypatch.setattr(sys.modules["health_store"], "_now",
                        lambda: __import__("datetime").datetime.fromisoformat(
                            "2026-07-27T20:00:00+08:00"))
    out = _totals(client, "active_energy_burned", "kJ", [418.4])
    # 418.4 kJ is 100 kcal — labelling it "418 kcal" overstates it 4.2x.
    assert out["active_energy_burned"] == "100 kcal"


def test_energy_in_kcal_is_left_alone(client, monkeypatch):
    monkeypatch.setattr(sys.modules["health_store"], "_now",
                        lambda: __import__("datetime").datetime.fromisoformat(
                            "2026-07-27T20:00:00+08:00"))
    out = _totals(client, "active_energy_burned", "kcal", [250.0])
    assert out["active_energy_burned"] == "250 kcal"


def test_distance_in_km_is_not_rescaled(client, monkeypatch):
    monkeypatch.setattr(sys.modules["health_store"], "_now",
                        lambda: __import__("datetime").datetime.fromisoformat(
                            "2026-07-27T20:00:00+08:00"))
    out = _totals(client, "walking_running_distance", "km", [5.0])
    # Previously multiplied by 1.609344 unconditionally, inflating km sources.
    assert out["walking_running_distance"] == "5.0 km"


def test_distance_in_miles_is_converted(client, monkeypatch):
    monkeypatch.setattr(sys.modules["health_store"], "_now",
                        lambda: __import__("datetime").datetime.fromisoformat(
                            "2026-07-27T20:00:00+08:00"))
    out = _totals(client, "walking_running_distance", "mi", [5.0])
    assert out["walking_running_distance"] == "8.0 km"


def test_daylight_and_noise_reach_health_now(client, monkeypatch):
    """Context metrics: a daily total for daylight, a latest level for noise."""
    monkeypatch.setattr(sys.modules["health_store"], "_now",
                        lambda: __import__("datetime").datetime.fromisoformat(
                            "2026-07-27T20:00:00+08:00"))
    client.post("/api/health", headers=_auth(), json={"data": {"metrics": [
        {"name": "time_in_daylight", "units": "min", "data": [
            {"date": "2026-07-27 11:00:00 +0800", "qty": 45},
            {"date": "2026-07-27 15:00:00 +0800", "qty": 50}]},
        {"name": "environmental_audio_exposure", "units": "dBASPL", "data": [
            {"date": "2026-07-27 19:30:00 +0800", "Avg": 72}]},
    ]}})
    out = sys.modules["health_store"].health_now()
    assert out["time_in_daylight"] == "1 hr 35 min"      # summed across the day
    assert out["environmental_audio_exposure"].startswith("72 dB")
