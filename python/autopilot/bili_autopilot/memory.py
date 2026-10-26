from __future__ import annotations

from typing import Any

from .config import RunPaths
from .util import (
    atomic_write_json,
    atomic_write_text,
    clip,
    json_dumps,
    now_iso,
    read_json,
    ts_text,
)


def _as_list(value: Any, limit: int = 8) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [x.strip() for x in value.replace("；", "\n").replace(";", "\n").splitlines()]
        return [x for x in parts if x][:limit]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value[:limit]:
            if isinstance(item, dict):
                text = item.get("content") or item.get("text") or json_dumps(item)
            else:
                text = str(item)
            text = text.strip()
            if text:
                out.append(text)
        return out
    return [str(value)]


def _as_comment_list(value: Any, limit: int = 6) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"content": x, "user": "", "like": 0} for x in _as_list(value, limit)]
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


class MemoryStore:
    """Structured run memory.

    JSON is the machine-readable source. Markdown files are for humans and for
    the final DeepSeek digest. Thinking is stored separately so it never has to
    be replayed into the small model's context verbatim.
    """

    def __init__(self, paths: RunPaths, config: dict[str, Any]):
        self.paths = paths
        self.config = config
        self.data: dict[str, Any] = self._blank()

    def _blank(self) -> dict[str, Any]:
        return {
            "run_id": self.paths.run_dir.name,
            "goal": self.config.get("task", ""),
            "model": self.config.get("model"),
            "write": bool(self.config.get("write")),
            "started_at": now_iso(),
            "finished_at": None,
            "status": "running",
            "partial": False,
            "bili_status": {},
            "bucket_plan": {},
            "candidates": [],
            "videos": [],
            "findings": [],
            "thinking": [],
            "tools": [],
            "continuations": [],
            "errors": [],
            "usage": {"calls": 0, "prompt_eval_count": 0, "eval_count": 0},
            "finish": {},
            # 本地模型整合后的紧凑总结（云端只读这个，不读原始记忆）
            "summary": {},
        }

    # ── persistence ────────────────────────────────────────────────
    def reset(self) -> dict[str, Any]:
        """Start a fresh memory for a new run (keeps the run id/paths)."""
        self.data = self._blank()
        self.save()
        return self.data

    def load(self) -> dict[str, Any]:
        data = read_json(self.paths.memory_json, {})
        if isinstance(data, dict) and data:
            base = self._blank()
            base.update(data)
            for key in ("candidates", "videos", "findings", "thinking", "tools",
                        "continuations", "errors"):
                if not isinstance(base.get(key), list):
                    base[key] = []
            if not isinstance(base.get("usage"), dict):
                base["usage"] = {"calls": 0, "prompt_eval_count": 0, "eval_count": 0}
            self.data = base
        return self.data

    def save(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.paths.memory_json, self.data)
        atomic_write_text(self.paths.memory_md, self.render_markdown())
        atomic_write_text(self.paths.thinking_md, self.render_thinking())

    # ── candidates ─────────────────────────────────────────────────
    def set_candidates(self, candidates: list[dict[str, Any]]) -> None:
        self.data["candidates"] = candidates
        self.save()

    def set_bili_status(self, status: dict[str, Any]) -> None:
        self.data["bili_status"] = status
        self.save()

    def set_bucket_plan(self, plan: dict[str, Any]) -> None:
        """这一轮的配额计划（哪块给了几条、实际拿到几条、门槛是否过滤过）。"""
        self.data["bucket_plan"] = plan
        self.save()

    def set_summary(self, summary: dict[str, Any]) -> None:
        self.data["summary"] = summary
        self.save()

    # ── videos ─────────────────────────────────────────────────────
    def get_video(self, bvid: str) -> dict[str, Any] | None:
        for row in self.data["videos"]:
            if row.get("bvid") == bvid:
                return row
        return None

    def add_video(self, bvid: str, **fields: Any) -> dict[str, Any]:
        row = self.get_video(bvid)
        if row is None:
            row = {
                "bvid": bvid,
                "title": "",
                "author": "",
                "author_mid": 0,
                "duration_text": "",
                "pubdate": 0,
                "pubdate_text": "",
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                "source": "",
                "description": "",
                "tags": [],
                "summary": "",
                "key_points": [],
                "hot_comments": [],
                "subtitle_excerpt": "",
                "ai_conclusion": "",
                "comment_text": "",
                "comment_action": "inspected",
                "comment_status": "none",
                # 是否真的"看过内容"（读了字幕/AI总结）—— 额度按这个算，不是按 note 算
                "inspected": False,
                "noted": False,
                "reason": "",
                "safety": {},
                "style": {},
                "draft_id": None,
                "draft_deleted": False,
                "draft_cleanup": {},
                "post_result": {},
                "verify_result": {},
                "seen": False,
                "thinking_highlights": [],
                "tools": [],
            }
            self.data["videos"].append(row)
        for key, value in fields.items():
            if value is None:
                continue
            if key in {"key_points"}:
                row[key] = _as_list(value, 6)
            elif key == "hot_comments":
                row[key] = _as_comment_list(value, 6)
            elif key == "tags":
                row[key] = _as_list(value, 12)
            elif key in {"summary", "subtitle_excerpt", "ai_conclusion", "comment_text",
                         "reason", "title", "author", "duration_text", "source"}:
                row[key] = clip(value, 2400 if key in {"summary", "subtitle_excerpt"} else 600)
            else:
                row[key] = value
        if not row.get("pubdate_text"):
            row["pubdate_text"] = ts_text(row.get("pubdate"))
        self.save()
        return row

    def update_video(self, bvid: str, **fields: Any) -> dict[str, Any]:
        return self.add_video(bvid, **fields)

    # ── findings / thinking / tools ────────────────────────────────
    def add_finding(self, text: str, importance: int = 1) -> None:
        text = clip(text, 1200)
        if not text:
            return
        self.data["findings"].append(
            {"at": now_iso(), "text": text, "importance": int(importance or 1)}
        )
        self.data["findings"] = self.data["findings"][-40:]
        self.save()

    def add_thinking(self, text: str, step: int = 0, kind: str = "assistant") -> None:
        text = clip(text, 4000)
        if not text.strip():
            return
        self.data["thinking"].append(
            {"at": now_iso(), "step": step, "kind": kind, "text": text}
        )
        self.data["thinking"] = self.data["thinking"][-120:]
        self.save()

    def add_tool(
        self,
        name: str,
        args: dict[str, Any] | None,
        ok: bool,
        result: str,
        step: int = 0,
        error: str = "",
    ) -> None:
        self.data["tools"].append(
            {
                "at": now_iso(),
                "step": step,
                "name": name,
                "args": clip(json_dumps(args or {}), 800),
                "ok": bool(ok),
                "result": clip(result, 2500),
                "error": clip(error, 800),
            }
        )
        self.data["tools"] = self.data["tools"][-200:]
        self.save()

    def add_continuation(self, reason: str, step: int = 0, detail: str = "") -> None:
        self.data["continuations"].append(
            {"at": now_iso(), "reason": reason, "step": step, "detail": clip(detail, 500)}
        )
        self.save()

    def add_error(self, where: str, message: str) -> None:
        self.data["errors"].append(
            {"at": now_iso(), "where": where, "message": clip(message, 1200)}
        )
        self.data["errors"] = self.data["errors"][-40:]
        self.save()

    def add_usage(self, prompt_eval_count: int = 0, eval_count: int = 0, calls: int = 1) -> None:
        usage = self.data["usage"]
        usage["calls"] = int(usage.get("calls") or 0) + calls
        usage["prompt_eval_count"] = int(usage.get("prompt_eval_count") or 0) + int(prompt_eval_count or 0)
        usage["eval_count"] = int(usage.get("eval_count") or 0) + int(eval_count or 0)
        self.save()

    def mark_finished(
        self,
        summary: str,
        highlights: list[str] | None = None,
        issues: list[str] | None = None,
        *,
        partial: bool = False,
    ) -> None:
        self.data["finish"] = {
            "summary": clip(summary, 2000),
            "highlights": _as_list(highlights, 8),
            "issues": _as_list(issues, 8),
            "partial": bool(partial),
            "at": now_iso(),
        }
        self.data["finished_at"] = now_iso()
        self.data["partial"] = bool(partial)
        self.data["status"] = "completed" if not partial else "needs_continue"
        self.save()

    def set_status(self, status: str) -> None:
        self.data["status"] = status
        self.save()

    # ── summaries for the small model ──────────────────────────────
    def compact(self, max_chars: int = 6000) -> str:
        videos = []
        for row in self.data.get("videos", []):
            videos.append(
                {
                    "bvid": row.get("bvid"),
                    "title": clip(row.get("title"), 100),
                    "summary": clip(row.get("summary"), 500),
                    "key_points": row.get("key_points", [])[:4],
                    "comment_action": row.get("comment_action"),
                    "comment_status": row.get("comment_status"),
                    "comment_text": clip(row.get("comment_text"), 80),
                    "reason": clip(row.get("reason"), 160),
                }
            )
        payload = {
            "goal": self.data.get("goal"),
            "bili_status": {
                "logged_in": self.data.get("bili_status", {}).get("logged_in"),
                "paused": self.data.get("bili_status", {}).get("paused"),
                "quota_left": self.data.get("bili_status", {}).get("quota_left"),
                "posts_24h": self.data.get("bili_status", {}).get("posts_24h"),
            },
            "candidates": [
                {
                    "bvid": c.get("bvid"),
                    "title": clip(c.get("title"), 90),
                    "source": c.get("source"),
                    "seen": c.get("seen"),
                }
                for c in self.data.get("candidates", [])[:10]
            ],
            "videos": videos,
            "findings": [f.get("text") for f in self.data.get("findings", [])[-5:]],
            "errors": [e.get("message") for e in self.data.get("errors", [])[-3:]],
            "continuations": len(self.data.get("continuations", [])),
            "usage": self.data.get("usage", {}),
        }
        text = json_dumps(payload, indent=1)
        if len(text) > max_chars:
            text = text[: max_chars - 120] + "\n…（记忆已截断，完整内容在 memory.md）"
        return text

    # ── markdown ───────────────────────────────────────────────────
    def render_markdown(self) -> str:
        d = self.data
        lines: list[str] = []
        lines.append(f"# bili-autopilot 记忆：{d.get('run_id')}")
        lines.append("")
        lines.append(f"- 目标：{d.get('goal')}")
        lines.append(f"- 模型：{d.get('model')}")
        lines.append(f"- 模式：write={d.get('write')}")
        lines.append(f"- 状态：{d.get('status')} partial={d.get('partial')}")
        lines.append(f"- 开始：{d.get('started_at')}")
        lines.append(f"- 结束：{d.get('finished_at') or '未结束'}")
        lines.append(f"- 本地 token：{json_dumps(d.get('usage', {}))}")
        lines.append("")
        status = d.get("bili_status") or {}
        if status:
            lines.append("## B站插件状态")
            lines.append(
                f"- 登录：{status.get('logged_in')} / {status.get('uname') or '未知'}；"
                f"暂停：{status.get('paused')}；24h 已发：{status.get('posts_24h')}；"
                f"剩余额度：{status.get('quota_left')}；写入：{status.get('write_enabled')}"
            )
            lines.append("")
        if d.get("candidates"):
            lines.append("## 候选")
            for c in d["candidates"][:12]:
                mark = "已看过" if c.get("seen") else "新"
                lines.append(
                    f"- [{mark}] `{c.get('bvid')}` {c.get('title')} "
                    f"（{c.get('author') or '未知 UP'}，{c.get('source')}，{c.get('pubdate_text')}）"
                )
            lines.append("")
        lines.append("## 看过的视频")
        if not d.get("videos"):
            lines.append("- 无")
        for i, v in enumerate(d.get("videos", []), 1):
            lines.append(f"### {i}. {v.get('title') or v.get('bvid')}")
            lines.append(
                f"- BV：`{v.get('bvid')}`；UP：{v.get('author') or '未知'}；"
                f"时长：{v.get('duration_text') or '未知'}；发布：{v.get('pubdate_text') or '未知'}"
            )
            if v.get("url"):
                lines.append(f"- 链接：{v.get('url')}")
            if v.get("summary"):
                lines.append(f"- 摘要：{v.get('summary')}")
            if v.get("key_points"):
                lines.append("- 要点：" + "；".join(v.get("key_points", [])))
            if v.get("hot_comments"):
                lines.append("- 热评：")
                for c in v["hot_comments"][:4]:
                    lines.append(f"  - {c.get('user') or '匿名'}（{c.get('like', 0)} 赞）：{c.get('content')}")
            if v.get("comment_action") == "comment":
                lines.append(
                    f"- 评论：{v.get('comment_text')}（{v.get('comment_status')}，"
                    f"draft={v.get('draft_id')}）"
                )
            elif v.get("comment_action") == "skip":
                lines.append(f"- 判断：跳过（{v.get('reason') or '无理由'}）")
            else:
                lines.append(f"- 判断：只看不评（{v.get('reason') or '无理由'}）")
            if v.get("safety"):
                lines.append(f"- 安全审：{json_dumps(v.get('safety'), indent=None)}")
            if v.get("verify_result"):
                lines.append(f"- 发送核验：{json_dumps(v.get('verify_result'), indent=None)}")
            lines.append("")
        if d.get("findings"):
            lines.append("## 发现")
            for f in d["findings"][-10:]:
                lines.append(f"- {f.get('text')}")
            lines.append("")
        if d.get("continuations"):
            lines.append("## 自动继续")
            for c in d["continuations"][-10:]:
                lines.append(f"- step {c.get('step')}：{c.get('reason')} {c.get('detail') or ''}")
            lines.append("")
        if d.get("errors"):
            lines.append("## 异常")
            for e in d["errors"][-10:]:
                lines.append(f"- [{e.get('where')}] {e.get('message')}")
            lines.append("")
        finish = d.get("finish") or {}
        if finish:
            lines.append("## 本地模型收尾")
            lines.append(f"- 总结：{finish.get('summary')}")
            if finish.get("highlights"):
                lines.append("- 亮点：")
                for h in finish["highlights"]:
                    lines.append(f"  - {h}")
            if finish.get("issues"):
                lines.append("- 问题：")
                for h in finish["issues"]:
                    lines.append(f"  - {h}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def render_thinking(self) -> str:
        lines = [f"# 本地模型思维链：{self.data.get('run_id')}", ""]
        for item in self.data.get("thinking", []):
            lines.append(f"## step {item.get('step')} · {item.get('at')}")
            lines.append("")
            lines.append(item.get("text") or "")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def thinking_highlights(self, limit: int = 8, width: int = 260) -> list[str]:
        out: list[str] = []
        for item in self.data.get("thinking", [])[-limit:]:
            text = " ".join(str(item.get("text") or "").split())
            if text:
                out.append(clip(text, width))
        return out
