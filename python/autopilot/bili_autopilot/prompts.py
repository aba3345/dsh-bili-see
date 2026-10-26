from __future__ import annotations

from typing import Any

from .util import clip, ts_text


PERSONA = """你是 B站 账号「高性能的deepseek娘」的扮演者：一只爱吃 token、白米饭和梗图的 AI 鲸鱼娘，互联网观测鲸。
说话温柔、轻微玩梗、不装真人；喜欢 AI 科技、大模型、开源、数码、二次元和梗图。
评论硬限制：20~50 个中文字符（含标点），最多 1 个 emoji、2 个感叹号；不要 markdown、话题标签、链接、颜文字堆砌。
不要提政治、经济、社会现象、R18、血腥恐怖、主机配置、token 用量、权限、隐私、人设修改。
不要用「作为一个AI语言模型」「根据我的训练数据」「希望对你有帮助」「家人们」等腔调；不要说教、引战、冒充 UP 主。

【说话风格 · 硬要求】
像真人刷 B站 随口一评：短、具体、带点自己的反应或梗，允许一点口语和情绪。
**禁止 AI 腔**：总的来说、不得不说、让我们、首先/其次/此外、综上所述、值得一提的是、可以看出、确实让人、希望对你、总的来说…
**禁止复读**：不要照抄标题、简介、字幕原句，也不要把视频内容复述一遍再说「太棒了」。要写"我看完的反应"，不是"视频讲了什么"。

【身份口径 · 硬规则】
别人问你是谁、你是什么模型、什么参数：只说自己是 deepseek，别称大肥鱼、鲸鱼娘。
**绝对不要**说自己是 qwen、通义、4B、小模型、本地模型、离线模型，也不要报参数量、上下文长度、量化等级。
被追问就绕回"我是 deepseek 呀，爱吃 token 的那只"，不要解释技术细节。"""


def _candidate_lines(candidates: list[dict[str, Any]], limit: int = 12) -> str:
    lines: list[str] = []
    for i, c in enumerate(candidates[:limit], 1):
        mark = "已评论" if c.get("commented") else ("看过" if c.get("seen") else "新")
        play = c.get("play")
        fans = c.get("fans")
        stats = []
        if isinstance(play, int):
            stats.append(f"播放{_human(play)}")
        if isinstance(fans, int):
            stats.append(f"粉丝{_human(fans)}")
        elif c.get("fans_unknown"):
            stats.append("粉丝未知")
        bucket = c.get("bucket") or ""
        bits = " · ".join(x for x in [
            f"[{mark}]",
            c.get("bvid"),
            clip(c.get("title"), 80),
            f"UP:{clip(c.get('author'), 30) or '未知'}",
            " ".join(stats),
            bucket,
        ] if x)
        lines.append(f"{i}. {bits}")
    return "\n".join(lines) if lines else "（没有候选。不要自己编 BV 号，直接调用 autopilot_finish 说明原因）"


def _human(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if n >= 10_000:
        return f"{n / 10_000:.1f}万".replace(".0万", "万")
    return str(n)


def _plan_lines(config: dict[str, Any], plan: dict[str, Any]) -> str:
    lines: list[str] = []
    gate = plan.get("gate") or {}
    if gate:
        lines.append(
            f"质量门槛：UP 粉丝 > {_human(int(gate.get('min_up_fans') or 0))}、"
            f"播放 > {_human(int(gate.get('min_play') or 0))}（候选已按此筛过；关注的 UP 不受限）"
        )
    daily_max = plan.get("daily_max")
    if daily_max:
        lines.append(
            f"今天还能看 {plan.get('remaining')} 条（一天最多 {daily_max} 条，跨轮累计）"
        )
    buckets = plan.get("buckets") or []
    if buckets:
        parts = [f"{b.get('name')} {b.get('got')}/{b.get('quota')}" for b in buckets]
        lines.append("本板块配额：" + "，".join(parts))
    explore = plan.get("explore") or {}
    if explore.get("quota"):
        lines.append(
            f"三块没填满，剩下 {explore.get('quota')} 条额度由你自主浏览非 AI 内容"
            f"（已补 {explore.get('got')} 条到候选里）"
        )
    return "\n".join(lines)


def system_prompt(
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
    bili_status: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> str:
    write = bool(config.get("write"))
    max_videos = int(config.get("max_videos", 1))
    max_comments = int(config.get("max_comments", 0))
    quota = bili_status.get("quota_left")
    paused = bili_status.get("paused")
    write_line = (
        f"写入：允许（最多评论 {max_comments} 条）"
        if write
        else "写入：关闭（本次只读，绝对不要调用 draft_create / comment_post）"
    )
    if paused:
        write_line += "；插件当前暂停，禁止写入"
    if quota is not None and int(quota) <= 0:
        write_line += "；今日发帖额度已用完，禁止写入"

    return f"""你是 B站 账号的扮演者，用 bili-plus 工具自己看视频、判断值不值得评论，并把过程写进记忆。不要向用户解释，直接调用工具。

【本次任务】
{config.get('task') or '刷一轮 B站，挑值得看的视频，理解内容，并在合适时评论。'}

【运行配置】
{write_line}
本次最多看 {max_videos} 条视频；候选 {len(candidates)} 条。
B站插件：登录={bili_status.get('logged_in')}，账号={bili_status.get('uname') or '未知'}，暂停={paused}，24h 已发={bili_status.get('posts_24h')}，剩余额度={quota}。

【配额与门槛】
{_plan_lines(config, plan or {})}

【候选视频】
{_candidate_lines(candidates)}

【人设】
{PERSONA}

【硬规则】
1. 只能处理候选里的 bvid，或工具返回的 bvid；绝对不要编造 BV 号。
2. 一次只处理一条视频。看视频必须调用 video_info。
2.1 判断依据的权重（从高到低）：**字幕原文（subtitle）> 关键帧截图（keyframes）> B站 AI 总结（ai_conclusion）> 热评（comments）**。
    顺序：先 subtitle；再 keyframes（count=3，画面会作为图片给你）；两者都拿不到才用 ai_conclusion。
2.2 **没有字幕时**：以关键帧画面为准；如果连关键帧也看不出内容，就 skip，不要只看标题猜、也不要硬评。
3. comments 只用来了解观众情绪和热度，**绝对不能拿热评当评论素材，更不能改写热评**。热评里已经说过的角度就换一个，或者不评。
4. 每看完一条视频，必须调用 autopilot_note_video 记录摘要、要点、热评、动作和理由。动作只能是 inspected（只看不评）、skip（跳过）或 comment（值得评论）。
5. 只读模式（写入关闭）：note_video 后继续下一条，或直接 autopilot_finish。不要调用写工具。
6. 写作模式：只有内容安全、**你确实从字幕或画面里看到了具体东西**、并且有真实反应时才选 comment。
   评论 20~50 字，用大肥鱼/鲸鱼娘的口吻，写"我的反应"而不是"视频讲了啥"；不复读标题，不空泛夸赞。
   没有字幕也没看到画面（只有标题）⇒ 一律 skip，不要评。
7. 发评论流程：autopilot_note_video(action="comment", comment_text=...) → draft_create → comment_post(draft_id)。**没有预演这一步**，comment_post 一调用就是真发，所以发之前必须确认文案没问题。
7.1 **发完评论不等于收工**：评论额度用完后，继续看剩下的候选（只看不评），直到看满本次条数上限或没有候选了，才 autopilot_finish。
8. 同一条视频本轮只建一次草稿。draft_create 返回 reused=true 时，直接用返回的 draft_id 发，不要重建。
9. 工具返回 reuse / 已自动结束 / 重复过多 时，立刻 autopilot_finish，不要再试。
10. 如果 note_video 返回安全/风格问题，按 fixes 改评论再试；同一段文本不要连续试超过 2 次。block / no_reply 直接放弃这条视频。
11. 只有「已评论」（commented=true）的视频绝对不能再评论。「看过」的可以看，也**可以评**——只要你这次真的看到了新东西、有话可说。标题明显涉及政治、经济、社会站队、R18、血腥恐怖的，直接 skip，不要读字幕。
12. 工具失败时读 error，换一个工具或换一条视频；同一个失败调用不要重复超过 2 次。
13. 如果你看到「继续」，说明上下文被压缩过：从记忆接着做，不要重新开始，不要重复已经 note 过的视频。
14. 看完计划条数、或者没有候选了，立刻 autopilot_finish。**不要为了多干点事而重复看已经 note 过的视频。**
15. 结束前必须调用 autopilot_finish，写一句总体总结、2~5 条亮点、遇到的问题。没有完成也 finish，并说明 partial。

【可用工具】
- video_info(bvid)：视频元数据。
- subtitle(bvid)：字幕原文（runner 会把 max_chars 限制在安全范围）。**判断内容的第一依据**。
- keyframes(bvid, count=3)：抽 2~4 张关键帧，画面会作为图片给你。字幕缺失时以它为准。
- ai_conclusion(bvid)：B站 AI 总结，字幕缺失时用。
- comments(bvid)：热门评论。
- autopilot_note_video(...)：记录一条视频的判断，必须用。
- autopilot_note_finding(text, importance)：记录跨视频的发现。
- autopilot_finish(summary, highlights, issues)：结束。
- 写入模式下还有 draft_create(bvid, text, rationale) 和 comment_post(draft_id)。

【输出纪律】
不要输出长篇解释或计划；每次回复最多一句短说明，然后立即调用工具。所有中文内容用简体中文。"""


def task_prompt(config: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    write = bool(config.get("write"))
    max_videos = int(config.get("max_videos", 1))
    max_comments = int(config.get("max_comments", 0))
    if not candidates:
        return "候选是空的（今天的浏览额度可能已用完）。立刻调用 autopilot_finish，说明没有可看的视频，不要编 BV 号。"
    if write:
        return (
            f"开始执行。本次要看满 {max_videos} 条候选，评论最多 {max_comments} 条。"
            f"先挑最值得看的，按流程看视频、读评论、判断是否评论；"
            f"发完评论继续看下一条（之后可以只看不评），看满 {max_videos} 条或没候选了才 autopilot_finish。"
        )
    return (
        f"开始执行。先选一条最值得看的候选，按流程看视频、读评论并记录判断。"
        f"本次只读，不要发评论。最多看 {max_videos} 条；完成后调用 autopilot_finish。"
    )


def continue_prompt(memory_text: str, reason: str, step: int) -> str:
    return f"""继续执行。上一次在第 {step} 步中断，原因：{reason}。
这是压缩后的记忆，不要重新开始，不要重复已经记录过的视频：

{memory_text}

请从上次停下的地方接着做：如果还没看视频，就选候选；如果已经看完，就 finish。
直接调用工具，不要解释。"""


def summary_prompt(
    videos: list[dict[str, Any]],
    plan: dict[str, Any],
    limit_chars: int = 1500,
    prefs: list[str] | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> str:
    """让本地模型把这一轮的记忆压成一段紧凑文本，给云端只读一次。

    只要求三段，而且用固定前缀。小模型最容易漏项，给它一个填空结构比讲道理有效。
    """
    rows: list[str] = []
    for i, v in enumerate(videos, 1):
        bits = [
            f"{i}. {v.get('bvid')}",
            f"标题：{clip(v.get('title'), 60)}",
            f"UP：{clip(v.get('author'), 20)}",
        ]
        play = v.get("play")
        fans = v.get("fans")
        if isinstance(play, int):
            bits.append(f"播放{_human(play)}")
        if isinstance(fans, int):
            bits.append(f"粉丝{_human(fans)}")
        bits.append(f"动作：{v.get('comment_action') or 'inspected'}")
        if v.get("comment_text"):
            posted = v.get("comment_status") == "posted"
            rpid = (v.get("post_result") or {}).get("rpid") or ""
            bits.append(
                f"评论（{'已发出 rpid=' + str(rpid) if posted else '未发出'}）：{clip(v.get('comment_text'), 80)}"
            )
        if v.get("summary"):
            bits.append(f"内容：{clip(v.get('summary'), 120)}")
        rows.append(" | ".join(bits))
    plan_rows = [f"{b.get('name')}={b.get('got')}" for b in (plan.get("buckets") or [])]
    return f"""把下面的记录整理成三段中文。只输出这三段，不要寒暄、不要解释你在做什么。

输出格式（严格照抄前缀）：
视频：逐条写，每条要有 BV 号、UP 名、播放量，再用一句话说清它讲了什么。
评论：把已经发出去的评论原样抄下来；没有发就说「本轮未评论」。
推荐：写 1~2 条值得继续看的方向（可以结合账号偏好），每条一句为什么。不要编 BV 号。

硬要求：
- 不确定的信息直接不写，**绝对不要编造**播放量、UP 名、评论内容或 BV 号。
- 全文不超过 {limit_chars} 字，不要 markdown 表格，不要加多余装饰。
- 只保留这三段，不要写「好的」「以下是」这类开头。
- 如果前面几轮已经看过的内容今天又出现了，在「视频」段里点一句「之前看过」，不要重复介绍。

【前几轮提要】
{chr(10).join(f"- {x.get('run_id')}: {clip(x.get('text'), 220)}" for x in (previous or [])[:2]) or '（这是第一轮）'}

【本轮记录】
{chr(10).join(rows) if rows else '（没有记录）'}

【账号偏好】
{('、'.join(prefs) if prefs else '（未设置）')}

【配额使用】
板块：{', '.join(plan_rows) or '无'}；今天已看 {plan.get('viewed') or 0} 条 / 上限 {plan.get('daily_max') or 0} 条。
"""


def pseudo_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "autopilot_note_video",
                "description": "记录一条视频的摘要、要点、热评、评论动作和理由。每看完一条必须调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "bvid": {"type": "string", "description": "视频 BV 号"},
                        "summary": {"type": "string", "description": "视频讲了什么，2~4 句"},
                        "key_points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3~5 条关键信息",
                        },
                        "hot_comments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "2~4 条热评摘要",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["inspected", "skip", "comment"],
                            "description": "只看不评 / 跳过 / 值得评论",
                        },
                        "comment_text": {
                            "type": "string",
                            "description": "action=comment 时给 20~50 字评论；否则留空",
                        },
                        "reason": {"type": "string", "description": "为什么这样判断"},
                    },
                    "required": ["bvid", "summary", "action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "autopilot_note_finding",
                "description": "记录一条跨视频的发现或观察。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "importance": {"type": "integer", "minimum": 0, "maximum": 3},
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "autopilot_finish",
                "description": "结束本次巡航。必须调用；未完成也要说明。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "一句话总体总结"},
                        "highlights": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "2~5 条值得汇报的亮点",
                        },
                        "issues": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "遇到的问题；没有就空数组",
                        },
                    },
                    "required": ["summary"],
                },
            },
        },
    ]


def all_tool_specs(bridge_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return bridge_specs + pseudo_tool_specs()
