"""scout.py —— 选片：按「视频详细设置」的三块配额 + 质量门槛挑候选。

一、质量门槛（关注 UP 除外）
    粉丝 > min_up_fans、播放 > min_play 才有资格进候选。不过关的直接丢，
    不给本地模型"看着标题猜"的机会 —— 4B 判断一条视频要花十几秒和上万 token。

二、三块配额
    AI技术 / AI二创 / 其他视频（可自定义多条），每块有自己的额度。
    三块之和超过「一天最多浏览」时，运行时按 **AI技术 > AI二创 > 其他** 收敛，
    前端另外会提示用户去调大上限（不在后端偷偷改用户填的值）。

三、剩余配额
    三块没填满时，剩下的交给本地模型自主浏览 —— 从热门里挑**非 AI** 视频，
    让账号不至于只活在 AI 信息茧房里。同样要过质量门槛。

四、跨轮记账
    「今天已经看了几条」记在 budget.py，跨 run 累计。额度用尽时 scout 直接返回空，
    agent 会立刻收尾 —— 不再出现"每轮重新巡检 12 条、全是看过"的空转。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import budget
from .bili_mcp import BiliPlusBridge
from .config import default_bili_plus_home, default_home
from .memory import MemoryStore
from .util import atomic_write_json, clip, normalize_bvid, normalize_mid, parse_timestamp, read_json, ts_text

# 粉丝数缓存 7 天过期：UP 主一天涨几千粉的情况很少，没必要每次重查。
FANS_TTL = 7 * 86400


def _db_uri(path: Path) -> str:
    # sqlite3 URI needs forward slashes and percent-encoded special characters.
    return "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"


def load_seen_bvids() -> set[str]:
    path = default_bili_plus_home() / "bili_plus.db"
    if not path.exists():
        return set()
    seen: set[str] = set()
    try:
        conn = sqlite3.connect(_db_uri(path), uri=True, timeout=3)
        try:
            for row in conn.execute("SELECT bvid FROM videos"):
                bvid = normalize_bvid(row[0])
                if bvid:
                    seen.add(bvid)
            for row in conn.execute("SELECT bvid FROM drafts WHERE status='posted'"):
                bvid = normalize_bvid(row[0])
                if bvid:
                    seen.add(bvid)
        finally:
            conn.close()
    except sqlite3.Error:
        return seen
    return seen


def load_commented_bvids() -> set[str]:
    """已经**发过评论**的视频。

    ⚠️ 不能拿 `videos` 表当"看过就不许评论"的依据：
       插件只要调过一次 video_info 就会往 videos 表写一行，
       于是任何浏览过的视频都会被永久拉黑，一天之后就没得评了（实测踩到）。
       真正不能再评的只有"已经评过"的那些。
    """
    path = default_bili_plus_home() / "bili_plus.db"
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        conn = sqlite3.connect(_db_uri(path), uri=True, timeout=3)
        try:
            for row in conn.execute("SELECT bvid FROM drafts WHERE status='posted'"):
                bvid = normalize_bvid(row[0])
                if bvid:
                    out.add(bvid)
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    return out


def _status_summary(status: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "ok",
        "logged_in",
        "uname",
        "mid",
        "paused",
        "pause_reason",
        "posts_24h",
        "quota_left",
        "drafts_pending",
        "drafts_posted",
        "write_enabled",
        "limits",
        "videos",
    }
    return {k: v for k, v in status.items() if k in keep}


# ─────────────────────────── 粉丝数缓存 ───────────────────────────


def _fans_path() -> Path:
    return default_home() / "up_fans.json"


def _fans_load() -> dict[str, Any]:
    data = read_json(_fans_path(), {})
    return data if isinstance(data, dict) else {}


def _fans_save(data: dict[str, Any]) -> None:
    try:
        atomic_write_json(_fans_path(), data)
    except OSError:
        pass


async def _fans_of(
    bridge: BiliPlusBridge,
    mid: int,
    cache: dict[str, Any],
    logger: logging.Logger,
) -> int | None:
    """UP 粉丝数。命中缓存就完全不发请求。"""
    if not mid:
        return None
    key = str(mid)
    row = cache.get(key)
    now = int(time.time())
    if isinstance(row, dict) and row.get("fans") is not None and now - int(row.get("at") or 0) < FANS_TTL:
        return int(row["fans"])
    try:
        result = await bridge.call("user_info", {"mid": int(mid)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("scout user_info(%s) failed: %s", mid, exc)
        return None
    if result.get("ok") is False:
        return None
    fans = result.get("fans")
    if not isinstance(fans, (int, float)):
        return None
    cache[key] = {"fans": int(fans), "at": now, "name": clip(result.get("name") or "", 40)}
    return int(fans)


# ─────────────────────────── 候选行 ───────────────────────────


def _video_row(
    video: dict[str, Any], source: str, seen: set[str], fallback_mid: int = 0,
    commented: set[str] | None = None,
) -> dict[str, Any] | None:
    bvid = normalize_bvid(video.get("bvid"))
    if not bvid:
        return None
    pubdate = parse_timestamp(video.get("pubdate") or video.get("created"))
    title = clip(video.get("title") or "", 140)
    author = clip(video.get("author") or "", 60)
    author_mid = normalize_mid(
        video.get("author_mid") or video.get("mid") or video.get("uid")
    ) or int(fallback_mid or 0)
    duration_text = (
        video.get("duration_text") or video.get("length") or video.get("duration") or ""
    )
    if isinstance(duration_text, (int, float)):
        duration_text = f"{int(duration_text) // 60}:{int(duration_text) % 60:02d}"
    play = video.get("play")
    play = int(play) if isinstance(play, (int, float)) else None
    return {
        "bvid": bvid,
        "title": title,
        "author": author,
        "author_mid": author_mid,
        "pubdate": pubdate,
        "pubdate_text": ts_text(pubdate),
        "duration_text": str(duration_text or ""),
        "play": play,
        "fans": None,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "source": source,
        "bucket": "",
        "exempt": False,
        "seen": bvid in seen,
        "commented": bvid in (commented or set()),
    }


def is_fresh(pubdate: Any, fresh_seconds: int, now: int, include_old: bool = False) -> bool:
    """这条视频算不算"新的"。

    `fresh_seconds <= 0` 表示**不限新旧** —— 设置页「只看最近几天」填 0 就是这个意思。
    以前 0 会被当成"2 天"，页面上写着不限、实际只给两天，属于提示与实现打架。
    """
    if include_old or not pubdate:
        return True
    if fresh_seconds <= 0:
        return True
    try:
        return now - int(pubdate) <= fresh_seconds
    except (TypeError, ValueError):
        return True


def _source_priority(source: str) -> int:
    if source.startswith("watch"):
        return 0
    if source.startswith("bucket:") or source.startswith("keyword"):
        return 1
    if source.startswith("explore"):
        return 3
    if source == "popular":
        return 2
    return 3


def bucket_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    """三块配额。顺序即优先级：AI技术 > AI二创 > 其他。"""
    specs: list[dict[str, Any]] = []
    tech = [str(x).strip() for x in (config.get("ai_tech_keywords") or []) if str(x).strip()]
    if int(config.get("ai_tech_quota") or 0) > 0 and tech:
        specs.append({
            "key": "ai_tech", "name": "AI技术", "kind": "tech",
            "keywords": tech[:4], "quota": int(config["ai_tech_quota"]),
        })
    fun = [str(x).strip() for x in (config.get("ai_fun_keywords") or []) if str(x).strip()]
    if int(config.get("ai_fun_quota") or 0) > 0 and fun:
        specs.append({
            "key": "ai_fun", "name": "AI二创", "kind": "fun",
            "keywords": fun[:4], "quota": int(config["ai_fun_quota"]),
        })
    for pref in config.get("other_prefs") or []:
        quota = int(pref.get("quota") or 0)
        words = [str(x).strip() for x in (pref.get("keywords") or []) if str(x).strip()]
        if quota <= 0 or not words:
            continue
        specs.append({
            "key": f"other:{pref.get('name')}", "name": str(pref.get("name") or "其他"),
            "kind": "other", "keywords": words[:4], "quota": quota,
        })
    return specs


def _is_ai_title(title: str, config: dict[str, Any]) -> bool:
    text = str(title or "").lower()
    words: list[str] = []
    for key in ("ai_tech_keywords", "ai_fun_keywords"):
        words.extend(str(x) for x in (config.get(key) or []))
    words.append("ai")
    return any(w.lower() in text for w in words if w)


async def scout(
    bridge: BiliPlusBridge,
    config: dict[str, Any],
    memory: MemoryStore,
    logger: logging.Logger | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    log = logger or logging.getLogger(__name__)
    status = await bridge.call("status", {})
    if status.get("ok") is False:
        raise RuntimeError(f"bili-plus status 失败：{status.get('error') or status}")
    status_summary = _status_summary(status)
    memory.set_bili_status(status_summary)
    if not status.get("logged_in"):
        raise RuntimeError("bili-plus 未登录 B站，请先在插件设置页扫码登录。")
    if status.get("paused"):
        log.warning("bili-plus is paused: %s", status.get("pause_reason"))

    seen = load_seen_bvids()
    commented = load_commented_bvids()
    now = int(time.time())
    fresh_seconds = int(config.get("fresh_days", 2)) * 86400
    include_old = bool(config.get("include_old"))
    daily_max = int(config.get("daily_video_max") or 6)

    # ── 今天的额度还够吗 ──
    daily = budget.summary(daily_max)
    remaining = daily["remaining"]
    memory.set_bucket_plan({"daily_max": daily_max, "remaining": remaining, "buckets": []})
    if remaining <= 0:
        log.info("daily video budget exhausted: %s/%s", daily["viewed"], daily_max)
        return [], status_summary

    fans_cache = _fans_load()
    mins_fans = int(config.get("min_up_fans") or 0)
    min_play = int(config.get("min_play") or 0)

    def fresh(row: dict[str, Any]) -> bool:
        return is_fresh(row.get("pubdate"), fresh_seconds, now, include_old)

    async def try_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await bridge.call(name, args)
        except Exception as exc:  # noqa: BLE001
            log.warning("scout %s failed: %s", name, exc)
            return {"ok": False, "error": str(exc)}
        if result.get("ok") is False:
            log.warning("scout %s returned error: %s", name, result.get("error"))
        return result

    async def gate(row: dict[str, Any]) -> tuple[bool, str]:
        """质量门槛。关注 UP 直接放行（exempt）。"""
        if row.get("exempt"):
            return True, "关注UP"
        play = row.get("play")
        if min_play > 0 and isinstance(play, int) and play < min_play:
            return False, f"播放{play}<{min_play}"
        mid = int(row.get("author_mid") or 0)
        fans = await _fans_of(bridge, mid, fans_cache, log)
        if fans is None:
            # 查不到粉丝数（接口抖动 / 私密）不硬杀，但排到后面，且标出来。
            row["fans_unknown"] = True
            return True, "粉丝未知"
        row["fans"] = fans
        if mins_fans > 0 and fans < mins_fans:
            return False, f"粉丝{fans}<{mins_fans}"
        return True, ""

    picked: list[dict[str, Any]] = []
    picked_ids: set[str] = set()

    # ── ② 三块配额 ──
    specs = bucket_specs(config)
    plan: list[dict[str, Any]] = []
    for spec in specs:
        plan.append({"key": spec["key"], "name": spec["name"], "quota": spec["quota"], "got": 0})

    total_cap = remaining
    for spec, plan_row in zip(specs, plan):
        if len(picked) >= total_cap:
            break
        want = min(spec["quota"], total_cap - len(picked))
        got = 0
        raw: list[dict[str, Any]] = []
        raw_ids: set[str] = set()
        for keyword in spec["keywords"][:3]:
            # 搜两次：
            #   totalrank —— 相关性 + 热度，容易搜到几天前的老热门（会被 fresh_days 砍掉）
            #   pubdate   —— 最新，播放量往往还没起来（会被 min_play 砍掉）
            # 单用任何一个都容易一个候选都剩不下，两个合起来才是"最近又够热"。
            for order in ("totalrank", "pubdate"):
                result = await try_call(
                    "search", {"keyword": keyword, "num": 8, "order": order}
                )
                for video in result.get("videos") or []:
                    if not isinstance(video, dict):
                        continue
                    row = _video_row(video, f"bucket:{spec['key']}", seen, commented=commented)
                    if not row or row["bvid"] in raw_ids or row["bvid"] in picked_ids:
                        continue
                    if not fresh(row):
                        continue
                    raw.append(row)
                    raw_ids.add(row["bvid"])
        # 播放量高的优先过门，省粉丝查询次数
        raw.sort(key=lambda r: -(r.get("play") or 0))
        for row in raw:
            if got >= want:
                break
            ok, why = await gate(row)
            if not ok:
                log.info("gate reject %s (%s)", row["bvid"], why)
                continue
            row["bucket"] = spec["name"]
            row["gate"] = why or "通过"
            picked.append(row)
            picked_ids.add(row["bvid"])
            got += 1
        plan_row["got"] = got

    # ── ③ 关注 UP：不受质量门槛限制，但**只吃剩下的额度** ──
    #
    # ⚠️ 顺序很重要。上一版把关注 UP 放在最前面，结果 3 个 UP × 6 条就把
    #    「一天最多浏览」全吃光，三块配额永远是 0，等于用户配的板块完全没生效。
    #    现在的语义是：板块配额先保证 → 关注 UP 补剩下的 → 最后才是热门自主浏览。
    watch_plan = {"quota": 0, "got": 0}
    if len(picked) < total_cap:
        watch_plan["quota"] = total_cap - len(picked)
        before = len(picked)
        for mid in list(config.get("watch_mids") or [])[:3]:
            mid = normalize_mid(mid)
            if not mid or len(picked) >= total_cap:
                continue
            result = await try_call("user_videos", {"mid": mid, "num": 6, "order": "pubdate"})
            for video in result.get("videos") or []:
                if len(picked) >= total_cap:
                    break
                if not isinstance(video, dict):
                    continue
                row = _video_row(video, f"watch:{mid}", seen, fallback_mid=mid, commented=commented)
                if not row or row["bvid"] in picked_ids or row["bvid"] in {p["bvid"] for p in picked}:
                    continue
                if not fresh(row):
                    continue
                row["exempt"] = True
                row["bucket"] = "关注UP"
                row["gate"] = "关注UP豁免"
                if not row.get("author"):
                    cache_row = fans_cache.get(str(mid)) or {}
                    row["author"] = clip(cache_row.get("name") or "", 60)
                row["fans"] = await _fans_of(bridge, mid, fans_cache, log)
                picked.append(row)
                picked_ids.add(row["bvid"])
        watch_plan["got"] = len(picked) - before

    # ── ④ 剩余配额：自主浏览非 AI 内容 ──
    explore_plan = {"quota": 0, "got": 0}
    if len(picked) < total_cap:
        explore_plan["quota"] = total_cap - len(picked)
        before = len(picked)
        result = await try_call("popular", {"num": 30})
        raw = []
        seen_pop: set[str] = set()
        for video in result.get("videos") or []:
            if not isinstance(video, dict):
                continue
            row = _video_row(video, "explore:popular", seen, commented=commented)
            if not row or row["bvid"] in picked_ids or row["bvid"] in seen_pop:
                continue
            if not fresh(row):
                continue
            raw.append(row)
            seen_pop.add(row["bvid"])
        # 先非 AI，再 AI —— 剩下的额度优先补信息茧房外面的内容
        raw.sort(key=lambda r: (1 if _is_ai_title(r.get("title") or "", config) else 0, -(r.get("play") or 0)))
        for row in raw:
            if len(picked) >= total_cap:
                break
            ok, why = await gate(row)
            if not ok:
                continue
            row["bucket"] = "自主浏览"
            row["gate"] = why or "通过"
            picked.append(row)
            picked_ids.add(row["bvid"])
        explore_plan["got"] = len(picked) - before

    _fans_save(fans_cache)

    # ── 排序：关注 UP > 过滤后的正式候选；seen 的沉底 ──
    picked.sort(
        key=lambda row: (
            1 if row.get("commented") else 0,
            1 if row.get("seen") else 0,
            1 if row.get("fans_unknown") else 0,
            _source_priority(row.get("source") or ""),
            -(row.get("pubdate") or 0),
        )
    )
    limit = int(config.get("candidate_limit", 12))
    rows = picked[:limit]

    memory.set_candidates(rows)
    memory.set_bucket_plan({
        "daily_max": daily_max,
        "remaining": remaining,
        "quota_wanted": int(config.get("quota_wanted") or 0),
        "quota_overflow": bool(config.get("quota_overflow")),
        "buckets": plan,
        "watch": watch_plan,
        "explore": explore_plan,
        "gate": {"min_up_fans": mins_fans, "min_play": min_play},
    })
    log.info(
        "scout: %s candidates (daily %s/%s, gate fans>%s play>%s)",
        len(rows), daily["viewed"], daily_max, mins_fans, min_play,
    )
    return rows, status_summary
