#!/usr/bin/env python3
"""断流告警:太久没收到新样本就叫一声。

为什么需要它:采集端一旦停了 —— 签名过期、手机关机、权限被撤、后台被系统掐 ——
表现都是**静默停传**。读数据的那一端只会看到"没有新数据",而不是"我看不到她了",
于是当作一切正常。这是这套链路最危险的失败方式,比任何一次报错都危险。

它只读 latest.json(store_samples 原子写出的那份),不碰采集与存储逻辑。

用法:
    python -m server.stale_alert --once     # 查一次,给退出码(0 正常 / 1 断流)
    python -m server.stale_alert --loop     # 常驻,按间隔自己查

环境变量:
    HEALTH_DATA_DIR            数据目录(与 health_store 一致)
    HEALTH_STALE_MINUTES       多久没数据算断流,默认 90
    HEALTH_STALE_TYPES         盯哪些类型(逗号分隔),默认 heart_rate
    HEALTH_ALERT_WEBHOOK       告警发到哪(POST JSON;Bark 的 https://api.day.app/<key> 直接可用)
    HEALTH_ALERT_COOLDOWN_MIN  同一次断流最多多久重复叫一次,默认 180
    HEALTH_STALE_INTERVAL_MIN  --loop 的检查间隔,默认 15
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_DATA_DIR = Path(os.environ.get("HEALTH_DATA_DIR", "./data/health")).expanduser()
_LATEST = _DATA_DIR / "latest.json"
_STATE = _DATA_DIR / "stale_alert.json"

_THRESHOLD_MIN = float(os.environ.get("HEALTH_STALE_MINUTES", "90") or 90)
_COOLDOWN_MIN = float(os.environ.get("HEALTH_ALERT_COOLDOWN_MIN", "180") or 180)
_INTERVAL_MIN = float(os.environ.get("HEALTH_STALE_INTERVAL_MIN", "15") or 15)
_WEBHOOK = os.environ.get("HEALTH_ALERT_WEBHOOK", "").strip()

_WATCHED = [t.strip() for t in
            (os.environ.get("HEALTH_STALE_TYPES", "heart_rate") or "").split(",")
            if t.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def newest_sample_at() -> tuple[Optional[datetime], Optional[str]]:
    """被盯住的类型里,最新那条样本的时间。返回 (时间, 类型名)。"""
    if not _LATEST.exists():
        return None, None
    try:
        latest = json.loads(_LATEST.read_text(encoding="utf-8"))
    except Exception:
        return None, None

    best: Optional[datetime] = None
    best_type: Optional[str] = None
    for stype, row in (latest or {}).items():
        if _WATCHED and stype not in _WATCHED:
            continue
        if not isinstance(row, dict):
            continue
        at = _parse(row.get("at"))
        if at and (best is None or at > best):
            best, best_type = at, stype
    return best, best_type


def _load_state() -> dict[str, Any]:
    if not _STATE.exists():
        return {}
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _notify(title: str, body: str) -> bool:
    """发一条告警。没配 webhook 就只打到 stdout —— 至少日志里留得下。"""
    print(f"[stale_alert] {title} | {body}", flush=True)
    if not _WEBHOOK:
        return False
    payload = json.dumps({"title": title, "body": body}).encode("utf-8")
    req = urllib.request.Request(
        _WEBHOOK, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"[stale_alert] webhook 发送失败:{exc}", flush=True)
        return False


def check(*, notify: bool = True) -> dict[str, Any]:
    """查一次。返回 {stale, age_minutes, last_at, last_type, alerted}。"""
    at, stype = newest_sample_at()
    now = _now()
    age_min = None if at is None else (now - at).total_seconds() / 60
    # 从没收到过数据也算断流:刚上线时就该被叫一声,而不是静悄悄等着
    stale = age_min is None or age_min > _THRESHOLD_MIN

    state = _load_state()
    was_alerting = bool(state.get("alerting"))
    last_alert = _parse(state.get("last_alert_at"))
    alerted = False

    if stale and notify:
        cooled = (last_alert is None
                  or now - last_alert > timedelta(minutes=_COOLDOWN_MIN))
        if not was_alerting or cooled:
            if age_min is None:
                body = "还没收到过任何健康数据。采集端可能从没跑起来。"
            else:
                body = (f"已经 {int(age_min)} 分钟没有新数据了"
                        f"（最后一条 {stype} 在 {at.astimezone().strftime('%m-%d %H:%M')}）。"
                        "先看采集端:签名是否过期、手机是否开着、后台刷新是否被关。")
            _notify("健康数据断流", body)
            alerted = True
            state["last_alert_at"] = now.isoformat()
        state["alerting"] = True
    elif not stale:
        if was_alerting and notify:
            _notify("健康数据恢复了", f"刚收到新的 {stype}，链路恢复正常。")
            alerted = True
        state["alerting"] = False
        state.pop("last_alert_at", None)

    _save_state(state)

    return {"stale": stale,
            "age_minutes": None if age_min is None else round(age_min, 1),
            "last_at": None if at is None else at.isoformat(),
            "last_type": stype,
            "threshold_minutes": _THRESHOLD_MIN,
            "alerted": alerted}


def main(argv: list[str]) -> int:
    loop = "--loop" in argv
    result = check()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not loop:
        return 1 if result["stale"] else 0

    while True:
        time.sleep(max(60.0, _INTERVAL_MIN * 60))
        try:
            print(json.dumps(check(), ensure_ascii=False), flush=True)
        except Exception as exc:  # 守护进程不能因为一次异常就退出
            print(f"[stale_alert] 检查出错:{exc}", flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
