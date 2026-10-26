"""budget.py —— 一天的视频浏览额度。

「视频详细设置」里的**一天最多浏览**是硬闸：不管分几轮跑，一天看的视频总数不能超过它。
这里只做记账，不做判断 —— 谁该看、看几条由 scout / agent 决定。

为什么单独一个文件而不是塞进 run 目录：额度必须**跨 run 累计**。
上一版把「看了几条」记在单轮 memory 里，于是每轮都重新有 12 条候选，
一晚上能把同一批视频反复"看"十几遍，白烧本地算力。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .config import default_home
from .util import atomic_write_json, read_json


def _path() -> Path:
    return default_home() / "daily.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _blank(today: str) -> dict[str, Any]:
    return {"date": today, "viewed": [], "comments": 0, "runs": 0, "updated_at": 0}


def load() -> dict[str, Any]:
    """读今天的账。跨天自动归零（不删文件，直接换一份空账）。"""
    today = _today()
    data = read_json(_path(), {})
    if not isinstance(data, dict) or data.get("date") != today:
        return _blank(today)
    data.setdefault("viewed", [])
    data.setdefault("comments", 0)
    data.setdefault("runs", 0)
    return data


def save(data: dict[str, Any]) -> None:
    data["date"] = data.get("date") or _today()
    data["updated_at"] = int(time.time())
    try:
        atomic_write_json(_path(), data)
    except OSError:
        pass


def remaining(daily_max: int) -> int:
    """今天还能看几条。"""
    data = load()
    return max(0, int(daily_max) - len(data.get("viewed") or []))


def note_viewed(bvids: list[str]) -> dict[str, Any]:
    data = load()
    seen = list(data.get("viewed") or [])
    for bvid in bvids:
        bvid = str(bvid or "").strip()
        if bvid and bvid not in seen:
            seen.append(bvid)
    data["viewed"] = seen
    save(data)
    return data


def note_run() -> dict[str, Any]:
    data = load()
    data["runs"] = int(data.get("runs") or 0) + 1
    save(data)
    return data


def note_comment(count: int = 1) -> dict[str, Any]:
    data = load()
    data["comments"] = int(data.get("comments") or 0) + max(0, int(count))
    save(data)
    return data


def summary(daily_max: int) -> dict[str, Any]:
    data = load()
    viewed = list(data.get("viewed") or [])
    return {
        "date": data.get("date"),
        "daily_max": int(daily_max),
        "viewed": len(viewed),
        "remaining": max(0, int(daily_max) - len(viewed)),
        "bvids": viewed,
        "comments": int(data.get("comments") or 0),
        "runs": int(data.get("runs") or 0),
    }
