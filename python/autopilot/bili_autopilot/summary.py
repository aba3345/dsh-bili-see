"""summary.py —— 视频总结：本地先整合，云端只读压缩结果。

为什么要有这一步
----------------
云端（DeepSeek）如果直接读 run 的原始记忆，一个 3 条视频的 run 大概是
10k~16k 字符（字幕摘录 + 热评 + 思维链要点 + 工具流水）。本地模型跑一轮
只要几十秒、不花钱，让它先把这些压成 700~1200 字的结论，云端就只用读一次。

代价：多一次本地推理（通常 3~8 秒，1500~4000 prompt token）。
收益：云端 prompt 从 1 万多字符降到一千多，**这才是省的地方**。
所以「省 token」指的是云端侧，不是本地侧 —— 本地那点 token 本来就不要钱。

降级：本地模型没起来、超时、返回空，就直接走规则拼装（不编造任何字段），
并标记 degraded=True，前端/工具结果里能看出来。
"""
from __future__ import annotations

import logging
from typing import Any

from . import budget
from .memory import MemoryStore
from .ollama_client import OllamaClient, OllamaError
from .prompts import _human, summary_prompt
from .util import clip


def _rule_based(memory: MemoryStore, plan: dict[str, Any]) -> str:
    """不调模型也能用的兜底：只搬记忆里的既有事实，一个字都不编。"""
    lines: list[str] = []
    videos = [v for v in (memory.data.get("videos") or []) if isinstance(v, dict)]
    for i, v in enumerate(videos, 1):
        bits = [f"{i}. {v.get('bvid')}", f"《{clip(v.get('title'), 60)}》"]
        up = clip(v.get("author"), 20)
        if up:
            bits.append(f"UP {up}")
        play = v.get("play")
        if isinstance(play, int):
            bits.append(f"播放 {_human(play)}")
        action = v.get("comment_action") or "inspected"
        bits.append({"comment": "已评论", "skip": "跳过", "inspected": "只看不评"}.get(action, action))
        if v.get("comment_text"):
            bits.append(f"评论：{v['comment_text']}")
        if v.get("summary"):
            bits.append(clip(v.get("summary"), 100))
        lines.append("｜".join(bits))
    if not lines:
        return "这一轮没有记录到视频。"
    tail = (
        f"\n今天已看 {plan.get('viewed') or 0} 条 / 上限 {plan.get('daily_max') or 0} 条。"
    )
    return "\n".join(lines) + tail


def _comment_facts(videos: list[dict[str, Any]]) -> str:
    """本轮真正的评论事实：发出去的照抄原文，没发就说没发。"""
    posted = [v for v in videos if v.get("comment_status") == "posted" and v.get("comment_text")]
    if not posted:
        return "评论：本轮未评论（只浏览，没有发出去）。"
    return "\n".join(
        f"评论：{v.get('bvid')} 下发出「{v.get('comment_text')}」"
        + (f"（rpid {(v.get('post_result') or {}).get('rpid')}）" if (v.get("post_result") or {}).get("rpid") else "")
        for v in posted[:3]
    )


def _fix_comment_section(text: str, videos: list[dict[str, Any]]) -> tuple[str, bool]:
    """把总结里的「评论：…」段替换成本轮的确定事实。

    保留模型写的 视频段 / 推荐段，只纠正这一段 —— 这段是事实陈述，不能靠模型。
    """
    import re

    fact = _comment_facts(videos)
    lines = str(text or "").splitlines()
    idx = next((i for i, line in enumerate(lines) if line.strip().startswith("评论：")), -1)
    if idx < 0:
        # 完全没有评论段：插在推荐段之前（没有推荐就追加到末尾）
        rec = next((i for i, line in enumerate(lines) if line.strip().startswith("推荐：")), -1)
        if rec >= 0:
            lines[rec:rec] = [fact, ""]
        else:
            lines.extend(["", fact])
        return "\n".join(lines).strip(), True

    # 收集这一段（连续的 评论： 行）
    end = idx
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("评论："):
        end += 1
    old = "\n".join(lines[idx:end + 1]).strip()
    if old == fact.strip():
        return "\n".join(lines).strip(), False
    return "\n".join(lines[:idx] + [fact] + lines[end + 1:]).strip(), True


def _strip_thinking(raw: str) -> str:
    """思维链里捞正文：取最后一个明显的「结论」段落。捞不到就返回空。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    for marker in ("总结：", "整理结果：", "最终输出：", "输出："):
        idx = text.rfind(marker)
        if idx >= 0:
            return text[idx + len(marker):].strip()
    return ""


def recent_summaries(exclude_run_id: str = "", limit: int = 2) -> list[dict[str, Any]]:
    """最近几轮已经整合好的提要。给本轮总结做"前几轮"上下文，顺便让云端不用逐轮去读。"""
    from .config import default_home
    from .util import read_json

    runs_dir = default_home() / "runs"
    if not runs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        names = sorted((p.name for p in runs_dir.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return []
    for name in names:
        if name == exclude_run_id:
            continue
        data = read_json(runs_dir / name / "memory.json", {})
        text = ((data or {}).get("summary") or {}).get("text")
        if not text:
            continue
        out.append({"run_id": name, "text": str(text)})
        if len(out) >= max(0, limit):
            break
    return out


def _raw_chars(memory: MemoryStore) -> int:
    """云端如果直接读原始记忆，要读多少字符（用来算节省比例）。"""
    import json

    videos = memory.data.get("videos") or []
    try:
        return len(json.dumps(videos, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def build(
    memory: MemoryStore,
    ollama: OllamaClient,
    config: dict[str, Any],
    logger: logging.Logger | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成本轮的视频总结。永远返回 dict，不抛异常。"""
    log = logger or logging.getLogger(__name__)
    daily_max = int(config.get("daily_video_max") or 6)
    plan = dict(memory.data.get("bucket_plan") or {})
    plan.update(budget.summary(daily_max))

    videos = [v for v in (memory.data.get("videos") or []) if isinstance(v, dict)]
    raw_chars = _raw_chars(memory)
    fallback = _rule_based(memory, plan)

    if not videos:
        result = {
            "ok": True, "degraded": True, "why": "没有视频记录",
            "text": fallback, "raw_chars": raw_chars, "summary_chars": len(fallback),
            "usage": {}, "model": config.get("model"),
        }
        memory.set_summary(result)
        return result

    prefs: list[str] = []
    for key in ("ai_tech_keywords", "ai_fun_keywords"):
        prefs.extend(str(x) for x in (config.get(key) or []))
    for pref in (config.get("other_prefs") or []):
        prefs.extend(str(x) for x in (pref.get("keywords") or []))
    prompt = summary_prompt(videos, plan, limit_chars=1500, prefs=prefs[:8], previous=previous)
    messages = [
        {"role": "system", "content": "你是文字整理员。只输出整理结果，不寒暄、不解释、不加前缀。"},
        {"role": "user", "content": prompt},
    ]
    usage: dict[str, Any] = {}
    text = ""
    try:
        # ⚠️ think=False 是必须的：qwen3 这类带思维链的模型会把 num_predict
        #    全花在 thinking 上，content 直接是空的（done_reason=length）。
        #    整理文本不需要推理过程，关掉思维链又快又省。
        chat = ollama.chat(messages, [], think=False)
        text = (chat.content or "").strip()
        if not text and chat.thinking:
            # 有些模型关不掉思维链：退一步从 thinking 里捞结论（去掉思考标记）
            text = _strip_thinking(chat.thinking)
        usage = {
            "prompt_eval_count": chat.prompt_eval_count or 0,
            "eval_count": chat.eval_count or 0,
            "calls": 1,
        }
        memory.add_usage(chat.prompt_eval_count, chat.eval_count, 1)
    except (OllamaError, Exception) as exc:  # noqa: BLE001
        log.warning("summary via local model failed: %s", exc)
        text = ""
        usage = {"error": f"{type(exc).__name__}: {exc}"}

    degraded = not text
    if degraded:
        text = fallback

    # 「评论」段一律用本轮事实重写。
    # 两个真实故障都撞在这句上：
    #   ① 明明发了，总结写「本轮未评论」（漏写）；
    #   ② 本轮没发，总结把**前几轮提要**里的评论抄了进来（串台）。
    # 这段本来就是"照抄原文"，交给 4B 只会出错，直接由代码填。
    repaired = False
    if not degraded:
        text, repaired = _fix_comment_section(text, videos)
    summary_chars = len(text)
    saved = max(0, raw_chars - summary_chars)
    result = {
        "ok": True,
        "degraded": degraded,
        "text": text,
        "raw_chars": raw_chars,
        "summary_chars": summary_chars,
        "saved_chars": saved,
        "saved_ratio": round(saved / raw_chars, 3) if raw_chars else 0,
        "usage": usage,
        "model": config.get("model"),
        "videos": len(videos),
        "repaired": repaired,
        "posted": sum(1 for v in videos if v.get("comment_status") == "posted"),
        "plan": plan,
        "previous_runs": [x.get("run_id") for x in (previous or [])],
    }
    try:
        memory.set_summary(result)
    except OSError:
        pass
    log.info(
        "summary built degraded=%s raw=%s chars, summary=%s chars",
        degraded, raw_chars, summary_chars,
    )
    return result
