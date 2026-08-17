# ============================================================
# health_store.py — HealthKit sample ingestion, storage & aggregation
#
#   - Multi-source idempotent merge: Health Auto Export REST format +
#     a compact watch/app format. Samples are deduped by (type, at);
#     whoever sends it, latest wins.
#   - File storage only (no database required):
#       <DATA_DIR>/latest.json             newest sample per type (atomic write)
#       <DATA_DIR>/samples-YYYYMMDD.jsonl  raw samples, rolling 48h retention
#       <DATA_DIR>/wrist_baseline.json     cumulative wrist-temp baseline
#       <DATA_DIR>/sleep_history.jsonl     daily sleep summaries, 30d retention
#       <DATA_DIR>/cycle_history.jsonl     menstrual flow by day, 400d retention
#   - Display times use a configurable UTC offset; machine fields store UTC.
#
#   Environment:
#       HEALTH_DATA_DIR         storage directory   (default ./data/health)
#       HEALTH_TZ_OFFSET_HOURS  display tz offset    (default 0 = UTC)
#       HEALTH_ALLOWED_TYPES    comma-separated type allow-list (optional override)
# ============================================================

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TZ_OFFSET_HOURS = float(os.environ.get("HEALTH_TZ_OFFSET_HOURS", "0") or 0)
_TZ = timezone(timedelta(hours=_TZ_OFFSET_HOURS))
_DATA_DIR = Path(os.environ.get("HEALTH_DATA_DIR", "./data/health")).expanduser()
_LATEST = _DATA_DIR / "latest.json"
_WRIST_BASELINE = _DATA_DIR / "wrist_baseline.json"
_SLEEP_HISTORY = _DATA_DIR / "sleep_history.jsonl"
_CYCLE_HISTORY = _DATA_DIR / "cycle_history.jsonl"
_WRIST_TYPE = "apple_sleeping_wrist_temperature"
_CYCLE_TYPE = "menstrual_flow"

# Drill/test sources are stored but excluded from the status view.
_STATUS_EXCLUDED_SOURCES = {"filter_test", "drill"}

# HealthKit emits these as interval contributions; the status view exposes only
# their current calendar-day sum as an additive field.
_CUMULATIVE_TYPES = {
    "step_count", "flights_climbed", "walking_running_distance",
    "active_energy_burned", "apple_exercise_time",
}
_HRV_STATUS_MAX_AGE_MIN = 24 * 60

# Audio exposure arrives as interval samples that already carry an average dB
# level, so summing them is meaningless: the status view reports today's peak
# and sample mean instead. Apple's own "loud" threshold is 80 dB(A).
_AUDIO_TYPES = {"environmental_audio_exposure", "headphone_audio_exposure"}
_AUDIO_LOUD_DB = 80.0

# Category samples whose numeric value is a code, not a measurement: min/max/avg
# over them would be nonsense, so the range stats are skipped.
_NON_NUMERIC_TYPES = {_CYCLE_TYPE}

# Normally a sample is identified by (type, at). Editing a cycle-tracking entry
# in the Health app keeps the same day but changes the flow level, so for these
# types the value joins the dedup key — otherwise the correction is silently
# dropped as a duplicate and the history keeps the old level forever.
_VALUE_KEYED_TYPES = {_CYCLE_TYPE}

# Type allow-list: only ingest the metrics that matter; anything else is dropped
# at the door. Override with the HEALTH_ALLOWED_TYPES env var if you need more.
# Dropping "menstrual_flow" from this list is also the off switch for the whole
# cycle feature (ingest and the health_now / health_detail views alike).
ALLOWED_TYPES: list[str] = [
    "heart_rate", "heart_rate_variability", "resting_heart_rate",
    "sleep_analysis", "respiratory_rate", "blood_oxygen_saturation",
    "step_count", "flights_climbed", "walking_running_distance",
    "active_energy_burned", "apple_exercise_time",
    "apple_sleeping_wrist_temperature",
    "environmental_audio_exposure", "headphone_audio_exposure",
    _CYCLE_TYPE,
]
_env_allowed = os.environ.get("HEALTH_ALLOWED_TYPES", "").strip()
if _env_allowed:
    ALLOWED_TYPES = [t.strip() for t in _env_allowed.split(",") if t.strip()]

# Health Auto Export names a few of these differently than the watch does.
_TYPE_ALIASES = {
    "menstruation": _CYCLE_TYPE,
    "oxygen_saturation": "blood_oxygen_saturation",
}

# HKCategoryValueMenstrualFlow codes, for payloads that carry the raw value
# without the watch app's readable "flow" label.
_FLOW_NAMES = {1: "unspecified", 2: "light", 3: "medium", 4: "heavy", 5: "none"}
_CYCLE_HISTORY_DAYS = 400
# Bleeding days this far apart still count as one period (a gap day mid-period
# is common); anything longer starts a new one.
_CYCLE_PERIOD_GAP_DAYS = 2
# Cycle lengths outside this range are treated as gaps in logging, not cycles.
_CYCLE_MIN_LEN, _CYCLE_MAX_LEN = 15, 60

# Per-metric window cap for health_detail. Dense metrics stay at 2h so a reply
# is not a wall of points; sparse ones (a handful of samples a day) get a day,
# still inside the 48h raw-sample retention.
_DETAIL_MAX_HOURS = {
    "heart_rate": 2, "heart_rate_variability": 2, "respiratory_rate": 2,
    "blood_oxygen_saturation": 24,
    "environmental_audio_exposure": 24, "headphone_audio_exposure": 24,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z",
                    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------
# Normalize: two payload formats -> [{type, value, unit, at(UTC iso), source}]
# ------------------------------------------------------------

def normalize_payload(body: Any) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return samples

    # --- Format 2: compact watch / app format ---
    if isinstance(body.get("samples"), list):
        source = str(body.get("source") or "app")[:40]
        for s in body["samples"]:
            if not isinstance(s, dict):
                continue
            at = _parse_dt(s.get("at"))
            stype = str(s.get("type") or "").strip().lower()
            stype = _TYPE_ALIASES.get(stype, stype)
            if not stype or at is None:
                continue
            extra = s.get("extra") if isinstance(s.get("extra"), dict) else None
            try:
                value = float(s.get("value"))
            except (TypeError, ValueError):
                # Structured samples without a scalar value (sleep stages, etc.):
                # keep only if they carry extra, mirroring format 1's fallback.
                if extra is None:
                    continue
                value = None
            row = {"type": stype, "value": value,
                   "unit": str(s.get("unit") or "")[:20],
                   "at": _iso(at), "source": source}
            if extra is not None:
                row["extra"] = extra
            samples.append(row)
        return samples

    # --- Format 1: Health Auto Export REST format ---
    metrics = ((body.get("data") or {}).get("metrics")
               if isinstance(body.get("data"), dict) else None)
    if isinstance(metrics, list):
        for m in metrics:
            if not isinstance(m, dict):
                continue
            stype = str(m.get("name") or "").strip().lower()
            stype = _TYPE_ALIASES.get(stype, stype)
            unit = str(m.get("units") or "")[:20]
            for d in m.get("data") or []:
                if not isinstance(d, dict):
                    continue
                at = _parse_dt(d.get("date"))
                if at is None:
                    continue
                raw_v = d.get("qty")
                if raw_v is None:
                    raw_v = d.get("Avg") or d.get("avg")
                if raw_v is None:
                    # Structured samples (sleep_analysis, etc.): keep raw in extra.
                    samples.append({"type": stype, "value": None, "unit": unit,
                                    "at": _iso(at), "source": "auto_export",
                                    "extra": {k: v for k, v in d.items()
                                              if k != "date"}})
                    continue
                try:
                    value = float(raw_v)
                except (TypeError, ValueError):
                    continue
                samples.append({"type": stype, "value": value, "unit": unit,
                                "at": _iso(at), "source": "auto_export"})
    return samples


# ------------------------------------------------------------
# Storage: rolling jsonl + atomic latest.json
# ------------------------------------------------------------

def _day_file(dt: datetime) -> Path:
    return _DATA_DIR / f"samples-{dt.astimezone(_TZ).strftime('%Y%m%d')}.jsonl"


def _dedup_key(s: dict[str, Any]) -> tuple:
    stype = s.get("type")
    if stype in _VALUE_KEYED_TYPES:
        return (stype, s.get("at"), s.get("value"))
    return (stype, s.get("at"))


def _load_seen_keys() -> set:
    seen = set()
    now = _now()
    for f in (_day_file(now - timedelta(days=1)), _day_file(now)):
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    seen.add(_dedup_key(json.loads(line)))
                except Exception:
                    continue
        except Exception:
            logger.warning("health_store: seen-key load failed for %s", f, exc_info=True)
    return seen


def _cleanup_old_files() -> None:
    cutoff = (_now().astimezone(_TZ) - timedelta(days=2)).strftime("%Y%m%d")
    try:
        for f in _DATA_DIR.glob("samples-*.jsonl"):
            if f.stem.replace("samples-", "") < cutoff:
                f.unlink()
    except Exception:
        logger.warning("health_store: old-file cleanup failed", exc_info=True)


def _update_wrist_baseline(new_values: list[float]) -> None:
    """Persistent wrist-temperature baseline (cumulative average, survives the
    48h cleanup). One reading per night, n = number of nights."""
    if not new_values:
        return
    base = {"avg": None, "n": 0}
    if _WRIST_BASELINE.exists():
        try:
            base = json.loads(_WRIST_BASELINE.read_text(encoding="utf-8"))
        except Exception:
            base = {"avg": None, "n": 0}
    avg = base.get("avg")
    n = int(base.get("n") or 0)
    for v in new_values:
        avg = v if avg is None else (avg * n + v) / (n + 1)
        n += 1
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_json({"avg": avg, "n": n}))
        os.replace(tmp, _WRIST_BASELINE)
    except Exception:
        logger.warning("health_store: wrist baseline write failed", exc_info=True)


def _wrist_baseline_tag(current: Any) -> Optional[str]:
    """Current wrist temp vs. the persistent baseline, as a short deviation tag."""
    try:
        cur = float(current)
    except (TypeError, ValueError):
        return None
    try:
        base = json.loads(_WRIST_BASELINE.read_text(encoding="utf-8"))
        avg = float(base.get("avg"))
        n = int(base.get("n") or 0)
    except Exception:
        return "baseline forming"
    if n < 3:
        return f"baseline forming (night {n} of ~5)"
    diff = cur - avg
    if abs(diff) < 0.1:
        return "at baseline"
    return f"{abs(diff):.1f}°C {'above' if diff > 0 else 'below'} baseline"


def store_samples(samples: list[dict[str, Any]]) -> tuple[int, int]:
    """Ingest. Returns (stored, deduped)."""
    if not samples:
        return 0, 0
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen = _load_seen_keys()
    stored = deduped = 0
    latest: dict[str, Any] = {}
    if _LATEST.exists():
        try:
            latest = json.loads(_LATEST.read_text(encoding="utf-8"))
        except Exception:
            latest = {}

    by_file: dict[Path, list[str]] = {}
    new_wrist: list[float] = []
    new_sleep: list[dict[str, Any]] = []
    new_cycle: list[dict[str, Any]] = []
    for s in sorted(samples, key=lambda x: str(x.get("at") or "")):
        key = _dedup_key(s)
        if key in seen:
            deduped += 1
            continue
        seen.add(key)
        stored += 1
        if (s.get("type") == _WRIST_TYPE and s.get("value") is not None
                and str(s.get("source") or "") == "watch"):
            try:
                new_wrist.append(float(s["value"]))
            except (TypeError, ValueError):
                pass
        if s.get("type") == "sleep_analysis" and str(s.get("source") or "") == "watch":
            new_sleep.append(s)
        if s.get("type") == _CYCLE_TYPE:
            new_cycle.append(s)
        at = _parse_dt(s["at"]) or _now()
        by_file.setdefault(_day_file(at), []).append(_json(s))
        prev = latest.get(s["type"]) or {}
        if str(s["at"]) >= str(prev.get("at") or ""):
            latest[s["type"]] = {"value": s.get("value"), "unit": s.get("unit"),
                                 "at": s["at"], "source": s.get("source")}

    for f, lines in by_file.items():
        with f.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # latest.json atomic write (tmp + rename, guards against half-written JSON)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_json(latest))
        os.replace(tmp, _LATEST)
    except Exception:
        logger.warning("health_store: latest.json write failed", exc_info=True)

    _update_wrist_baseline(new_wrist)
    _update_sleep_history(new_sleep)
    _update_cycle_history(new_cycle)
    _cleanup_old_files()
    return stored, deduped


def _parse_range_dt(raw: Any, *, naive_tz: timezone = _TZ) -> Optional[datetime]:
    dt = _parse_dt(raw)
    if dt is None:
        return None
    text = str(raw).strip()
    if dt.tzinfo is timezone.utc and text and not any(x in text for x in ("+", "Z", "z")):
        return dt.replace(tzinfo=naive_tz)
    return dt


def _iter_day_files(start: datetime, end: datetime) -> list[Path]:
    day = start.astimezone(_TZ).date()
    last = end.astimezone(_TZ).date()
    files: list[Path] = []
    while day <= last:
        files.append(_DATA_DIR / f"samples-{day.strftime('%Y%m%d')}.jsonl")
        day = day + timedelta(days=1)
    return files


def _samples_between(start: datetime, end: datetime,
                     *, types: Optional[set[str]] = None,
                     exclude_sources: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Filter by HealthKit sample `at`, not by upload/write time."""
    rows: dict[tuple[Any, Any], dict[str, Any]] = {}
    for f in _iter_day_files(start, end):
        if not f.exists():
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                s = json.loads(line)
            except Exception:
                continue
            stype = str(s.get("type") or "")
            if types is not None and stype not in types:
                continue
            if exclude_sources and str(s.get("source") or "") in exclude_sources:
                continue
            at = _parse_dt(s.get("at"))
            if at is None or at < start or at > end:
                continue
            # Historical files can contain drills or retries; keep one per (type, at).
            rows[(stype, s.get("at"))] = s
    out = list(rows.values())
    out.sort(key=lambda s: (str(s.get("type") or ""), str(s.get("at") or "")))
    return out


def _sample_points(rows: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(rows) <= max_points:
        picked = rows
    elif max_points <= 1:
        picked = rows[-1:]
    else:
        step = (len(rows) - 1) / float(max_points - 1)
        indexes = sorted({round(i * step) for i in range(max_points)})
        picked = [rows[i] for i in indexes]
    points = []
    for s in picked:
        at = _parse_dt(s.get("at"))
        points.append({
            "at": at.astimezone(_TZ).isoformat(timespec="minutes") if at else s.get("at"),
            "value": s.get("value"),
            "unit": s.get("unit"),
            "source": s.get("source"),
        })
    return points


def _rounded_total(stype: str, values: list[float]) -> Optional[float | int]:
    if not values:
        return None
    total = sum(values)
    if stype in {"step_count", "flights_climbed"}:
        return int(round(total))
    return round(total, 3)


def _stress_hint_from_hrv(value: Any, age_min: Any) -> Optional[str]:
    """Coarse stress hint derived from HRV (SDNN, ms). Returns None if stale/unknown."""
    try:
        hrv = float(value)
        age = int(age_min)
    except (TypeError, ValueError):
        return None
    if age > _HRV_STATUS_MAX_AGE_MIN:
        return None
    if hrv >= 60:
        return "relaxed"
    if hrv >= 45:
        return "low stress"
    if hrv >= 25:
        return "normal"
    if hrv >= 15:
        return "elevated"
    return "high stress"


# ------------------------------------------------------------
# Health summary (feeds the status snapshot / charting front-ends)
# ------------------------------------------------------------

def health_summary(*, hours: float = 6, start: Any = None, end: Any = None,
                   max_points: int = 120) -> Optional[dict[str, Any]]:
    if not _LATEST.exists():
        return None
    try:
        latest = json.loads(_LATEST.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not latest:
        return None

    now = _now()
    end_dt = _parse_range_dt(end) if end else now
    if end_dt is None:
        end_dt = now
    try:
        hours_f = float(hours)
    except Exception:
        hours_f = 6.0
    hours_f = min(48.0, max(0.25, hours_f))
    start_dt = _parse_range_dt(start) if start else end_dt - timedelta(hours=hours_f)
    if start_dt is None:
        start_dt = end_dt - timedelta(hours=hours_f)
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    if end_dt - start_dt > timedelta(hours=48):
        start_dt = end_dt - timedelta(hours=48)
    try:
        max_points_i = int(max_points)
    except Exception:
        max_points_i = 120
    max_points_i = min(500, max(10, max_points_i))

    status_types = set(ALLOWED_TYPES)
    window_rows = _samples_between(start_dt, end_dt,
                                   types=status_types,
                                   exclude_sources=_STATUS_EXCLUDED_SOURCES)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for s in window_rows:
        grouped.setdefault(str(s.get("type") or ""), []).append(s)

    today_start = now.astimezone(_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0)
    today_rows = _samples_between(
        today_start, now,
        types=status_types & (_CUMULATIVE_TYPES | _AUDIO_TYPES),
        exclude_sources=_STATUS_EXCLUDED_SOURCES)
    today_grouped: dict[str, list[dict[str, Any]]] = {}
    for s in today_rows:
        today_grouped.setdefault(str(s.get("type") or ""), []).append(s)

    out: dict[str, Any] = {
        "_range": {
            "from": start_dt.astimezone(_TZ).isoformat(timespec="minutes"),
            "to": end_dt.astimezone(_TZ).isoformat(timespec="minutes"),
            "hours": round((end_dt - start_dt).total_seconds() / 3600, 2),
            "basis": "sample_at",
            "dedup": "type+at",
            "max_points_per_metric": max_points_i,
            "excluded_sources": sorted(_STATUS_EXCLUDED_SOURCES),
            "included_types": sorted(status_types),
        }
    }

    for stype, s in latest.items():
        if stype not in status_types:
            continue
        if str(s.get("source") or "") in _STATUS_EXCLUDED_SOURCES:
            continue
        at = _parse_dt(s.get("at"))
        out[stype] = {
            "latest": s.get("value"), "unit": s.get("unit"),
            "at": at.astimezone(_TZ).isoformat(timespec="minutes") if at else s.get("at"),
            "age_min": max(0, int((now - at).total_seconds() // 60)) if at else None,
            "source": s.get("source"),
            "in_range": bool(at and start_dt <= at <= end_dt),
        }

    for stype in status_types & _CUMULATIVE_TYPES:
        metric = out.get(stype)
        if not isinstance(metric, dict):
            continue
        # Source priority: if a type has any "watch" sample today, count only the
        # watch samples and ignore other sources (avoids double-counting when both
        # a watch and Health Auto Export report the same cumulative metric).
        day_samples = today_grouped.get(stype, [])
        watch_only = [s for s in day_samples if str(s.get("source") or "") == "watch"]
        use_samples = watch_only if watch_only else day_samples
        values: list[float] = []
        for sample in use_samples:
            try:
                if sample.get("value") is not None:
                    values.append(float(sample.get("value")))
            except (TypeError, ValueError):
                continue
        metric["today_total"] = _rounded_total(stype, values)

    for stype in status_types & _AUDIO_TYPES:
        metric = out.get(stype)
        if not isinstance(metric, dict):
            continue
        values = []
        for sample in today_grouped.get(stype, []):
            try:
                if sample.get("value") is not None:
                    values.append(float(sample.get("value")))
            except (TypeError, ValueError):
                continue
        if not values:
            continue
        metric["today_max"] = round(max(values), 1)
        # Unweighted mean of the interval samples — a rough "how loud was today",
        # not a duration-weighted exposure level.
        metric["today_avg"] = round(sum(values) / len(values), 1)
        metric["today_n"] = len(values)
        metric["today_loud_n"] = sum(1 for v in values if v >= _AUDIO_LOUD_DB)

    hrv = out.get("heart_rate_variability")
    if isinstance(hrv, dict):
        hint = _stress_hint_from_hrv(hrv.get("latest"), hrv.get("age_min"))
        if hint:
            hrv["stress_hint"] = hint

    wrist = out.get(_WRIST_TYPE)
    if isinstance(wrist, dict):
        wrist["vs_baseline"] = _wrist_baseline_tag(wrist.get("latest"))

    for stype, rows in grouped.items():
        rows.sort(key=lambda s: str(s.get("at") or ""))
        vals = []
        if stype not in _NON_NUMERIC_TYPES:
            for s in rows:
                try:
                    if s.get("value") is not None:
                        vals.append(float(s.get("value")))
                except Exception:
                    continue
        latest_row = rows[-1]
        latest_at = _parse_dt(latest_row.get("at"))
        metric = out.setdefault(stype, {})
        metric.update({
            "latest_in_range": latest_row.get("value"),
            "latest_in_range_at": latest_at.astimezone(_TZ).isoformat(timespec="minutes") if latest_at else latest_row.get("at"),
            "unit": metric.get("unit") or latest_row.get("unit"),
            "sources_in_range": sorted({str(s.get("source") or "") for s in rows if s.get("source")}),
            "range": {
                "n": len(rows),
                "numeric_n": len(vals),
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
                "avg": round(sum(vals) / len(vals), 1) if vals else None,
                "from": _parse_dt(rows[0].get("at")).astimezone(_TZ).isoformat(timespec="minutes") if _parse_dt(rows[0].get("at")) else rows[0].get("at"),
                "to": latest_at.astimezone(_TZ).isoformat(timespec="minutes") if latest_at else latest_row.get("at"),
            },
            "samples": _sample_points(rows, max_points_i),
            "samples_truncated": len(rows) > max_points_i,
        })
        label = f"{int(out['_range']['hours'])}h" if float(out["_range"]["hours"]).is_integer() else f"{out['_range']['hours']}h"
        if vals:
            metric[label] = {"min": min(vals), "max": max(vals),
                             "avg": round(sum(vals) / len(vals), 1), "n": len(vals)}

    # Sleep stages: Health Auto Export's sleep_analysis has value=null; the real
    # data (deep/core/rem/awake/total + times) lives in extra, which latest.json
    # does not keep. Pull the newest extra-bearing sample from the last 48h.
    sleep = out.get("sleep_analysis")
    if isinstance(sleep, dict):
        sleep_rows = _samples_between(now - timedelta(hours=48), now,
                                      types={"sleep_analysis"},
                                      exclude_sources=_STATUS_EXCLUDED_SOURCES)
        best = None
        for row in sorted(sleep_rows, key=lambda s: str(s.get("at") or "")):
            ex = row.get("extra")
            if isinstance(ex, dict) and ex.get("totalSleep"):
                best = ex
        if best:
            def _sleep_h(k: str) -> Optional[float]:
                try:
                    return round(float(best.get(k) or 0), 3)
                except Exception:
                    return None
            sleep["stages"] = {
                "total_h": _sleep_h("totalSleep"),
                "deep_h": _sleep_h("deep"),
                "core_h": _sleep_h("core"),
                "rem_h": _sleep_h("rem"),
                "awake_h": _sleep_h("awake"),
                "start": best.get("sleepStart"),
                "end": best.get("sleepEnd"),
            }
    return out


def sleep_period_vitals(start, end):
    """Vitals within the sleep period (onset -> wake): avg/lowest HR, HRV,
    respiration. Only computable while the samples are still within the 48h window."""
    s = _parse_dt(start)
    e = _parse_dt(end)
    if s is None or e is None:
        return {}
    def _vals(t):
        return [float(r["value"]) for r in _samples_between(s, e, types={t},
                exclude_sources=_STATUS_EXCLUDED_SOURCES) if r.get("value") is not None]
    out = {}
    hr = _vals("heart_rate")
    if hr:
        out["avg_heart_rate"] = round(sum(hr) / len(hr))
        out["lowest_heart_rate"] = round(min(hr))
    hrv = _vals("heart_rate_variability")
    if hrv:
        out["heart_rate_variability"] = round(sum(hrv) / len(hrv))
    rr = _vals("respiratory_rate")
    if rr:
        out["respiratory_rate"] = round(sum(rr) / len(rr))
    return out


def _update_sleep_history(sleep_samples) -> None:
    """Long-term daily sleep summary (30 days), deduped by sleep-onset date."""
    if not sleep_samples:
        return
    hist = {}
    if _SLEEP_HISTORY.exists():
        try:
            for line in _SLEEP_HISTORY.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if r.get("date"):
                        hist[r["date"]] = r
                except Exception:
                    continue
        except Exception:
            pass
    for s in sleep_samples:
        ex = s.get("extra")
        if not isinstance(ex, dict) or not ex.get("sleepStart"):
            continue
        d = _parse_dt(ex.get("sleepStart"))
        if d is None:
            continue
        date = d.astimezone(_TZ).strftime("%Y-%m-%d")
        rec = {"date": date}
        rec.update(ex)
        rec["vitals"] = sleep_period_vitals(ex.get("sleepStart"), ex.get("sleepEnd"))
        try:
            _lt = json.loads(_LATEST.read_text(encoding="utf-8")).get(_WRIST_TYPE)
            if isinstance(_lt, dict) and _lt.get("value") is not None:
                rec["vitals"]["wrist_temperature"] = round(float(_lt["value"]), 1)
        except Exception:
            pass
        hist[date] = rec
    cutoff = (_now().astimezone(_TZ) - timedelta(days=30)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in hist.items() if k >= cutoff}
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for k in sorted(kept):
                fh.write(_json(kept[k]) + "\n")
        os.replace(tmp, _SLEEP_HISTORY)
    except Exception:
        logger.warning("health_store: sleep_history write failed", exc_info=True)


def sleep_7day_avg():
    """Average total sleep hours over the last 7 days (fewer if less history)."""
    if not _SLEEP_HISTORY.exists():
        return None
    cutoff = (_now().astimezone(_TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
    totals = []
    try:
        for line in _SLEEP_HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("date", "") >= cutoff and r.get("totalSleep") is not None:
                    totals.append(float(r["totalSleep"]))
            except Exception:
                continue
    except Exception:
        return None
    return round(sum(totals) / len(totals), 1) if totals else None


# ------------------------------------------------------------
# Menstrual cycle
#   Raw samples only live 48h, so cycle tracking needs its own long history:
#   one record per logged day, from which periods and cycle lengths are derived.
#   Everything here is descriptive bookkeeping of what was logged in the Health
#   app — the "prediction" is just last start + average cycle length.
# ------------------------------------------------------------

def _flow_label(sample: dict[str, Any]) -> str:
    """Readable flow level. The watch app sends it in extra; other sources may
    only send the raw HKCategoryValueMenstrualFlow code."""
    extra = sample.get("extra") if isinstance(sample.get("extra"), dict) else {}
    label = str((extra or {}).get("flow") or "").strip().lower()
    if label in set(_FLOW_NAMES.values()):
        return label
    try:
        return _FLOW_NAMES.get(int(float(sample.get("value"))), "unspecified")
    except (TypeError, ValueError):
        return "unspecified"


def _load_cycle_history() -> list[dict[str, Any]]:
    if not _CYCLE_HISTORY.exists():
        return []
    hist: dict[str, dict[str, Any]] = {}
    try:
        for line in _CYCLE_HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("date"):
                    hist[r["date"]] = r
            except Exception:
                continue
    except Exception:
        logger.warning("health_store: cycle_history read failed", exc_info=True)
        return []
    return [hist[k] for k in sorted(hist)]


def _update_cycle_history(cycle_samples: list[dict[str, Any]]) -> None:
    """One record per logged day. A corrected entry (same day, new flow level)
    wins by sample time — that is why menstrual_flow is value-keyed on ingest."""
    if not cycle_samples:
        return
    hist = {r["date"]: r for r in _load_cycle_history() if r.get("date")}
    changed = False
    for s in cycle_samples:
        at = _parse_dt(s.get("at"))
        if at is None:
            continue
        date = at.astimezone(_TZ).strftime("%Y-%m-%d")
        prev = hist.get(date)
        if prev and str(prev.get("at") or "") > str(s.get("at") or ""):
            continue
        extra = s.get("extra") if isinstance(s.get("extra"), dict) else {}
        hist[date] = {
            "date": date,
            "flow": _flow_label(s),
            "cycle_start": bool((extra or {}).get("cycle_start")),
            "at": s.get("at"),
            "source": s.get("source"),
        }
        changed = True
    if not changed:
        return
    cutoff = (_now().astimezone(_TZ) - timedelta(days=_CYCLE_HISTORY_DAYS)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in hist.items() if k >= cutoff}
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for k in sorted(kept):
                fh.write(_json(kept[k]) + "\n")
        os.replace(tmp, _CYCLE_HISTORY)
    except Exception:
        logger.warning("health_store: cycle_history write failed", exc_info=True)


def _cycle_periods(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group logged bleeding days into periods. A one-day gap mid-period is
    normal (nothing logged that day), so days up to _CYCLE_PERIOD_GAP_DAYS apart
    stay in the same period; an explicit cycle_start flag always opens a new one."""
    periods: list[dict[str, Any]] = []
    for r in records:
        if r.get("flow") == "none":
            continue
        try:
            day = datetime.strptime(str(r.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        last = periods[-1] if periods else None
        if (last is not None and not r.get("cycle_start")
                and (day - last["end"]).days <= _CYCLE_PERIOD_GAP_DAYS):
            last["end"] = day
            last["logged_days"] += 1
        else:
            periods.append({"start": day, "end": day, "logged_days": 1})
    for p in periods:
        p["days"] = (p["end"] - p["start"]).days + 1
    return periods


def cycle_status() -> Optional[dict[str, Any]]:
    """Where the cycle currently stands, plus the averages it is derived from.
    Returns None when nothing has ever been logged."""
    records = _load_cycle_history()
    if not records:
        return None
    today = _now().astimezone(_TZ).date()
    flow_today = next((r.get("flow") for r in records
                       if r.get("date") == today.strftime("%Y-%m-%d")), None)
    periods = _cycle_periods(records)
    if not periods:
        return {"logged_days": len(records),
                "note": "cycle entries logged, but no bleeding days among them"}

    last = periods[-1]
    ongoing = (today - last["end"]).days <= 1
    cycle_day = (today - last["start"]).days + 1
    lengths = [(b["start"] - a["start"]).days for a, b in zip(periods, periods[1:])]
    lengths = [n for n in lengths if _CYCLE_MIN_LEN <= n <= _CYCLE_MAX_LEN]
    recent_lengths = lengths[-6:]
    recent_periods = periods[-6:]

    out: dict[str, Any] = {
        "cycle_day": cycle_day,
        "state": (f"period, day {cycle_day}" if ongoing
                  else f"day {cycle_day} of cycle"),
        "last_period_start": last["start"].strftime("%Y-%m-%d"),
        "last_period_days": last["days"],
        "avg_period_days": round(sum(p["days"] for p in recent_periods) / len(recent_periods), 1),
        "based_on_cycles": len(recent_lengths),
        "logged_days": len(records),
    }
    if flow_today:
        out["flow_today"] = flow_today
    if recent_lengths:
        avg_cycle = round(sum(recent_lengths) / len(recent_lengths))
        predicted = last["start"] + timedelta(days=avg_cycle)
        out["avg_cycle_days"] = avg_cycle
        out["predicted_next_start"] = predicted.strftime("%Y-%m-%d")
        out["days_until_next"] = (predicted - today).days
    else:
        out["note"] = ("only one period logged so far - no cycle length to "
                       "predict from yet")
    return out


# ------------------------------------------------------------
# Detail query (drives the health_detail LLM/MCP tool; see mcp/ later)
# ------------------------------------------------------------

async def execute_health_detail(arguments=None) -> str:
    """health_detail tool: for heart_rate / HRV / respiratory, sample + min/max/avg
    over a window (<=2h); same for blood oxygen and the two audio-exposure metrics
    (<=24h, they are far sparser). For sleep, query a night's stage timeline +
    period vitals + wrist temperature. For cycle, the logged periods and the
    cycle lengths derived from them. Raw samples are kept only 48h; sleep and
    cycle have their own long-term history files."""
    args = arguments or {}
    metric = str(args.get("metric") or "heart_rate").strip().lower()
    alias = {"hr": "heart_rate", "hrv": "heart_rate_variability", "sleep": "sleep_analysis",
             "respiratory": "respiratory_rate", "breath": "respiratory_rate", "resp": "respiratory_rate",
             "spo2": "blood_oxygen_saturation", "blood_oxygen": "blood_oxygen_saturation",
             "oxygen": "blood_oxygen_saturation",
             "noise": "environmental_audio_exposure", "environment": "environmental_audio_exposure",
             "environmental": "environmental_audio_exposure",
             "headphone": "headphone_audio_exposure", "headphones": "headphone_audio_exposure",
             "cycle": _CYCLE_TYPE, "period": _CYCLE_TYPE, "menstrual": _CYCLE_TYPE}
    metric = alias.get(metric, metric)
    if metric == "sleep_analysis":
        return _health_sleep_query(args)
    if metric == _CYCLE_TYPE:
        return _health_cycle_query(args)
    if metric not in _DETAIL_MAX_HOURS:
        return _json({"ok": False, "error": f"unknown metric '{metric}'; use "
                      + " / ".join(sorted(_DETAIL_MAX_HOURS)) + " / sleep / cycle"})
    # Sparse metrics (a handful of samples a day) get a wider window than the
    # dense ones, still inside the 48h raw-sample retention.
    max_hours = _DETAIL_MAX_HOURS[metric]
    now = _now()
    end = _parse_range_dt(args.get("to")) or now
    start = _parse_range_dt(args.get("from")) or (end - timedelta(hours=max_hours))
    if end - start > timedelta(hours=max_hours):
        start = end - timedelta(hours=max_hours)
    rows = _samples_between(start, end, types={metric}, exclude_sources=_STATUS_EXCLUDED_SOURCES)
    vals = [float(r["value"]) for r in rows if r.get("value") is not None]
    # A window longer than half a day needs the date to stay readable.
    tfmt = "%H:%M" if max_hours <= 12 else "%m-%d %H:%M"
    pts = [{"time": _parse_dt(r["at"]).astimezone(_TZ).strftime(tfmt), "value": round(float(r["value"]), 1)}
           for r in rows if r.get("value") is not None]
    out = {"ok": True, "metric": metric,
           "window": start.astimezone(_TZ).strftime(tfmt) + " -> " + end.astimezone(_TZ).strftime(tfmt),
           "sample_count": len(vals), "min": round(min(vals), 1) if vals else None,
           "max": round(max(vals), 1) if vals else None,
           "avg": round(sum(vals) / len(vals), 1) if vals else None, "samples": pts}
    if metric in _AUDIO_TYPES and vals:
        out["loud_sample_count"] = sum(1 for v in vals if v >= _AUDIO_LOUD_DB)
        out["loud_threshold_db"] = _AUDIO_LOUD_DB
    return _json(out)


def _health_sleep_query(args) -> str:
    date = str(args.get("date") or "").strip()
    hist = {}
    if _SLEEP_HISTORY.exists():
        try:
            for line in _SLEEP_HISTORY.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if r.get("date"):
                        hist[r["date"]] = r
                except Exception:
                    continue
        except Exception:
            pass
    if not hist:
        return _json({"ok": True, "note": "no sleep history yet"})
    if date:
        tgt = next((d for d in hist if date in d), None)
        if not tgt:
            return _json({"ok": True, "note": f"no sleep on {date} (kept 30 days)", "available_dates": sorted(hist)})
    else:
        tgt = max(hist)
    ex = hist[tgt]
    def _hm(iso):
        dd = _parse_dt(iso)
        return dd.astimezone(_TZ).strftime("%H:%M") if dd else iso
    def _h1(x):
        try:
            return round(float(x), 1)
        except (TypeError, ValueError):
            return x
    timeline = [f"{s.get('stage')} {_hm(s.get('start'))} -> {_hm(s.get('end'))}"
                for s in (ex.get("segments") or [])]
    v = ex.get("vitals") or {}
    period = {"avg_heart_rate": v.get("avg_heart_rate"), "lowest_heart_rate": v.get("lowest_heart_rate"),
              "heart_rate_variability": v.get("heart_rate_variability"), "respiratory_rate": v.get("respiratory_rate")}
    if v.get("wrist_temperature") is not None:
        period["wrist_temperature"] = v.get("wrist_temperature")
    avg7 = sleep_7day_avg()
    return _json({"ok": True, "sleep_onset_date": tgt,
                  "fell_asleep": _hm(ex.get("sleepStart")), "woke": _hm(ex.get("sleepEnd")),
                  "total_hours": _h1(ex.get("totalSleep")),
                  "stages_hours": {"deep": _h1(ex.get("deep")), "core": _h1(ex.get("core")),
                                   "rem": _h1(ex.get("rem")), "awake": _h1(ex.get("awake"))},
                  "sleep_period_vitals": period,
                  "last_7day_avg_hours": (avg7 if avg7 is not None else None),
                  "stage_timeline": timeline})


def _health_cycle_query(args) -> str:
    if _CYCLE_TYPE not in ALLOWED_TYPES:
        return _json({"ok": True, "note": "cycle tracking is not enabled on this server"})
    status = cycle_status()
    if not status:
        return _json({"ok": True, "note": "no cycle data logged yet"})
    periods = _cycle_periods(_load_cycle_history())
    recent = periods[-12:]
    starts = [p["start"] for p in periods]
    lengths = {b.strftime("%Y-%m-%d"): (b - a).days for a, b in zip(starts, starts[1:])}
    return _json({
        "ok": True,
        "current": status,
        "recent_periods": [{"start": p["start"].strftime("%Y-%m-%d"),
                            "end": p["end"].strftime("%Y-%m-%d"),
                            "days": p["days"],
                            "logged_days": p["logged_days"],
                            "days_since_previous_start": lengths.get(p["start"].strftime("%Y-%m-%d"))}
                           for p in recent],
        "history_days_kept": _CYCLE_HISTORY_DAYS,
        "note": ("derived from what was logged in the Health app - a skipped day "
                 "shows up as a shorter period, not as a missing one"),
    })


# ------------------------------------------------------------
# Compact snapshot (drives the health_now LLM/MCP tool)
# ------------------------------------------------------------

def _ago(age_min: Any) -> str:
    try:
        m = int(age_min)
    except (TypeError, ValueError):
        return "unknown"
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 1440:
        return f"{m // 60} hr ago"
    return f"{m // 1440} days ago"


def health_now(hours: float = 6) -> Any:
    """Compact snapshot for an LLM: each metric = value + freshness, present only
    when there is data. Heart rate, resting HR, HRV, respiratory rate, blood
    oxygen; last night's sleep (stages, period vitals, wrist temperature);
    today's activity totals; today's audio exposure; and where the menstrual
    cycle stands."""
    h = health_summary(hours=hours)
    if not isinstance(h, dict):
        return "connected, waiting for first samples"

    out_head: dict[str, Any] = {}
    # 应令实测(requested measurement):两小时内完成的 on-demand 测量单独一行,
    # 放在最新心率之前;与 heart_rate(被动最新值)各自独立。
    try:
        _cs = get_command_state()
        if _cs and _cs.get("status") == "done" and isinstance(_cs.get("result"), dict):
            _done = _parse_dt(_cs.get("completed_at"))
            if _done and (_now() - _done) <= timedelta(hours=2):
                _r = _cs["result"]
                _mins = int((_now() - _done).total_seconds() // 60)
                _age = f"{_mins} min ago" if _mins < 60 else f"{_mins // 60} hr {_mins % 60} min ago"
                _local = _done.astimezone(_TZ).strftime("%H:%M")
                try:
                    out_head["requested_measurement"] = (
                        f"{round(float(_r.get('heart_rate_average')))} bpm average "
                        f"({round(float(_r.get('heart_rate_minimum')))}-"
                        f"{round(float(_r.get('heart_rate_maximum')))}), "
                        f"{_r.get('sample_count')} samples, measured at {_local} ({_age})")
                except (TypeError, ValueError):
                    pass
    except Exception:
        logger.warning("health_now: requested_measurement block failed", exc_info=True)

    def _n(x):
        try:
            return round(float(x))
        except (TypeError, ValueError):
            return None

    out: dict[str, Any] = dict(out_head)
    hr = h.get("heart_rate")
    if isinstance(hr, dict) and hr.get("latest") is not None:
        out["heart_rate"] = f"{_n(hr['latest'])} bpm, {_ago(hr.get('age_min'))}"
    rhr = h.get("resting_heart_rate")
    if isinstance(rhr, dict) and rhr.get("latest") is not None:
        out["resting_heart_rate"] = f"{_n(rhr['latest'])} bpm, {_ago(rhr.get('age_min'))}"
    hrv = h.get("heart_rate_variability")
    if isinstance(hrv, dict) and hrv.get("latest") is not None:
        out["heart_rate_variability"] = f"{_n(hrv['latest'])} ms, {_ago(hrv.get('age_min'))}"
    rr = h.get("respiratory_rate")
    if isinstance(rr, dict) and rr.get("latest") is not None:
        out["respiratory_rate"] = f"{_n(rr['latest'])} breaths/min, {_ago(rr.get('age_min'))}"
    spo2 = h.get("blood_oxygen_saturation")
    if isinstance(spo2, dict) and spo2.get("latest") is not None:
        line = f"{_n(spo2['latest'])}%, {_ago(spo2.get('age_min'))}"
        low = (spo2.get("range") or {}).get("min")
        if low is not None and low < float(spo2["latest"]):
            line += f" (low {_n(low)}% in range)"
        out["blood_oxygen_saturation"] = line

    def _h1(x):
        try:
            return round(float(x), 1)
        except (TypeError, ValueError):
            return x

    sl = h.get("sleep_analysis")
    if isinstance(sl, dict) and isinstance(sl.get("stages"), dict):
        st = sl["stages"]
        v = sleep_period_vitals(st.get("start"), st.get("end"))

        def _hm(iso):
            d = _parse_dt(iso)
            return d.astimezone(_TZ).strftime("%H:%M") if d else None

        sleep_out: dict[str, Any] = {
            "fell_asleep": _hm(st.get("start")),
            "woke": _hm(st.get("end")),
            "total_hours": _h1(st.get("total_h")),
            "stages_hours": {"deep": _h1(st.get("deep_h")), "core": _h1(st.get("core_h")),
                             "rem": _h1(st.get("rem_h")), "awake": _h1(st.get("awake_h"))},
        }
        if v.get("avg_heart_rate") is not None:
            sleep_out["avg_heart_rate"] = f"{v['avg_heart_rate']} bpm"
        if v.get("lowest_heart_rate") is not None:
            sleep_out["lowest_heart_rate"] = f"{v['lowest_heart_rate']} bpm"
        if v.get("heart_rate_variability") is not None:
            sleep_out["heart_rate_variability"] = f"{v['heart_rate_variability']} ms"
        if v.get("respiratory_rate") is not None:
            sleep_out["respiratory_rate"] = f"{v['respiratory_rate']} breaths/min"
        wt = h.get(_WRIST_TYPE)
        if isinstance(wt, dict) and wt.get("latest") is not None:
            vb = wt.get("vs_baseline")
            _t = f"{float(wt['latest']):.1f}°C"
            sleep_out["wrist_temperature"] = f"{_t} ({vb})" if vb else _t
        _avg7 = sleep_7day_avg()
        if _avg7 is not None:
            sleep_out["last_7day_avg_hours"] = _avg7
        out["sleep"] = sleep_out

    def _int(x):
        try:
            return int(round(float(x)))
        except (TypeError, ValueError):
            return x

    for key, unit in [("step_count", ""), ("active_energy_burned", " kcal"),
                      ("apple_exercise_time", " min"), ("flights_climbed", "")]:
        m = h.get(key)
        if isinstance(m, dict) and m.get("today_total") is not None:
            out[key] = f"{_int(m['today_total'])}{unit}"
    dist = h.get("walking_running_distance")
    if isinstance(dist, dict) and dist.get("today_total") is not None:
        try:
            out["walking_running_distance"] = f"{round(float(dist['today_total']) * 1.609344, 1)} km"
        except (TypeError, ValueError):
            pass

    for key in ("environmental_audio_exposure", "headphone_audio_exposure"):
        m = h.get(key)
        if not isinstance(m, dict) or m.get("today_max") is None:
            continue
        line = f"today max {m['today_max']} dB, avg {m.get('today_avg')} dB"
        loud = m.get("today_loud_n") or 0
        if loud:
            line += f" ({loud} of {m.get('today_n')} samples at or above {int(_AUDIO_LOUD_DB)} dB)"
        out[key] = line

    if _CYCLE_TYPE in ALLOWED_TYPES:
        try:
            cyc = cycle_status()
        except Exception:
            logger.warning("health_now: cycle block failed", exc_info=True)
            cyc = None
        if cyc:
            block: dict[str, Any] = {}
            if cyc.get("state"):
                block["status"] = cyc["state"]
            if cyc.get("flow_today"):
                block["flow_today"] = cyc["flow_today"]
            if cyc.get("last_period_start"):
                block["last_period_start"] = cyc["last_period_start"]
            if cyc.get("predicted_next_start"):
                _d = cyc["days_until_next"]
                _when = (f"in {_d} days" if _d > 0 else
                         "today" if _d == 0 else f"{-_d} days overdue")
                block["predicted_next_start"] = (
                    f"{cyc['predicted_next_start']} ({_when}, "
                    f"est. from {cyc['based_on_cycles']} cycles of "
                    f"~{cyc['avg_cycle_days']} days)")
            elif cyc.get("note"):
                block["note"] = cyc["note"]
            if block:
                out["menstrual_cycle"] = block
    return out or "connected, waiting for first samples"


# ------------------------------------------------------------
# 按需实时测量 · 指令通道 (on-demand measurement command channel)
#   command.json 单槽:MCP 工具下指令 → 手表拉取 → 跑一段短 workout
#   session 实测 → 回执结果。状态机 pending → seen → done;
#   超过 TTL 未完成自动 expired。手表侧无指令时零动作。
# ------------------------------------------------------------

_COMMAND_FILE = _DATA_DIR / "command.json"
_COMMAND_TTL_MIN = int(os.environ.get("HEALTH_COMMAND_TTL_MIN", "30") or 30)
_MEASURE_DURATION_S = int(os.environ.get("HEALTH_MEASURE_DURATION_S", "30") or 30)


def _write_command(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_json(data))
        os.replace(tmp, _COMMAND_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_command() -> Optional[dict[str, Any]]:
    try:
        return json.loads(_COMMAND_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def create_command(command: str = "measure_heart_rate") -> dict[str, Any]:
    """下指令。单槽:新指令覆盖旧指令。"""
    import uuid
    data = {
        "command_id": f"cmd_{uuid.uuid4().hex[:12]}",
        "command": command,
        "status": "pending",
        "requested_at": _iso(_now()),
        "duration_seconds": _MEASURE_DURATION_S,
        "result": None,
    }
    _write_command(data)
    return data


def _expire_if_stale(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("status") in ("pending", "seen"):
        ts = _parse_dt(data.get("requested_at"))
        if ts and (_now() - ts) > timedelta(minutes=_COMMAND_TTL_MIN):
            data["status"] = "expired"
            _write_command(data)
    return data


def fetch_pending_command(mark_seen: bool = True) -> Optional[dict[str, Any]]:
    """手表拉指令(GET /command 的执行体)。pending/seen 且未过期才返回。"""
    data = _read_command()
    if not data:
        return None
    data = _expire_if_stale(data)
    if data.get("status") not in ("pending", "seen"):
        return None
    if mark_seen and data.get("status") == "pending":
        data["status"] = "seen"
        data["seen_at"] = _iso(_now())
        _write_command(data)
    return data


def complete_command(command_id: str, result: dict[str, Any]) -> bool:
    """手表回执测量结果(POST /command/result 的执行体)。"""
    data = _read_command()
    if not data or data.get("command_id") != command_id:
        return False
    data["status"] = "done"
    data["completed_at"] = _iso(_now())
    data["result"] = result
    _write_command(data)
    return True


def get_command_state() -> Optional[dict[str, Any]]:
    """MCP 工具轮询用。"""
    data = _read_command()
    if not data:
        return None
    return _expire_if_stale(data)


async def execute_measure_heart_rate() -> str:
    """measure_heart_rate 工具执行体:下指令 → 等手表回执(最多 90 秒)。

    本项目不内置推送:工具只把指令放进 command.json,由 CollarWatch 自己
    捡走(app 开着时前台每 15s 轮询一次;后台醒来也会顺路查)。想要
    "叫人来开表"的通知,请在你自己的 ingest 服务里挂,渠道随意。
    """
    import asyncio
    cmd = create_command("measure_heart_rate")
    for _ in range(45):
        await asyncio.sleep(2)
        state = get_command_state()
        if state and state.get("command_id") == cmd["command_id"] \
                and state.get("status") == "done":
            r = state.get("result") or {}
            done_dt = _parse_dt(state.get("completed_at"))
            measured_local = (done_dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")
                              + " (server tz)") if done_dt else state.get("completed_at")
            return _json({
                "status": "measured",
                "heart_rate_average": r.get("heart_rate_average"),
                "heart_rate_minimum": r.get("heart_rate_minimum"),
                "heart_rate_maximum": r.get("heart_rate_maximum"),
                "sample_count": r.get("sample_count"),
                "measured_at": measured_local,
            })
    return _json({
        "status": "pending",
        "note": ("no result yet - the watch has not executed the command "
                 "(it polls every 15s while the app is open, or on its next "
                 "background wake). The command stays valid for "
                 f"{_COMMAND_TTL_MIN} minutes; once executed the measurement "
                 "appears in health_now."),
    })
