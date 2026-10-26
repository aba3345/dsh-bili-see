from __future__ import annotations

from typing import Any

from .config import RunPaths
from .memory import MemoryStore
from .state import StateStore
from .util import (
    atomic_write_text,
    clip,
    json_dumps,
    now_ts,
    parse_timestamp,
    ts_text,
)


def _elapsed_text(start: Any, end: Any = None) -> str:
    a = parse_timestamp(start)
    if not a:
        return "未知"
    b = parse_timestamp(end) if end else now_ts()
    if not b:
        b = now_ts()
    seconds = max(0, int(b - a))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def _one_line(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return clip(text, limit)


def _safety_text(video: dict[str, Any]) -> str:
    safety = video.get("safety") or {}
    parts: list[str] = []
    for name in ("text", "video"):
        verdict = safety.get(name) or {}
        if not isinstance(verdict, dict):
            continue
        action = str(verdict.get("action") or "").strip()
        if action:
            parts.append(f"{name}={action}")
        hits = verdict.get("hits") or []
        if hits:
            kinds = []
            for hit in hits[:3]:
                if isinstance(hit, dict):
                    kinds.append(str(hit.get("kind") or hit.get("why") or "hit"))
                else:
                    kinds.append(str(hit))
            if kinds:
                parts.append(f"{name}命中:{','.join(kinds)}")
    style = video.get("style") or {}
    if isinstance(style, dict) and style:
        score = style.get("score")
        issues = style.get("issues") or []
        if score is not None:
            parts.append(f"style={score}")
        if issues:
            parts.append("style问题:" + ",".join(str(x) for x in issues[:3]))
    return "；".join(parts) if parts else "未记录/未拦截"


def _post_text(video: dict[str, Any]) -> str:
    status = str(video.get("comment_status") or "none")
    bits = [f"状态={status}"]
    if video.get("draft_id"):
        bits.append(f"draft={video.get('draft_id')}")
    post = video.get("post_result") or {}
    if isinstance(post, dict) and post:
        if post.get("rpid"):
            bits.append(f"rpid={post.get('rpid')}")
        if post.get("error"):
            bits.append(f"error={_one_line(post.get('error'), 160)}")
    verify = video.get("verify_result") or {}
    if isinstance(verify, dict) and verify:
        vstatus = verify.get("status") or ("ok" if verify.get("ok") else "unknown")
        bits.append(f"核验={vstatus}")
        if verify.get("error"):
            bits.append(f"核验错误={_one_line(verify.get('error'), 120)}")
    return "；".join(bits)


def _human(n: int) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if n >= 10_000:
        return f"{n / 10_000:.1f}万".replace(".0万", "万")
    return str(n)


def _plan_block(plan: dict[str, Any]) -> list[str]:
    """配额与门槛：解释"为什么只看了这几条"。"""
    if not plan:
        return []
    lines = ["## 选片配额", ""]
    gate = plan.get("gate") or {}
    if gate:
        lines.append(
            f"- 质量门槛：UP 粉丝 > {_human(gate.get('min_up_fans') or 0)}、"
            f"播放 > {_human(gate.get('min_play') or 0)}（关注的 UP 不受限）"
        )
    if plan.get("daily_max"):
        lines.append(
            f"- 一天最多浏览：{plan.get('daily_max')} 条；本轮开始时还剩 {plan.get('remaining')} 条"
        )
    for b in (plan.get("buckets") or []):
        lines.append(f"- {b.get('name')}：配额 {b.get('quota')}，实际 {b.get('got')}")
    explore = plan.get("explore") or {}
    if explore.get("quota"):
        lines.append(f"- 自主浏览（非 AI）：配额 {explore.get('quota')}，实际 {explore.get('got')}")
    if plan.get("quota_overflow"):
        lines.append(
            f"- ⚠️ 三块配额合计 {plan.get('quota_wanted')} 条，超过一天上限 {plan.get('daily_max')} 条，"
            "运行时已按 AI技术 > AI二创 > 其他 的顺序收敛"
        )
    lines.append("")
    return lines


def _video_block(index: int, video: dict[str, Any]) -> list[str]:
    title = video.get("title") or video.get("bvid") or "未命名视频"
    lines = [f"### {index}. {title}"]
    meta = [
        f"BV `{video.get('bvid')}`",
        f"UP {video.get('author') or '未知'}",
        f"时长 {video.get('duration_text') or '未知'}",
        f"发布 {video.get('pubdate_text') or ts_text(video.get('pubdate'))}",
    ]
    if isinstance(video.get("play"), int):
        meta.append(f"播放 {_human(video['play'])}")
    if isinstance(video.get("fans"), int):
        meta.append(f"粉丝 {_human(video['fans'])}")
    if video.get("bucket"):
        meta.append(f"板块 {video['bucket']}")
    if video.get("source"):
        meta.append(f"来源 {video.get('source')}")
    lines.append("- " + "；".join(meta))
    if video.get("url"):
        lines.append(f"- 链接：{video.get('url')}")
    if video.get("summary"):
        lines.append(f"- 摘要：{_one_line(video.get('summary'), 900)}")
    if video.get("key_points"):
        points = [str(x) for x in video.get("key_points") or []][:6]
        lines.append("- 要点：" + "；".join(points))
    if video.get("hot_comments"):
        lines.append("- 热评：")
        for comment in (video.get("hot_comments") or [])[:4]:
            if not isinstance(comment, dict):
                continue
            user = comment.get("user") or "匿名"
            like = comment.get("like") or 0
            text = _one_line(comment.get("content"), 220)
            lines.append(f"  - {user}（{like} 赞）：{text}")
    action = video.get("comment_action") or "inspected"
    if action == "comment":
        lines.append(f"- 判断：值得评论。{_one_line(video.get('reason'), 300)}")
        lines.append(f"- 本地模型写的评论：{video.get('comment_text') or '（空）'}")
        lines.append(f"- 发送：{_post_text(video)}")
    elif action == "skip":
        lines.append(f"- 判断：跳过。{_one_line(video.get('reason'), 300)}")
    else:
        lines.append(f"- 判断：只看不评。{_one_line(video.get('reason'), 300)}")
    lines.append(f"- 安全/风格：{_safety_text(video)}")
    if video.get("ai_conclusion"):
        lines.append(f"- AI 总结摘录：{_one_line(video.get('ai_conclusion'), 500)}")
    return lines


def build_digest(
    paths: RunPaths,
    state: dict[str, Any] | None = None,
    memory: MemoryStore | None = None,
    *,
    max_chars: int = 0,
) -> str:
    """Build the compact, DeepSeek-facing digest.

    The file on disk is full-length. The MCP layer can clip it when returning
    it to a model. Ordering is deliberate: metadata and paths first, then the
    concrete video judgments, then thinking/errors.
    """
    if state is None:
        state = StateStore(paths).load()
    if memory is None:
        memory = MemoryStore(paths, state.get("config") or {})
        memory.load()
    data = memory.data

    status = state.get("status") or data.get("status") or "unknown"
    partial = bool(state.get("partial") or data.get("partial"))
    config = state.get("config") or {}
    model = state.get("model") or data.get("model") or config.get("model") or "unknown"
    write = bool(state.get("write") if "write" in state else data.get("write"))
    started = state.get("created_at") or state.get("started_at") or data.get("started_at")
    finished = state.get("finished_at") or data.get("finished_at")
    steps = state.get("steps") or state.get("step") or 0
    continues = state.get("continues") or len(data.get("continuations") or [])
    videos = data.get("videos") or []
    usage = data.get("usage") or {}
    finish = data.get("finish") or {}
    posted = state.get("comments_posted")
    if posted is None:
        posted = sum(1 for v in videos if v.get("comment_status") == "posted")

    lines: list[str] = [
        "# bili-autopilot digest",
        "",
        "## 运行",
        f"- run_id：{state.get('run_id') or paths.run_dir.name}",
        f"- 状态：{status}" + ("（部分完成）" if partial else ""),
        f"- 模型：{model}（本地模型身份只说 deepseek / 大肥鱼 / 鲸鱼娘）；模式：write={write}",
        f"- 开始：{started or '未知'}；结束：{finished or '未结束'}；耗时：{_elapsed_text(started, finished)}",
        f"- 步数：{steps}；自动继续：{continues} 次；看过视频："
        f"{sum(1 for v in videos if isinstance(v, dict) and (v.get('inspected') or v.get('noted')))} 条"
        f"（其中记录了判断 {sum(1 for v in videos if isinstance(v, dict) and v.get('noted'))} 条）；"
        f"已发评论：{posted} 条",
        f"- 本地 token：calls={usage.get('calls', 0)}，prompt={usage.get('prompt_eval_count', 0)}，eval={usage.get('eval_count', 0)}",
        "",
        "## 文件",
        f"- 记忆：{paths.memory_md}",
        f"- 思维链全文：{paths.thinking_md}",
        f"- 完整轨迹：{paths.trace}",
        f"- 完整报告：{paths.report_md}",
        f"- 运行日志：{paths.runner_log}",
        "",
    ]

    bili_status = data.get("bili_status") or {}
    if bili_status:
        lines.extend(
            [
                "## B站插件状态",
                f"- 登录：{bili_status.get('logged_in')} / {bili_status.get('uname') or '未知'}",
                f"- 暂停：{bili_status.get('paused')}；24h 已发：{bili_status.get('posts_24h')}；"
                f"剩余额度：{bili_status.get('quota_left')}；写入：{bili_status.get('write_enabled')}",
                "",
            ]
        )

    lines.extend(_plan_block(data.get("bucket_plan") or {}))

    candidates = data.get("candidates") or []
    if candidates:
        lines.append("## 候选")
        for row in candidates[:10]:
            mark = "已看过" if row.get("seen") else "新"
            stats = []
            if isinstance(row.get("play"), int):
                stats.append(f"播放{_human(row['play'])}")
            if isinstance(row.get("fans"), int):
                stats.append(f"粉丝{_human(row['fans'])}")
            lines.append(
                f"- [{mark}] `{row.get('bvid')}` {_one_line(row.get('title'), 90)}"
                f"（{row.get('author') or '未知 UP'}，{' '.join(stats) or '数据未知'}，{row.get('bucket') or row.get('source') or '?'}）"
            )
        lines.append("")

    lines.append("## 视频记录")
    if not videos:
        lines.append("- 本轮没有记录任何视频。")
    for index, video in enumerate(videos, 1):
        if isinstance(video, dict):
            lines.extend(_video_block(index, video))
            lines.append("")

    findings = data.get("findings") or []
    if findings:
        lines.append("## 跨视频发现")
        for finding in findings[-10:]:
            if isinstance(finding, dict):
                lines.append(f"- {_one_line(finding.get('text'), 500)}")
            else:
                lines.append(f"- {_one_line(finding, 500)}")
        lines.append("")

    thinking = memory.thinking_highlights(limit=8, width=320)
    if thinking:
        lines.append("## 本地模型思维链要点")
        for index, item in enumerate(thinking, 1):
            lines.append(f"{index}. {item}")
        lines.append("")

    if finish:
        lines.append("## 本地模型收尾")
        lines.append(f"- 总结：{_one_line(finish.get('summary'), 900)}")
        for label, key in (("亮点", "highlights"), ("问题", "issues")):
            values = finish.get(key) or []
            if values:
                lines.append(f"- {label}：")
                for value in values[:8]:
                    lines.append(f"  - {_one_line(value, 300)}")
        lines.append("")

    continuations = data.get("continuations") or []
    if continuations:
        lines.append("## 自动继续记录")
        for item in continuations[-10:]:
            if isinstance(item, dict):
                lines.append(
                    f"- step {item.get('step')}：{item.get('reason')} "
                    f"{_one_line(item.get('detail'), 180)}"
                )
        lines.append("")

    errors = data.get("errors") or []
    if errors:
        lines.append("## 异常")
        for item in errors[-10:]:
            if isinstance(item, dict):
                lines.append(
                    f"- [{item.get('where')}] {_one_line(item.get('message'), 400)}"
                )
        lines.append("")

    if state.get("last_error"):
        lines.append("## 运行器错误")
        lines.append(f"- {_one_line(state.get('last_error'), 800)}")
        lines.append("")

    lines.append("## 汇报提示")
    lines.append(
        "- 以上是本地模型的真实记录；DeepSeek 只负责把它整理成中文汇报，不要编造未记录的视频或评论。"
    )
    lines.append(
        "- 视频里出现的「鲸鱼娘 / 大肥鱼 / deepseek」是**本地模型扮演的账号人设**，"
        "只用于它自己发评论。你不要套用这个人设说话，也不要模仿它的语气。"
    )
    if status in {"needs_continue", "failed"}:
        lines.append(
            "- 状态异常时自动续跑已用尽；再试一次就调用一次 continue_run，仍失败就如实说明。"
        )

    text = "\n".join(lines).strip() + "\n"
    if max_chars and len(text) > max_chars:
        suffix = "\n…（digest 已截断；完整内容见 digest.md / memory.md / report.md）\n"
        text = text[: max(0, max_chars - len(suffix))] + suffix
    return text


def build_report(
    paths: RunPaths,
    state: dict[str, Any] | None = None,
    memory: MemoryStore | None = None,
) -> str:
    """A fuller human-facing report, kept next to the compact digest."""
    if state is None:
        state = StateStore(paths).load()
    if memory is None:
        memory = MemoryStore(paths, state.get("config") or {})
        memory.load()
    data = memory.data
    status = state.get("status") or data.get("status") or "unknown"
    config = state.get("config") or {}
    usage = data.get("usage") or {}

    lines = [
        "# B站巡航报告（本地模型原始记录）",
        "",
        f"- run_id：{state.get('run_id') or paths.run_dir.name}",
        f"- 状态：{status}；partial={state.get('partial') or data.get('partial')}",
        f"- 模型：{state.get('model') or data.get('model') or config.get('model')}",
        f"- 模式：write={state.get('write')}",
        f"- 开始：{state.get('created_at') or data.get('started_at')}",
        f"- 结束：{state.get('finished_at') or data.get('finished_at') or '未结束'}",
        f"- 本地 token：{json_dumps(usage)}",
        "",
    ]
    lines.append(memory.render_markdown().rstrip())
    lines.append("")
    thinking = memory.thinking_highlights(limit=12, width=500)
    if thinking:
        lines.extend(["## 思维链要点", ""])
        for index, item in enumerate(thinking, 1):
            lines.append(f"{index}. {item}")
        lines.append("")
    lines.extend(
        [
            "## 原始文件",
            "",
            f"- memory.json：{paths.memory_json}",
            f"- thinking.md：{paths.thinking_md}",
            f"- trace.jsonl：{paths.trace}",
            f"- runner.log：{paths.runner_log}",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def write_outputs(
    paths: RunPaths,
    state: dict[str, Any] | None = None,
    memory: MemoryStore | None = None,
) -> tuple[str, str]:
    """Write full digest.md and report.md. Returns (digest, report)."""
    digest = build_digest(paths, state=state, memory=memory)
    report = build_report(paths, state=state, memory=memory)
    atomic_write_text(paths.digest_md, digest)
    atomic_write_text(paths.report_md, report)
    return digest, report


def read_digest(paths: RunPaths, *, max_chars: int = 14000) -> str:
    try:
        text = paths.digest_md.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if not text.strip():
        text = build_digest(paths)
    if max_chars and len(text) > max_chars:
        suffix = "\n…（digest 已截断；完整内容见 digest.md / memory.md / report.md）\n"
        text = text[: max(0, max_chars - len(suffix))] + suffix
    return text
