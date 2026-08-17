# Tests for the metrics added on top of the original set: blood oxygen,
# environmental / headphone audio exposure, and menstrual cycle tracking.
#
# Standard library only — run either way:
#     python tests/test_new_metrics.py
#     pytest tests/test_new_metrics.py
#
# Each test gets its own HEALTH_DATA_DIR; health_store reads the env at import
# time, so the temp dir is set up before the import below.

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = tempfile.mkdtemp(prefix="collar-test-")
os.environ["HEALTH_DATA_DIR"] = _TMP
os.environ["HEALTH_TZ_OFFSET_HOURS"] = "0"

import health_store as hs  # noqa: E402


def _reset():
    """Empty store between tests (the module holds paths, not state)."""
    for name in os.listdir(_TMP):
        path = os.path.join(_TMP, name)
        os.unlink(path) if os.path.isfile(path) else shutil.rmtree(path)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _ingest(samples):
    stored, deduped = hs.store_samples(
        [s for s in samples if s["type"] in hs.ALLOWED_TYPES])
    return stored, deduped


def _watch(stype, value, at, unit="", extra=None):
    row = {"type": stype, "value": value, "unit": unit,
           "at": _iso(at), "source": "watch"}
    if extra is not None:
        row["extra"] = extra
    return row


def _today(minutes_ago):
    """`minutes_ago` before now, but never earlier than today 00:0X — the
    "today total" aggregations are calendar-day based, and a test running just
    after midnight would otherwise push its samples into yesterday."""
    now = datetime.now(timezone.utc)
    # Distinct, order-preserving fallbacks: samples must not collapse onto one
    # timestamp (dedup is by time) when they all get clamped.
    floor = (now.replace(hour=0, minute=0, second=0, microsecond=0)
             + timedelta(seconds=max(0, 600 - minutes_ago)))
    return max(now - timedelta(minutes=minutes_ago), floor)


def _day(offset_days, hour=12):
    return (datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0,
                                               microsecond=0)
            + timedelta(days=offset_days))


# ------------------------------------------------------------ blood oxygen

def test_blood_oxygen_snapshot():
    _reset()
    now = datetime.now(timezone.utc)
    stored, _ = _ingest([
        _watch("blood_oxygen_saturation", 97, now - timedelta(minutes=90), "%"),
        _watch("blood_oxygen_saturation", 94, now - timedelta(minutes=40), "%"),
        _watch("blood_oxygen_saturation", 98, now - timedelta(minutes=5), "%"),
    ])
    assert stored == 3

    snap = hs.health_now()
    line = snap["blood_oxygen_saturation"]
    assert line.startswith("98%"), line
    # The dip inside the window is surfaced next to the latest value.
    assert "low 94%" in line, line

    print("  blood oxygen:", line)


def test_blood_oxygen_detail_window_is_24h():
    _reset()
    now = datetime.now(timezone.utc)
    _ingest([_watch("blood_oxygen_saturation", 96, now - timedelta(hours=9), "%")])
    import asyncio
    out = json.loads(asyncio.run(
        hs.execute_health_detail({"metric": "spo2"})))
    assert out["ok"] and out["sample_count"] == 1, out
    # A 2h cap (the dense-metric default) would have missed a 9h-old sample.
    assert out["min"] == 96.0, out
    print("  spo2 detail window:", out["window"])


# ------------------------------------------------------------ audio exposure

def test_audio_exposure_is_not_summed():
    _reset()
    _ingest([
        _watch("environmental_audio_exposure", 62.0, _today(180), "dBASPL"),
        _watch("environmental_audio_exposure", 88.5, _today(120), "dBASPL"),
        _watch("environmental_audio_exposure", 70.0, _today(20), "dBASPL"),
        _watch("headphone_audio_exposure", 75.0, _today(30), "dBASPL"),
    ])
    summary = hs.health_summary(hours=6)
    env = summary["environmental_audio_exposure"]
    assert env.get("today_total") is None, "audio must never be summed"
    assert env["today_max"] == 88.5, env
    assert env["today_avg"] == 73.5, env
    assert env["today_loud_n"] == 1, env

    snap = hs.health_now()
    assert "max 88.5 dB" in snap["environmental_audio_exposure"]
    assert "at or above 80 dB" in snap["environmental_audio_exposure"]
    assert "headphone_audio_exposure" in snap
    print("  audio:", snap["environmental_audio_exposure"])


# ------------------------------------------------------------ cycle tracking

def _flow(offset_days, flow, cycle_start=False):
    level = {"none": 5, "light": 2, "medium": 3, "heavy": 4, "unspecified": 1}[flow]
    return _watch("menstrual_flow", level, _day(offset_days),
                  extra={"flow": flow, "cycle_start": cycle_start})


def test_cycle_periods_and_prediction():
    _reset()
    samples = []
    # Three periods, 28 days apart, 4-5 logged days each; the newest one starts
    # 3 days ago and is still going.
    for start in (-59, -31, -3):
        for i, flow in enumerate(["medium", "heavy", "medium", "light"]):
            samples.append(_flow(start + i, flow, cycle_start=(i == 0)))
    _ingest(samples)

    st = hs.cycle_status()
    assert st["cycle_day"] == 4, st
    assert st["state"] == "period, day 4", st
    assert st["avg_cycle_days"] == 28, st
    assert st["based_on_cycles"] == 2, st
    assert st["last_period_days"] == 4, st
    predicted = datetime.strptime(st["predicted_next_start"], "%Y-%m-%d").date()
    assert (predicted - _day(-3).date()).days == 28, st
    assert st["days_until_next"] == 25, st

    snap = hs.health_now()
    cyc = snap["menstrual_cycle"]
    assert cyc["status"] == "period, day 4", cyc
    assert "in 25 days" in cyc["predicted_next_start"], cyc
    print("  cycle:", cyc)


def test_cycle_gap_day_stays_one_period():
    _reset()
    # day 0,1 logged, day 2 skipped, day 3 logged again -> still one period.
    _ingest([_flow(-9, "medium", cycle_start=True), _flow(-8, "medium"),
             _flow(-6, "light")])
    periods = hs._cycle_periods(hs._load_cycle_history())
    assert len(periods) == 1, periods
    assert periods[0]["days"] == 4 and periods[0]["logged_days"] == 3, periods

    # A far-apart entry opens a new period.
    _ingest([_flow(-1, "medium")])
    periods = hs._cycle_periods(hs._load_cycle_history())
    assert len(periods) == 2, periods
    print("  periods:", [(str(p["start"]), p["days"]) for p in periods])


def test_cycle_none_days_do_not_start_a_period():
    _reset()
    _ingest([_flow(-5, "none"), _flow(-4, "none")])
    st = hs.cycle_status()
    assert st.get("cycle_day") is None, st
    assert "no bleeding days" in st["note"], st


def test_corrected_flow_entry_overwrites_same_day():
    _reset()
    # Yesterday, so the entry is inside the window store_samples loads dedup
    # keys from (yesterday + today).
    _ingest([_flow(-1, "light", cycle_start=True)])
    # Same day re-logged as heavy: (type, at) alone would drop it as a dup.
    stored, deduped = _ingest([_flow(-1, "heavy", cycle_start=True)])
    assert stored == 1 and deduped == 0, (stored, deduped)
    hist = hs._load_cycle_history()
    assert len(hist) == 1 and hist[0]["flow"] == "heavy", hist

    # An identical re-upload is still deduped.
    stored, deduped = _ingest([_flow(-1, "heavy", cycle_start=True)])
    assert (stored, deduped) == (0, 1), (stored, deduped)
    print("  corrected entry:", hist[0])


def test_hae_menstruation_alias_and_raw_level():
    _reset()
    at = _day(-1)
    body = {"data": {"metrics": [{"name": "menstruation", "units": "",
                                  "data": [{"date": at.strftime("%Y-%m-%d %H:%M:%S +0000"),
                                            "qty": 3}]}]}}
    samples = hs.normalize_payload(body)
    assert samples and samples[0]["type"] == "menstrual_flow", samples
    _ingest(samples)
    hist = hs._load_cycle_history()
    # No "flow" label from HAE -> derived from the HKCategoryValue code.
    assert hist[0]["flow"] == "medium", hist


def test_cycle_detail_query():
    _reset()
    for start in (-60, -32, -4):
        _ingest([_flow(start + i, "medium", cycle_start=(i == 0)) for i in range(3)])
    import asyncio
    out = json.loads(asyncio.run(hs.execute_health_detail({"metric": "cycle"})))
    assert out["ok"], out
    assert len(out["recent_periods"]) == 3, out
    assert out["recent_periods"][-1]["days_since_previous_start"] == 28, out
    assert out["current"]["cycle_day"] == 5, out
    print("  cycle detail:", out["recent_periods"][-1])


def test_cycle_off_switch():
    _reset()
    _ingest([_flow(-2, "medium", cycle_start=True)])
    original = hs.ALLOWED_TYPES
    try:
        hs.ALLOWED_TYPES = [t for t in original if t != "menstrual_flow"]
        assert "menstrual_cycle" not in hs.health_now()
        import asyncio
        out = json.loads(asyncio.run(hs.execute_health_detail({"metric": "cycle"})))
        assert "not enabled" in out["note"], out
    finally:
        hs.ALLOWED_TYPES = original


def test_menstrual_flow_has_no_bogus_averages():
    _reset()
    now = datetime.now(timezone.utc)
    _ingest([_watch("menstrual_flow", 3, now - timedelta(minutes=30),
                    extra={"flow": "medium", "cycle_start": True})])
    summary = hs.health_summary(hours=6)
    rng = summary["menstrual_flow"]["range"]
    assert rng["avg"] is None and rng["min"] is None, rng
    assert rng["n"] == 1, rng


# ------------------------------------------------------------ regression

def test_existing_metrics_still_work():
    _reset()
    _ingest([
        _watch("heart_rate", 72, _today(5), "count/min"),
        _watch("step_count", 400, _today(120), "count"),
        _watch("step_count", 350, _today(10), "count"),
    ])
    snap = hs.health_now()
    assert snap["heart_rate"].startswith("72 bpm"), snap
    assert snap["step_count"] == "750", snap
    # Cumulative metrics are still summed, unlike the audio ones.
    assert hs.health_summary(hours=6)["step_count"]["today_total"] == 750


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
