from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import budget
from .bili_mcp import BiliPlusBridge
from .config import RunPaths, allow_write_env
from .memory import MemoryStore
from .ollama_client import ChatResult, OllamaClient, OllamaError
from .prompts import all_tool_specs, continue_prompt, system_prompt, task_prompt
from .state import StateStore
from .util import (
    append_jsonl,
    clip,
    json_dumps,
    normalize_bvid,
    now_iso,
)

# 说这些词等于自曝"我是本地小模型"。账号人设是 deepseek / 大肥鱼 / 鲸鱼娘。
IDENTITY_WORDS = (
    "qwen", "Qwen", "QWEN", "通义", "千问", "4b", "4B", "四b",
    "本地模型", "本地小模型", "小模型", "参数量", "量化", "离线模型",
)


# ── 评论风格闸门 ──────────────────────────────────────────────────
# 4B 写评论有两个固定毛病：套模板（"总的来说/不得不说"）和复读视频内容。
# 只靠提示词压不住，这里做代码级拦截。

AI_SMELL_WORDS = (
    "总的来说", "不得不说", "让我们", "首先", "其次", "此外", "再者",
    "综上所述", "总而言之", "值得一提的是", "可以看出", "确实让人",
    "希望对你", "在这个视频中", "本期视频", "up主", "UP主", "这期视频",
)

# 复读判定：和标题/摘要的**最长公共子串**超过这个长度就算照抄。
REPLAY_MIN_CHARS = 10
REPLAY_RATIO = 0.6


def ai_smell_violation(text: str) -> str:
    for word in AI_SMELL_WORDS:
        if word in text:
            return word
    return ""


def _longest_common_substring(a: str, b: str) -> str:
    """最长公共子串（只用滚动数组，a 限制在评论长度，够快）。"""
    if not a or not b:
        return ""
    best_len = 0
    best_end = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len = cur[j]
                    best_end = i
        prev = cur
    return a[best_end - best_len:best_end] if best_len else ""


def replay_violation(text: str, sources: list[str]) -> str:
    """评论和视频标题/摘要/字幕重合太多 = 复读。返回重合片段。"""
    body = str(text or "").strip()
    if not body:
        return ""
    for src in sources:
        src = str(src or "")
        if not src:
            continue
        common = _longest_common_substring(body, src)
        if len(common) >= REPLAY_MIN_CHARS or (common and len(common) / len(body) >= REPLAY_RATIO):
            return common
    return ""


# 娱乐/二创视频：关键词命中即算"娱乐向"，风控放宽（见 _note_video）。
ENTERTAINMENT_HINTS = (
    "二创", "娱乐", "整活", "搞笑", "玩梗", "鬼畜", "MV", "恶搞", "翻唱",
    "绘画", "配音", "抽象", "直播", "游戏实况", "游戏攻略", "vlog", "Vlog",
)


def is_entertainment(bucket: str = "", title: str = "", tags=None) -> bool:
    """判断一条视频是不是非 AI 科技类的娱乐/二创内容。"""
    haystack = f"{bucket or ''} {title or ''} {' '.join(str(x) for x in (tags or []))}".lower()
    return any(hint.lower() in haystack for hint in ENTERTAINMENT_HINTS)


def identity_violation(text: str) -> str:
    """返回命中的身份词（空串=没问题）。"""
    for word in IDENTITY_WORDS:
        if word in text:
            return word
    return ""


@dataclass
class AgentOutcome:
    finished: bool
    partial: bool = False
    cancelled: bool = False
    reason: str = ""
    summary: str = ""


def _as_list(value: Any, limit: int = 8) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.replace("；", "\n").splitlines() if x.strip()][:limit]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value[:limit]:
            if isinstance(item, dict):
                text = item.get("content") or item.get("text") or json_dumps(item)
            else:
                text = str(item)
            if text.strip():
                out.append(text.strip())
        return out
    return [str(value)]


def _as_comments(value: Any, limit: int = 6) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"user": "", "content": x, "like": 0} for x in _as_list(value, limit)]
    out: list[dict[str, Any]] = []
    if isinstance(value, (list, tuple)):
        for item in value[:limit]:
            if isinstance(item, dict):
                out.append(
                    {
                        "user": clip(item.get("user") or item.get("author") or "", 40),
                        "content": clip(item.get("content") or item.get("text") or "", 240),
                        "like": item.get("like") or 0,
                    }
                )
            else:
                out.append({"user": "", "content": clip(item, 240), "like": 0})
    return out


def _overlap(a: str, b: str) -> int:
    """Longest common substring length, capped. Cheap and good enough here."""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return 0
    if a in b or b in a:
        return min(len(a), len(b))
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def _enrich_comments(
    given: list[dict[str, Any]], cached: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Fill author/like from the real comments tool output.

    A 4B model reliably echoes comment text but not the metadata around it, so
    we match its snippets back to the cached rows instead of asking it to copy
    structured fields.
    """
    cached = [c for c in (cached or []) if isinstance(c, dict) and c.get("content")]
    if not cached:
        return given
    out: list[dict[str, Any]] = []
    for row in given:
        text = str(row.get("content") or "")
        if row.get("user") and row.get("like"):
            out.append(row)
            continue
        best, score = None, 0
        for candidate in cached:
            hits = _overlap(text, str(candidate.get("content") or ""))
            if hits > score:
                best, score = candidate, hits
        # Require a real overlap so unrelated text is not glued to a comment.
        if best is not None and score >= max(6, len(text) // 3):
            out.append(
                {
                    "user": row.get("user") or best.get("user") or "",
                    "content": clip(best.get("content") or text, 240),
                    "like": row.get("like") or best.get("like") or 0,
                }
            )
        else:
            out.append(row)
    return out


class Agent:
    def __init__(
        self,
        bridge: BiliPlusBridge,
        ollama: OllamaClient,
        memory: MemoryStore,
        config: dict[str, Any],
        state: StateStore,
        paths: RunPaths,
        candidates: list[dict[str, Any]],
        bili_status: dict[str, Any],
        seen_bvids: set[str],
        commented_bvids: set[str] | None = None,
        logger: logging.Logger | None = None,
    ):
        self.bridge = bridge
        self.ollama = ollama
        self.memory = memory
        self.config = config
        self.state = state
        self.paths = paths
        self.candidates = candidates
        self.bili_status = bili_status
        self.log = logger or logging.getLogger(__name__)
        self.tools = all_tool_specs(bridge.model_tool_specs())
        self.system = system_prompt(config, candidates, bili_status, memory.data.get("bucket_plan") or {})
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": task_prompt(config, candidates)},
        ]
        self.allowed_bvids = {c["bvid"] for c in candidates if c.get("bvid")}
        self.seen_bvids = set(seen_bvids) | {c["bvid"] for c in candidates if c.get("seen")}
        # 只有"已经评过"的视频才不能再评。看过一次不算 —— 见 scout.load_commented_bvids 的注释。
        self.commented_bvids = set(commented_bvids or set()) | {
            c["bvid"] for c in candidates if c.get("commented")
        }
        # Resume safety: remember videos/drafts already handled by an earlier
        # process so a continuation cannot duplicate notes or posts.
        previous = [v for v in (memory.data.get("videos") or []) if isinstance(v, dict)]
        self.note_bvids: set[str] = {
            v.get("bvid") for v in previous if v.get("bvid")
        }
        # 本轮"消费过内容"的视频集合（video_info / subtitle / 结论 / 关键帧）。
        # 浏览预算按它算，而不是只按 note 算 —— 否则换个工具就绕过去了。
        self.touched_bvids: set[str] = set(self.note_bvids)
        self.draft_ids: dict[int, str] = {
            int(v["draft_id"]): v["bvid"]
            for v in previous
            if v.get("draft_id") and v.get("bvid")
        }
        self.posted: set[str] = {
            v.get("bvid") for v in previous if v.get("comment_status") == "posted"
        }
        self.current_bvid = ""
        self.step = 0
        self.continues = 0
        self.finished = False
        self.partial = False
        self.cancelled = False
        self.finish_summary = ""
        self.finish_rejections = 0
        self.repeat_counts: dict[str, int] = {}
        # 关键帧：本地模型支持 vision，把图直接喂进上下文（最多 2 张，控制 token）
        self.pending_images: list[str] = []
        self.start_monotonic = time.monotonic()
        self.last_prompt_tokens = 0
        self.write_allowed = (
            bool(config.get("write"))
            # ⚠️ 必须同时看**当前**授权：run 快照可能是上次开的 write=true，
            #    而用户已经把「允许发评论」关了。只看快照 = 关不掉（实测过）。
            and allow_write_env()
            and not bool(bili_status.get("paused"))
            and int(bili_status.get("quota_left") if bili_status.get("quota_left") is not None else 1) > 0
        )
        self.context_char_limit = int(float(config.get("num_ctx", 32768)) * 0.68)

    # ── main loop ──────────────────────────────────────────────────
    async def run(self) -> AgentOutcome:
        self._trace("agent_start", candidates=len(self.candidates), write=self.config.get("write"))
        self.state.update(phase="agent", write=bool(self.config.get("write")))
        while True:
            if self._cancel_requested():
                return self._cancelled()
            if self._time_exceeded():
                return self._partial("time_limit")
            if self.step >= int(self.config.get("max_steps", 40)):
                return self._partial("max_steps")
            if self._context_too_big():
                if not self._reset_context("context_limit"):
                    return self._partial("context_limit")

            try:
                result = self.ollama.chat(self.messages, self.tools)
            except OllamaError as exc:
                self.memory.add_error("ollama", str(exc))
                self._trace("ollama_error", error=str(exc))
                if not self._reset_context("ollama_error", detail=str(exc)):
                    return self._partial("ollama_error")
                continue

            self.step += 1
            self.last_prompt_tokens = result.prompt_eval_count or self.last_prompt_tokens
            self.memory.add_usage(result.prompt_eval_count, result.eval_count, 1)
            self.state.update(steps=self.step, step=self.step, phase="agent")
            if result.thinking:
                self.memory.add_thinking(result.thinking, step=self.step)
                self._trace("thinking", text=clip(result.thinking, 3000))
            self._trace(
                "assistant",
                content=clip(result.content, 1500),
                tool_calls=[c.get("function", {}).get("name") for c in result.tool_calls],
                done_reason=result.done_reason,
                prompt_eval_count=result.prompt_eval_count,
                eval_count=result.eval_count,
            )

            if result.tool_calls:
                self.messages.append(self._assistant_message(result))
                for call in result.tool_calls[:3]:
                    if self._cancel_requested():
                        return self._cancelled()
                    outcome = await self._execute_tool(call)
                    name = str(call.get("function", {}).get("name") or "unknown")
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_name": name,
                            "content": outcome.get("message") or json_dumps({"ok": False, "error": "empty result"}),
                        }
                    )
                    # 关键帧是图片，工具消息里只能给路径；真正的图挂在随后的 user 消息上。
                    if self.pending_images:
                        self.messages.append(self._image_message())

                    if self.finished:
                        return AgentOutcome(
                            finished=True,
                            partial=self.partial,
                            summary=self.finish_summary,
                        )
                continue

            # The model answered with prose only. That is not progress.
            if result.done_reason == "length":
                reason = "length"
            elif not result.content.strip():
                reason = "empty_response"
            else:
                reason = "no_tool_call"
            self._trace("no_tool_call", reason=reason, content=clip(result.content, 800))
            if not self._reset_context(reason, detail=result.content[:500]):
                return self._partial(reason)

    def _image_message(self) -> dict[str, Any]:
        """把刚抽到的关键帧作为图片塞进上下文（Ollama 的 messages[].images）。"""
        import base64

        paths = self.pending_images[:2]
        self.pending_images = []
        images: list[str] = []
        for path in paths:
            try:
                with open(path, "rb") as fh:
                    images.append(base64.b64encode(fh.read()).decode("ascii"))
            except OSError as exc:
                self.log.warning("keyframe read failed: %s", exc)
        if not images:
            return {"role": "user", "content": "关键帧图片没读到，只用字幕判断。"}
        self._trace("images", count=len(images), paths=paths)
        return {
            "role": "user",
            "content": f"这是刚才那条视频的 {len(images)} 张关键帧截图，按顺序看画面。判断内容以字幕和这些画面为准。",
            "images": images,
        }

    # ── tool dispatch ──────────────────────────────────────────────
    async def _execute_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}

        # A 4B model will happily call the exact same tool with the exact same
        # arguments forever. Break that before it eats the whole step budget.
        fingerprint = name + "|" + json_dumps({k: args[k] for k in sorted(args)})
        seen = self.repeat_counts.get(fingerprint, 0) + 1
        self.repeat_counts[fingerprint] = seen
        if seen >= 5:
            self._trace("repeat_break", name=name, count=seen)
            self._auto_finish(
                f"模型把同一个调用（{name}）重复了 {seen} 次，已自动收尾。"
            )
            return {
                "ok": True,
                "data": {"ok": True},
                "message": "同一调用重复过多，本轮已自动结束。请调用 autopilot_finish 或直接汇报。",
            }

        self._trace("tool_call", name=name, args=args)
        try:
            if name.startswith("autopilot_"):
                result = await self._handle_pseudo(name, args)
            else:
                result = await self._handle_bili(name, args)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("tool %s failed", name)
            self.memory.add_error(f"tool:{name}", str(exc))
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        message = self._compact_result(name, result)
        ok = bool(result.get("ok", False))
        if seen >= 3 and not ok:
            message = (
                f"（同一调用第 {seen} 次失败）{message}\n"
                "不要再重复这个调用。换一条视频，或调用 autopilot_finish 收尾。"
            )
        self.memory.add_tool(name, args, ok, message, self.step, str(result.get("error") or ""))
        self._trace("tool_result", name=name, ok=ok, result=result)
        return {"ok": ok, "data": result, "message": message}

    async def _handle_bili(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in {"video_info", "subtitle", "keyframes", "ai_conclusion", "comments"}:
            bvid = normalize_bvid(args.get("bvid")) or self.current_bvid
            if not bvid:
                return {"ok": False, "error": "缺少 bvid"}
            if bvid not in self.allowed_bvids:
                return {
                    "ok": False,
                    "error": f"{bvid} 不在候选里。只能处理候选视频，不要编造 BV 号。",
                }
            if name == "video_info" and len(self.note_bvids) >= int(self.config.get("max_videos", 1)) and bvid not in self.note_bvids:
                return {
                    "ok": False,
                    "error": f"本次最多看 {self.config.get('max_videos')} 条视频，已经记录够了，请调用 autopilot_finish。",
                }
            # ⭐ 每轮浏览预算统一在这里扣（新）：
            #    旧实现只挡 video_info，直接调 subtitle 就能在「最多看 1 条」的
            #    设定下连着读第 2、3 条视频的字幕（探针复现）。凡是"第一次真正
            #    消费某条视频内容"的工具，都必须先过这道预算。
            if name in {"video_info", "subtitle", "ai_conclusion", "keyframes"}:
                if (bvid not in self.touched_bvids
                        and len(self.touched_bvids) >= int(self.config.get("max_videos", 1))):
                    return {
                        "ok": False,
                        "error": (
                            f"本次最多看 {self.config.get('max_videos')} 条视频，"
                            "已经用到上限了。请对已看过的视频收尾并调用 autopilot_finish。"
                        ),
                    }
                self.touched_bvids.add(bvid)
            if name == "video_info":
                call_args = {"bvid": bvid}
            elif name == "subtitle":
                call_args = {
                    "bvid": bvid,
                    "max_chars": int(self.config.get("subtitle_max_chars", 4500)),
                }
            elif name == "keyframes":
                call_args = {
                    "bvid": bvid,
                    "count": int(args.get("count") or 3),
                }
            elif name == "ai_conclusion":
                call_args = {"bvid": bvid}
            else:
                call_args = {
                    "bvid": bvid,
                    "num": int(self.config.get("comments_num", 8)),
                    "with_replies": False,
                }
            result = await self.bridge.call(name, call_args)
            if result.get("ok") is False:
                return result
            self.current_bvid = bvid
            self._cache_tool_result(name, bvid, result)
            return result

        if name == "draft_create":
            return await self._draft_create(args)
        if name == "comment_post":
            return await self._comment_post(args)
        if name == "verify_posted":
            return await self._verify_posted(args)
        if name == "safety_check":
            return await self.bridge.call("safety_check", {
                "text": str(args.get("text") or ""),
                "bvid": normalize_bvid(args.get("bvid")) or self.current_bvid,
            })
        if name == "style_check":
            return await self.bridge.call("style_check", {"text": str(args.get("text") or "")})
        if name == "persona_prompt":
            return await self.bridge.call("persona_prompt", {})
        return {"ok": False, "error": f"未授权的工具：{name}"}

    async def _handle_pseudo(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "autopilot_note_video":
            return await self._note_video(args)
        if name == "autopilot_note_finding":
            text = clip(args.get("text") or "", 1200)
            if not text:
                return {"ok": False, "error": "text 不能为空"}
            importance = int(args.get("importance") or 1)
            self.memory.add_finding(text, importance)
            return {"ok": True, "message": "已记录发现"}
        if name == "autopilot_finish":
            return self._finish(args)
        return {"ok": False, "error": f"未知伪工具：{name}"}

    # ── pseudo tools ───────────────────────────────────────────────
    async def _note_video(self, args: dict[str, Any]) -> dict[str, Any]:
        bvid = normalize_bvid(args.get("bvid")) or self.current_bvid
        if not bvid:
            return {"ok": False, "error": "缺少 bvid"}
        if bvid not in self.allowed_bvids:
            return {"ok": False, "error": f"{bvid} 不在候选里，不要编造 BV 号"}
        if bvid not in self.note_bvids and len(self.note_bvids) >= int(self.config.get("max_videos", 1)):
            return {
                "ok": False,
                "error": f"本次最多记录 {self.config.get('max_videos')} 条视频，请调用 autopilot_finish。",
            }

        action = str(args.get("action") or "inspected").strip().lower()
        if action not in {"inspected", "skip", "comment"}:
            return {"ok": False, "error": "action 只能是 inspected / skip / comment"}
        summary = clip(args.get("summary") or "", 1800)
        key_points = _as_list(args.get("key_points"), 6)
        cached_hot = (self.memory.get_video(bvid) or {}).get("hot_comments") or []
        hot_comments = _enrich_comments(
            _as_comments(args.get("hot_comments"), 6), cached_hot
        )
        reason = clip(args.get("reason") or "", 500)
        comment_text = clip(args.get("comment_text") or "", 120)
        cache = self.memory.get_video(bvid) or {}
        # 候选里带的播放量/粉丝数落进记忆 —— 视频总结要用，而且不用再查一遍。
        cand = next((c for c in self.candidates if c.get("bvid") == bvid), {})
        if not summary:
            summary = cache.get("summary") or cache.get("ai_conclusion") or cache.get("subtitle_excerpt") or "未获取到内容摘要"
        if cand:
            self.memory.add_video(
                bvid,
                play=cand.get("play"),
                fans=cand.get("fans"),
                bucket=cand.get("bucket") or "",
                author=cand.get("author") or cache.get("author") or "",
                title=cand.get("title") or cache.get("title") or "",
            )

        if action == "comment":
            if not self.write_allowed:
                return {
                    "ok": False,
                    "error": "本次只读或额度不足/插件暂停，不能评论。把 action 改成 inspected 或 skip。",
                }
            early_bad = identity_violation(comment_text)
            if early_bad:
                return {
                    "ok": False,
                    "error": (
                        f"评论里出现了「{early_bad}」，这会暴露模型身份。改掉：只自称 deepseek / 大肥鱼 / 鲸鱼娘，"
                        "别提 qwen、4B、小模型、参数量。"
                    ),
                }
            problem = self._comment_problem(comment_text, cache)
            if problem:
                return {"ok": False, "error": problem}
            if bvid in self.commented_bvids:
                return {"ok": False, "error": "这条视频之前已经评论过了（commented=true），别再发。"}
            if len(self.posted) >= int(self.config.get("max_comments", 0)):
                return {"ok": False, "error": "本次评论额度已用完，请 finish。"}
            if not comment_text:
                return {"ok": False, "error": "action=comment 时必须给 comment_text（20~50 字）。"}
            if len(comment_text) < 20 or len(comment_text) > 50:
                return {
                    "ok": False,
                    "error": (
                        f"评论长度 {len(comment_text)} 字，超了。请压到 30~40 字："
                        "只留一句最核心的感受，删掉铺垫和修饰，再调用一次。"
                    ),
                }
            safety = await self.bridge.call("safety_check", {"text": comment_text, "bvid": bvid})
            if safety.get("ok") is False:
                return {"ok": False, "error": f"safety_check 失败：{safety.get('error') or safety}"}
            text_verdict = safety.get("text") or {}
            video_verdict = safety.get("video") or {}
            text_action = str(text_verdict.get("action") or "allow")
            video_action = str(video_verdict.get("action") or "allow")
            if text_action in {"block", "no_reply", "rewrite"}:
                return {
                    "ok": False,
                    "error": f"评论安全审未通过：{text_action}",
                    "fixes": text_verdict.get("fixes") or [],
                    "hits": text_verdict.get("hits") or [],
                }
            if video_action in {"block", "no_reply"}:
                self.memory.update_video(
                    bvid, comment_action="skip", reason=f"视频安全审 {video_action}"
                )
                return {
                    "ok": False,
                    "error": f"视频安全审未通过：{video_action}，放弃这条。",
                    "hits": video_verdict.get("hits") or [],
                }
            style = await self.bridge.call("style_check", {"text": comment_text})
            style_issues = style.get("issues") or []
            if style.get("ok") is False and not style_issues:
                # A tool-level failure is not a style verdict. Keep going, but
                # leave a trace so the digest can show that style was unchecked.
                self.memory.add_error("style_check", str(style.get("error") or style))
                style_data = {}
            else:
                style_data = style
                # 娱乐/二创视频：风格风控放宽。这几类问题对整活内容不构成硬伤，
                # 降级为 warning，不拦发送；涉及安全/身份的问题仍照旧拦。
                if is_entertainment(cache.get("bucket"), cache.get("title"), cache.get("tags")):
                    soft = {"ai_tone", "preaching", "too_many_emoji", "too_many_exclamations",
                            "catchphrase_repeated"}
                    hard = [i for i in style_issues if not any(i.startswith(k) for k in soft)]
                    for issue in style_issues:
                        if any(issue.startswith(k) for k in soft):
                            style_data.setdefault("warnings", []).append(issue)
                    style_issues = hard
                    if style_issues:
                        style_data["ok"] = False
                    else:
                        style_data["ok"] = True
                    style_data["relaxed"] = True
            if style_issues:
                return {
                    "ok": False,
                    "error": "评论风格不像人设，先改一改再 note。",
                    "issues": style_issues,
                    "warnings": style.get("warnings") or [],
                }
            self.memory.add_video(
                bvid,
                summary=summary,
                key_points=key_points,
                hot_comments=hot_comments,
                comment_action="comment",
                comment_text=comment_text,
                reason=reason,
                safety={"text": text_verdict, "video": video_verdict},
                style=style_data,
            )
            self.note_bvids.add(bvid)
            self.memory.update_video(bvid, noted=True)
            self._mark_viewed(bvid)
            self.state.update(videos_seen=self._viewed_count(), current_bvid=bvid)
            return {
                "ok": True,
                "message": "已记录 comment 判断。下一步调用 draft_create。",
                "safety": {"text_action": text_action, "video_action": video_action},
                "style": {"ok": style_data.get("ok"), "issues": style_data.get("issues") or []},
            }

        self.memory.add_video(
            bvid,
            summary=summary,
            key_points=key_points,
            hot_comments=hot_comments,
            comment_action=action,
            reason=reason,
        )
        self.note_bvids.add(bvid)
        self.memory.update_video(bvid, noted=True)
        self._mark_viewed(bvid)
        self.state.update(videos_seen=self._viewed_count(), current_bvid=bvid)
        return {"ok": True, "message": f"已记录 {action}。可以继续下一条或 finish。"}

    def _viewed_count(self) -> int:
        """真正看过内容的条数（读过字幕/AI总结，或做过判断）。"""
        return sum(
            1 for v in (self.memory.data.get("videos") or [])
            if isinstance(v, dict) and (v.get("inspected") or v.get("noted"))
        )

    def _mark_viewed(self, bvid: str) -> None:
        """把「今天看了这条」记进跨轮额度。失败不影响本轮。

        ⚠️ 按"真读了内容"算，不是按 note 算：
           4B 经常看了字幕却忘了调 note_video，按 note 计数会让当天额度虚高，
           于是同一天反复看同一批视频。
        """
        row = self.memory.get_video(bvid)
        already = bool(row and row.get("inspected"))
        if row is not None and not already:
            self.memory.update_video(bvid, inspected=True)
        if already:
            return
        try:
            budget.note_viewed([bvid])
        except Exception as exc:  # noqa: BLE001
            self.log.warning("budget.note_viewed failed: %s", exc)

    def _finish(self, args: dict[str, Any]) -> dict[str, Any]:
        summary = clip(args.get("summary") or "本地模型结束", 1800)
        highlights = _as_list(args.get("highlights"), 8)
        issues = _as_list(args.get("issues"), 8)
        if not self.note_bvids and self.candidates and self.finish_rejections < 1:
            self.finish_rejections += 1
            return {
                "ok": False,
                "error": "还没有记录任何视频。先看一条候选并调用 autopilot_note_video，再 finish。",
            }

        # 没看满就不许收工：4B 常常发完一条评论就想 finish，视频额度白白浪费。
        want = min(int(self.config.get("max_videos", 1)), len(self.candidates))
        viewed = self._viewed_count()
        remaining = [c for c in self.candidates if not (self.memory.get_video(c["bvid"]) or {}).get("inspected")]
        if viewed < want and remaining and self.finish_rejections < 2:
            self.finish_rejections += 1
            nxt = remaining[0]
            return {
                "ok": False,
                "error": (
                    f"还没看满：本次要看 {want} 条，现在看过 {viewed} 条。"
                    f"评论额度用完也可以继续看（只看不评），下一条建议 {nxt['bvid']}。"
                    "看完再 finish。"
                ),
            }

        # 整轮一条都没评，但看过 ≥3 条 → 从娱乐/二创里挑一条补一条评论。
        # 这是兜底，不是常态：默认仍让它自由判断，只在"全线保守"时推一把。
        if (
            self.write_allowed
            and len(self.posted) == 0
            and viewed >= 3
            and int(self.config.get("max_comments", 0)) >= 1
            and self.finish_rejections < 2
        ):
            ent = [v for v in (self.memory.data.get("videos") or [])
                   if isinstance(v, dict)
                   and v.get("comment_action") in {"inspected", "skip"}
                   and v.get("bvid") not in self.commented_bvids
                   and is_entertainment(v.get("bucket"), v.get("title"), v.get("tags"))]
            if ent:
                self.finish_rejections += 1
                pick = ent[0]
                return {
                    "ok": False,
                    "error": (
                        f"这轮还没发评论。从看过的娱乐/二创视频里补一条："
                        f"建议 {pick['bvid']}《{(pick.get('title') or '')[:20]}》"
                        "（它更放松，适合用大肥鱼/鲸鱼娘的口吻随手一评）。"
                        "用 autopilot_note_video(action='comment') 记一条再 finish。"
                    ),
                }
        # 一条视频都没看成却报"正常完成"会骗人（旧实现就是这样，探针复现过）。
        # 这种情况按**部分完成**落账，reason 写进 issues，摘要里也带着。
        no_video = self._viewed_count() == 0
        if no_video:
            issues = list(issues) + ["本轮没有任何视频被实际看到（0 条），这不是一次正常完成"]
        self.memory.mark_finished(summary, highlights, issues, partial=no_video)
        self.finished = True
        self.partial = no_video
        self.finish_summary = summary
        self._trace("finish", summary=summary, highlights=highlights,
                    issues=issues, partial=no_video)
        return {"ok": True, "message": "已结束，正在生成本地汇报。"}

    def _auto_finish(self, reason: str) -> None:
        """Stop the run cleanly when the model is stuck in a loop.

        ⚠️ 熔断**不是**正常完成：重复调同一个失败工具 5 次说明这一轮已经废了。
        旧实现写 `partial=False`，于是摘要说"completed"，把失败包装成成功
        （探针复现：0 条视频、status=completed、partial=false）。
        """
        if self.finished:
            return
        notes = len(self.note_bvids)
        summary = f"{reason}（本轮已记录 {notes} 条视频）"
        self.memory.mark_finished(
            summary,
            highlights=[f"已看 {notes} 条视频"] if notes else [],
            issues=[reason],
            partial=True,
        )
        self.partial = True
        self.finished = True
        self.partial = False
        self.finish_summary = summary
        self._trace("auto_finish", reason=reason, videos=notes)

    # ── write path ─────────────────────────────────────────────────
    def _comment_problem(self, text: str, row: dict[str, Any]) -> str:
        """评论能不能用：依据够不够、有没有 AI 腔、是不是复读。返回错误文案（空=通过）。"""
        has_evidence = bool(
            (row.get("subtitle_excerpt") or "").strip()
            or (row.get("ai_conclusion") or "").strip()
            or (row.get("keyframes") or [])
        )
        if not has_evidence:
            return (
                "你还没拿到字幕或关键帧画面，不能凭空评论。"
                "先调用 subtitle；没有字幕就调用 keyframes 看画面；两者都没有就 skip。"
            )
        smell = ai_smell_violation(text)
        if smell:
            return (
                f"评论里有「{smell}」，是明显的 AI 腔。"
                "改成随口一评的口语，去掉总结式套话。"
            )
        common = replay_violation(text, [
            row.get("title") or "",
            row.get("summary") or "",
            (row.get("subtitle_excerpt") or "")[:1200],
        ])
        if common:
            return (
                f"评论和视频内容重合太多（「{common}」），等于复读。"
                "写你自己的反应或梗，别转述视频讲了什么。"
            )
        return ""

    def _note_for(self, bvid: str) -> dict[str, Any] | None:
        row = self.memory.get_video(bvid)
        if not row or row.get("comment_action") != "comment":
            return None
        return row

    def _draft_for(self, bvid: str) -> int:
        for draft_id, draft_bvid in self.draft_ids.items():
            if draft_bvid == bvid:
                return int(draft_id)
        return 0

    async def _draft_create(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.write_allowed:
            return {"ok": False, "error": "写入未启用（只读/暂停/额度不足）"}
        bvid = normalize_bvid(args.get("bvid")) or self.current_bvid
        if not bvid:
            return {"ok": False, "error": "缺少 bvid"}
        note = self._note_for(bvid)
        if not note:
            return {"ok": False, "error": "先调用 autopilot_note_video(action='comment') 记录判断。"}
        if bvid in self.posted:
            return {"ok": False, "error": "这个视频本次已经发过评论。"}
        text = clip(args.get("text") or note.get("comment_text") or "", 120)
        if not text:
            return {"ok": False, "error": "缺少评论文本"}
        if len(text) < 20 or len(text) > 50:
            return {
                "ok": False,
                "error": (
                    f"评论 {len(text)} 字，超了。请压到 30~40 字："
                    "只留一句最核心的感受，删掉铺垫，再试一次。"
                ),
            }

        # Reuse the draft this run already made for the video instead of piling
        # up draft_id 33/34/35… every time the model re-plans the same step.
        existing = self._draft_for(bvid)
        if existing:
            if text == (note.get("comment_text") or "") or text == str(
                (note.get("post_result") or {}).get("would_post") or ""
            ):
                return {
                    "ok": True,
                    "draft_id": existing,
                    "status": "reused",
                    "reused": True,
                    "message": "这条视频本轮已有草稿，直接用它预演，不要重复创建。",
                }
            return {
                "ok": False,
                "error": (
                    f"这条视频本轮已有草稿 draft_id={existing}，评论文本必须与之一致。"
                    "不要为同一条视频重复建草稿；要做就 finish。"
                ),
            }

        if text != note.get("comment_text"):
            problem = self._comment_problem(text, note)
            if problem:
                return {"ok": False, "error": problem}
            bad = identity_violation(text)
            if bad:
                return {
                    "ok": False,
                    "error": f"评论里出现了「{bad}」，会暴露模型身份。改掉再试。",
                }
            # If the model changed the text after the safety check, re-check.
            safety = await self.bridge.call("safety_check", {"text": text, "bvid": bvid})
            if safety.get("ok") is False:
                return {"ok": False, "error": f"safety_check 失败：{safety.get('error') or safety}"}
            action = str((safety.get("text") or {}).get("action") or "allow")
            if action in {"block", "no_reply", "rewrite"}:
                return {"ok": False, "error": f"评论安全审未通过：{action}"}
            style = await self.bridge.call("style_check", {"text": text})
            style_issues = style.get("issues") or []
            if style_issues:
                return {
                    "ok": False,
                    "error": "改过的评论风格不像人设，先改一改。",
                    "issues": style_issues,
                    "warnings": style.get("warnings") or [],
                }
            self.memory.update_video(
                bvid, comment_text=text, safety=safety, style=style
            )

        result = await self.bridge.call(
            "draft_create",
            {
                "bvid": bvid,
                "text": text,
                "rationale": clip(args.get("rationale") or "bili-autopilot local model", 300),
            },
        )
        if result.get("ok") is False:
            return result
        draft_id = int(result.get("draft_id") or 0)
        if not draft_id:
            return {"ok": False, "error": f"draft_create 没有返回 draft_id：{json_dumps(result)}"}
        self.draft_ids[draft_id] = bvid
        self.memory.update_video(bvid, draft_id=draft_id, comment_status="drafted")
        self.state.update(phase="write")
        return result

    async def _comment_post(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.write_allowed:
            return {"ok": False, "error": "写入未启用（只读/暂停/额度不足）"}
        # ⭐ 本轮评论额度在**发送前**再数一遍（新）。
        #    旧实现只在 note_video 里看已发数，于是"先 note 两条、再发两条"
        #    能在 max_comments=1 时真发出 2 条（探针复现）。
        cap = int(self.config.get("max_comments", 0) or 0)
        if cap > 0 and len(self.posted) >= cap:
            return {
                "ok": False,
                "error": f"本轮已发 {len(self.posted)} 条评论（上限 {cap}），不要再发，请 finish。",
            }
        draft_id = int(args.get("draft_id") or 0)
        if draft_id not in self.draft_ids:
            return {"ok": False, "error": "draft_id 不是本次运行创建的，拒绝发送。"}
        bvid = self.draft_ids[draft_id]
        result = await self.bridge.call("comment_post", {"draft_id": draft_id})
        if result.get("ok") is False:
            self.memory.update_video(bvid, comment_status="failed", post_result=result)
            error = str(result.get("error") or "")
            if "风控" in error or result.get("paused"):
                self.write_allowed = False
                self.memory.add_error("comment_post", f"风控/暂停：{error}")
            return result
        self.posted.add(bvid)
        self.memory.update_video(bvid, comment_status="posted", post_result=result)
        self.state.update(comments_posted=len(self.posted), phase="write")
        try:
            budget.note_comment(1)
        except Exception:  # noqa: BLE001
            pass
        verify = await self._verify_posted({"draft_id": draft_id})
        combined = dict(result)
        combined["verify"] = verify
        return combined

    async def _verify_posted(self, args: dict[str, Any]) -> dict[str, Any]:
        draft_id = int(args.get("draft_id") or 0)
        if draft_id not in self.draft_ids:
            return {"ok": False, "error": "draft_id 不是本次运行创建的"}
        result = await self.bridge.call("verify_posted", {"draft_id": draft_id})
        bvid = self.draft_ids.get(draft_id)
        if bvid:
            self.memory.update_video(bvid, verify_result=result)
        return result

    # ── caching / compaction ───────────────────────────────────────
    def _merge_candidate(self, bvid: str) -> None:
        """把候选里带的播放量/粉丝/板块并进记忆（总结和 digest 都要用）。"""
        cand = next((c for c in self.candidates if c.get("bvid") == bvid), None)
        if not cand:
            return
        row = self.memory.get_video(bvid) or {}
        fields: dict[str, Any] = {"bucket": cand.get("bucket") or ""}
        for key in ("play", "fans"):
            if isinstance(cand.get(key), int) and not isinstance(row.get(key), int):
                fields[key] = cand[key]
        for key in ("title", "author"):
            if cand.get(key) and not row.get(key):
                fields[key] = cand[key]
        self.memory.update_video(bvid, **fields)

    def _cache_tool_result(self, name: str, bvid: str, result: dict[str, Any]) -> None:
        if name in {"subtitle", "ai_conclusion", "video_info"}:
            self._merge_candidate(bvid)
        if name == "video_info":
            self.memory.add_video(
                bvid,
                title=result.get("title") or "",
                author=result.get("author") or "",
                author_mid=result.get("author_mid") or 0,
                duration_text=result.get("duration_text") or "",
                pubdate=result.get("pubdate") or 0,
                description=result.get("description") or "",
                tags=result.get("tags") or [],
                url=result.get("url") or f"https://www.bilibili.com/video/{bvid}",
            )
            self.allowed_bvids.add(bvid)
        elif name == "subtitle":
            text = str(result.get("text") or "")
            if text:
                self._mark_viewed(bvid)
                self.memory.update_video(
                    bvid,
                    subtitle_excerpt=text[:3000],
                    summary=(self.memory.get_video(bvid) or {}).get("summary") or text[:700],
                )
            elif result.get("message"):
                self.memory.add_finding(f"{bvid} 字幕不可用：{result.get('message')}", 1)
        elif name == "ai_conclusion":
            self._mark_viewed(bvid)
            payload = {
                "summary": result.get("summary") or "",
                "outline": result.get("outline") or result.get("conclusion") or "",
            }
            self.memory.update_video(
                bvid,
                ai_conclusion=clip(json_dumps(payload), 2000),
                summary=(self.memory.get_video(bvid) or {}).get("summary") or clip(payload["summary"], 700),
            )
        elif name == "keyframes":
            frames = result.get("frames") or []
            paths = [f.get("path") for f in frames if isinstance(f, dict) and f.get("path")]
            times = [f.get("timestamp_text") or f.get("timestamp") for f in frames if isinstance(f, dict)]
            self.memory.update_video(bvid, keyframes=paths[:4], keyframe_times=times[:4])
            self.pending_images = paths[:2]
        elif name == "comments":
            self.memory.update_video(bvid, hot_comments=_as_comments(result.get("comments"), 6))

    def _compact_result(self, name: str, result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return clip(str(result), 2500)
        if name == "video_info":
            keep = {
                "ok": result.get("ok"),
                "bvid": result.get("bvid"),
                "title": result.get("title"),
                "author": result.get("author"),
                "duration_text": result.get("duration_text"),
                "pubdate": result.get("pubdate"),
                "description": clip(result.get("description"), 500),
                "tags": result.get("tags"),
                "stat": result.get("stat"),
            }
            return json_dumps(keep)
        if name == "subtitle":
            keep = {
                "ok": result.get("ok"),
                "bvid": result.get("bvid"),
                "title": result.get("title"),
                "language": result.get("language"),
                "chars": result.get("chars"),
                "text": clip(result.get("text"), 3500),
                "message": result.get("message"),
                "truncated": result.get("truncated"),
            }
            return json_dumps(keep)
        if name == "keyframes":
            keep = {
                "ok": result.get("ok"),
                "bvid": result.get("bvid"),
                "count": len(result.get("frames") or []),
                "frames": [
                    {"t": f.get("timestamp_text") or f.get("timestamp"), "path": f.get("path")}
                    for f in (result.get("frames") or []) if isinstance(f, dict)
                ][:4],
                "note": "画面已作为图片附在下一轮消息里。",
            }
            return json_dumps(keep)
        if name == "ai_conclusion":
            keep = {
                "ok": result.get("ok"),
                "bvid": result.get("bvid"),
                "title": result.get("title"),
                "summary": clip(result.get("summary"), 1200),
                "outline": clip(result.get("outline") or result.get("conclusion"), 1600),
                "status": result.get("status"),
            }
            return json_dumps(keep)
        if name == "comments":
            comments = []
            for row in (result.get("comments") or [])[:8]:
                if isinstance(row, dict):
                    comments.append(
                        {
                            "user": row.get("user"),
                            "content": clip(row.get("content"), 180),
                            "like": row.get("like"),
                            "reply_count": row.get("reply_count"),
                        }
                    )
            return json_dumps({"ok": result.get("ok"), "bvid": result.get("bvid"), "count": result.get("count"), "comments": comments})
        if name in {"draft_create", "comment_post", "verify_posted"}:
            keep = {
                "ok": result.get("ok"),
                "draft_id": result.get("draft_id"),
                "status": result.get("status"),
                "text": result.get("text"),
                "message": result.get("message"),
                "error": result.get("error"),
                "warnings": result.get("warnings"),
                "style": result.get("style"),
                "verify": result.get("verify"),
            }
            return json_dumps(keep)
        return clip(json_dumps(result), 2500)

    def _assistant_message(self, result: ChatResult) -> dict[str, Any]:
        calls = []
        for call in result.tool_calls:
            fn = call.get("function") or {}
            calls.append({"function": {"name": fn.get("name"), "arguments": fn.get("arguments") or {}}})
        return {"role": "assistant", "content": result.content or "", "tool_calls": calls}

    def _context_too_big(self) -> bool:
        if self.last_prompt_tokens > int(float(self.config.get("num_ctx", 32768)) * 0.72):
            return True
        total = 0
        for message in self.messages:
            total += len(str(message.get("content") or ""))
            if message.get("tool_calls"):
                total += len(json_dumps(message["tool_calls"]))
        return total > self.context_char_limit

    def _reset_context(self, reason: str, detail: str = "") -> bool:
        if self.continues >= int(self.config.get("max_continues", 8)):
            return False
        self.continues += 1
        self.last_prompt_tokens = 0
        memory_text = self.memory.compact(6000)
        self.memory.add_continuation(reason, self.step, detail)
        self.state.update(continues=self.continues, phase="agent")
        self._trace("continue", reason=reason, detail=clip(detail, 500), count=self.continues)
        self.messages = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": continue_prompt(memory_text, reason, self.step)},
        ]
        return True

    # ── outcome helpers ────────────────────────────────────────────
    def _cancel_requested(self) -> bool:
        return self.state.cancel_requested()

    def _time_exceeded(self) -> bool:
        return (time.monotonic() - self.start_monotonic) > int(self.config.get("max_minutes", 20)) * 60

    def _cancelled(self) -> AgentOutcome:
        self.cancelled = True
        self.memory.set_status("cancelled")
        self._trace("cancelled")
        return AgentOutcome(finished=False, cancelled=True, reason="cancelled", summary="用户取消")

    def _partial(self, reason: str) -> AgentOutcome:
        if not self.finished:
            summary = f"未完成（{reason}），已保留当前记忆。"
            self.memory.mark_finished(
                summary,
                highlights=[f"已记录 {len(self.note_bvids)} 条视频"] if self.note_bvids else [],
                issues=[reason],
                partial=True,
            )
            self.finished = True
            self.partial = True
            self.finish_summary = summary
        self._trace("partial", reason=reason)
        return AgentOutcome(finished=True, partial=True, reason=reason, summary=self.finish_summary)

    def _trace(self, event_type: str, **data: Any) -> None:
        append_jsonl(
            self.paths.trace,
            {"at": now_iso(), "step": self.step, "type": event_type, "data": data},
        )
