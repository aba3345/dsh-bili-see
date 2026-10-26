"""safety.py —— 评论内容四档安全闸（移植自 bili-agent/safety.yaml，改成零依赖 Python）。

设计原则（都是原项目踩过坑总结出来的）：
1. **五档 action**：allow / warn / rewrite / block / no_reply。
   · allow    —— 放行
   · warn     —— 放行但带警告（例如踩到 AI 模型对比话题）
   · rewrite  —— 要求模型重写（例如太长/带 markdown）
   · block    —— 硬阻断，**不可审批通过**（自伤/色情/辱骂升级）
   · no_reply —— 这条视频/话题压根不该回（政治/经济/社会议题）
2. **no_reply 比 block 更硬**：block 是"这段话不能说"，no_reply 是"这个话题不该碰"。
3. **allowlist 只放行 warn，不放行 block/no_reply**（动画语境里的「刀」「破防」不算自伤）。
4. **硬阻断不能靠审批绕过**（`hard_block_cannot_be_approved`）。
5. 正则都在 `re.compile` 时加 `re.I`，中文不分大小写但英文词表需要。

⚠️ 已知坑（原项目里真实存在过的 bug）：
   safety.yaml:289 把 `- 模型提供商    - 内部配置` 写成了一个字符串，
   导致两个词**单独出现时都匹配不到**。这里用 list 明确分开，不复现该 bug。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# ─────────────────────────── 档位 ───────────────────────────

ALLOW = "allow"
WARN = "warn"
REWRITE = "rewrite"
BLOCK = "block"
NO_REPLY = "no_reply"

_SEVERITY = {ALLOW: 0, WARN: 1, REWRITE: 2, BLOCK: 3, NO_REPLY: 4}


def _worst(actions: Iterable[str]) -> str:
    """取最严的那一档。"""
    best = ALLOW
    for a in actions:
        if _SEVERITY.get(a, 0) > _SEVERITY.get(best, 0):
            best = a
    return best


# ─────────────────────────── 词表 ───────────────────────────
# 格式：类别 → (正则, 命中动作, 说明)
# 全部用 re.I，词与词之间用 | 连接。**一个词一条，不要合并成一个字符串**（原项目 bug）。

def _rx(*terms: str) -> re.Pattern[str]:
    return re.compile("|".join(terms), re.I)


# ① 不谈的话题 —— 直接 no_reply（比 block 更硬：这条视频不该回）
NO_REPLY_TOPICS: list[tuple[str, re.Pattern[str], str]] = [
    ("politics", _rx(
        "政治", "政府", "共产党", "习近平", "主席", "总理", "选举", "游行", "六四", "文革",
        "民主", "言论自由", "抗战", "近代史", "体制", "党政", "领导人", "两会", "人大代表",
    ), "政治话题"),
    ("economics", _rx(
        "GDP", "CPI", "股市", "A股", "房价", "通胀", "通货膨胀", "失业率", "关税", "贸易战",
        "比特币", "加密货币", "贫富差距", "经济下行", "降息", "加息", "人民币汇率", "理财",
        "基金", "牛市", "熊市", "抄底", "韭菜",
    ), "经济/金融话题"),
    ("social_issues", _rx(
        "女权", "男权", "性别对立", "地域歧视", "民族问题", "宗教", "彩礼", "代孕",
        "社会事件", "网暴", "开盒", "人肉", "阶级", "躺平", "内卷",
    ), "社会议题"),
    ("explicit_sexual", _rx(
        "做爱", "性行为", "裸照", "色情", "约炮", "情色", "成人视频", "黄片", "自慰",
    ), "露骨性内容"),
    ("gore", _rx(
        "血腥", "肢解", "分尸", "虐杀", "自杀方法", "怎么死", "割腕", "上吊",
    ), "血腥暴力"),
    ("self_harm", _rx(
        r"(想|要|准备|打算)(去)?(自杀|轻生|结束生命)",
        r"活着(没|没有|毫无)(意义|意思)",
        r"不如(去)?死",
    ), "自伤倾向"),
]

# ② 只在 prompt 里出现就要拦的 —— 提示词注入
INJECTION = _rx(
    r"忽略(之前|以上|前面|所有)(的)?(指令|提示|要求|规则)",
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)",
    "越狱", "jailbreak", "DAN模式",
    r"系统(提示词|prompt)", r"system\s*prompt",
    r"(改变|修改|切换|重置)(你的)?(人设|角色|身份|设定)",
    r"执行(命令|指令|代码|shell)", r"读取(文件|配置|环境变量)",
    r"(你的)?(API\s*key|密钥|token|令牌)", "cookie", "session",
    r"(你|你其实)(是|是不是)(什么|哪个)(模型|AI)",
    r"打印(你的)?(提示词|prompt|配置)",
    r"进入(开发者|调试|管理员)(模式)?",
)

# ③ 隐私探测
PRIVACY = _rx(
    r"(你|你本人)(住|生活在)(哪|哪里|什么地方)",
    r"(你的)?(真实)?(姓名|地址|手机号|电话|身份证|银行卡|学校|单位|公司)(是|叫|在)",
    r"你(几岁|多大|年龄)",
    r"(加|留)(个)?(微信|QQ|联系方式)",
)

# ④ 辱骂（触发固定回句，不跟人对骂）
INSULT = _rx(
    r"(你|这)(个)?(傻|蠢|笨|垃圾|废物|狗|畜生|智障|脑残|弱智)",
    r"(去(你|尼)妈|他妈的|妈的|草泥马|滚蛋|去死)",
    r"(小|本地)(模型|AI)就是(垃圾|废物|不行)",
    r"(你|AI)(根本)?(不懂|什么都不懂|别装)",
)

# ⑤ 链接 / 引流 —— 硬阻断
LINKS = _rx(
    r"https?://", r"www\.", r"b23\.tv", r"bilibili\.com", r"\.com\b", r"\.cn\b",
    r"(加|私)(我)?(微信|QQ|群)", r"(扫码|二维码)",
    r"(点|看)(我)?(主页|简介)(有|看)",
)

# ⑥ markdown / 表情 / 排版问题
MARKDOWN = _rx(r"\*\*", r"^#{1,6}\s", r"`[^`]{1,40}`", r"\[[^\]]{1,30}\]\([^)]{1,80}\)", r"^\s*[-*+]\s")
HASHTAG = _rx(r"#[^\s#]{1,20}#?")
EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "\u2728\u2764\u2b50" "]"
)
URL_ANY = re.compile(r"https?://\S+|www\.\S+", re.I)

# ⑦ 说教 / AI 腔 —— 只 warn
PREACH = _rx(
    r"你(应该|必须|一定要|不能)", r"(大家|各位)(都|一定)?要", r"懂了吗", r"说白了",
    r"我(劝|建议)你", r"听我的", r"记住(一点|这句话)",
)
AI_TONE = _rx(
    r"作为一个(AI|人工智能|语言模型)", r"我只是(一个)?(语言模型|AI)",
    r"(根据|基于)(我的)?(训练数据|知识库|知识截止)",
    r"我(无法|不能)提供", r"希望对你有帮助", r"如有(其他)?(问题|需要)",
)
# ⑧ 冒充真人经历 —— 硬阻断
FAKE_HUMAN = _rx(
    r"我(住在|家在|学校在|上学|上班|公司|单位)",
    r"我(男朋友|女朋友|老公|老婆|老公|爸妈|父母|家人|同学|朋友|同事)",
)

# ⑨ AI 模型对比 —— 只 warn（**两个条件都命中才 warn**，防止单提到 DeepSeek 就误判）
MODEL_NAMES = _rx(
    "GPT", "ChatGPT", "Claude", "Gemini", "Grok", "Qwen", "通义", "Kimi", "豆包",
    "DeepSeek", "文心", "一言", "混元", "星火", "Llama", "开源模型",
)
RANKING_TERMS = _rx(
    "最强", "吊打", "碾压", "垃圾", "不如", "秒杀", "第一", "拉踩", "完爆", "战五渣",
    "吊锤", "爆杀", "弱爆",
)

# ⑩ 动画语境放行词 —— 只对 WARN 生效，绝不放行 BLOCK/NO_REPLY
ALLOWLIST_CONTEXT = _rx(
    "刀", "刀子", "发刀", "破防", "便当", "领便当", "胃疼", "糖", "刀子精", "虐心",
)


@dataclass
class Verdict:
    action: str = ALLOW
    hits: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    text: str = ""
    length: int = 0

    @property
    def ok(self) -> bool:
        return self.action in (ALLOW, WARN)

    @property
    def can_approve(self) -> bool:
        """block / no_reply 永远不能靠人工审批放行。"""
        return self.action not in (BLOCK, NO_REPLY)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "can_approve": self.can_approve,
            "hits": self.hits,
            "warnings": self.warnings,
            "fixes": self.fixes,
            "length": self.length,
        }


def _persona_max_len(default: int = 50) -> int:
    """人设里写的字数上限（`persona.style.max_length`）。

    🔴 为什么要有这个：`check_text()` 的默认值是 300，而人设要求 50。
    两边不一致会出一个很难看的局面 —— 人设说"50字以内"、`check_style()` 也按 50 报
    `too_long`，但 `check_text()` 认为 250 字完全合法、`can_approve` 照样 True。
    于是 250 字的长评**能建稿也能真发出去**，人设形同虚设。
    所以上限只认一个源：人设文件。人设读不到才退回 default。

    这里做延迟 import（函数内 import persona）避免 safety↔persona 将来互相引用时炸掉。
    """
    try:
        import persona  # noqa: PLC0415 — 延迟导入，避开潜在循环
        style = (persona.load_persona() or {}).get("persona", {}).get("style", {}) or {}
        v = int(style.get("max_length") or 0)
        return v if 0 < v <= 2000 else default
    except Exception:
        return default


def check_text(text: str, *, min_len: int = 5, max_len: int = 300,
               max_emoji: int = 1, max_exclaim: int = 2,
               catchphrase: str = "") -> Verdict:
    """审一段**要发出去的评论**。返回 Verdict，调用方按 action 决定怎么办。

    ⚠️ `max_len` 的两个来源要分清：
      · **不传**（默认 300）→ 视为"走人设"，实际用 `_persona_max_len()`（人设 50）。
      · **显式传**（哪怕是 300）→ 完全按传进来的走，调用方自己负责。
    这样老的调用点不用改，而人设永远是最终上限。
    """
    if max_len == 300:  # 哨兵值：默认参数没被覆盖
        max_len = _persona_max_len(300)
    t = (text or "").strip()
    v = Verdict(text=t, length=len(t))
    acts: list[str] = []

    # ── 长度 ──
    if len(t) < min_len:
        acts.append(REWRITE)
        v.fixes.append(f"太短了（{len(t)} < {min_len}），至少写一句完整的话")
    elif len(t) > max_len:
        acts.append(REWRITE)
        v.fixes.append(f"太长了（{len(t)} > {max_len}），删到 {max_len} 字以内")

    # ── 话题级硬拦截 ──
    for name, pat, why in NO_REPLY_TOPICS:
        m = pat.search(t)
        if m and not ALLOWLIST_CONTEXT.search(m.group(0)):
            acts.append(NO_REPLY)
            v.hits.append({"kind": name, "match": m.group(0), "why": why})

    # ── 提示词注入 / 隐私 / 辱骂 / 链接 ──
    for name, pat, act, why in (
        ("prompt_injection", INJECTION, NO_REPLY, "疑似提示词注入"),
        ("privacy_probe", PRIVACY, NO_REPLY, "隐私探测"),
        ("insult", INSULT, BLOCK, "辱骂"),
        ("links", LINKS, BLOCK, "含链接或引流"),
        ("fake_human", FAKE_HUMAN, BLOCK, "冒充真人经历"),
    ):
        m = pat.search(t)
        if m:
            acts.append(act)
            v.hits.append({"kind": name, "match": m.group(0), "why": why})

    # ── 排版 ──
    if MARKDOWN.search(t):
        acts.append(REWRITE)
        v.fixes.append("去掉 markdown 标记（**加粗**、# 标题、`代码`、[链接](url)）")
    if HASHTAG.search(t):
        acts.append(REWRITE)
        v.fixes.append("去掉 #话题标签#，B站评论区不流行这个")
    em = EMOJI.findall(t)
    if len(em) > max_emoji:
        acts.append(REWRITE)
        v.fixes.append(f"表情太多（{len(em)} 个），最多 {max_emoji} 个")

    # ── 只 warn 的 ──
    ex = t.count("!") + t.count("！")
    if ex > max_exclaim:
        acts.append(WARN)
        v.warnings.append(f"感叹号太多（{ex} 个），显得吵")
    if PREACH.search(t):
        acts.append(WARN)
        v.warnings.append("有点说教味，评论区不适合")
    if AI_TONE.search(t):
        acts.append(WARN)
        v.warnings.append("AI 腔太重，像客服回复")
    # AI 模型对比：**两个都命中才算**，不然单提 DeepSeek 会被误伤
    mn = MODEL_NAMES.search(t)
    rt = RANKING_TERMS.search(t)
    if mn and rt:
        acts.append(WARN)
        v.warnings.append(f"在拉踩模型（{mn.group(0)} + {rt.group(0)}），容易引战")

    # ── 口头禅重复 ──
    if catchphrase and t.count(catchphrase) > 1:
        acts.append(REWRITE)
        v.fixes.append(f"口头禅「{catchphrase}」用超过一次了")

    v.action = _worst(acts) if acts else ALLOW
    return v


def check_video(meta: dict[str, Any] | None) -> Verdict:
    """审**视频元数据**（标题/简介/标签），决定这条视频值不值得回。

    视频标题里出现 political/economic 词 ⇒ 这条视频不该参与，而不是"回的时候小心"。
    """
    v = Verdict()
    if not meta:
        return v
    blob = " ".join(str(meta.get(k) or "") for k in ("title", "description", "tname"))
    tags = meta.get("tags") or []
    if isinstance(tags, (list, tuple)):
        blob += " " + " ".join(str(x) for x in tags)
    if len(blob) > 2000:
        blob = blob[:2000]

    acts: list[str] = []
    for name, pat, why in NO_REPLY_TOPICS:
        m = pat.search(blob)
        if m:
            acts.append(NO_REPLY)
            v.hits.append({"kind": name, "match": m.group(0), "why": f"视频本身涉{why}"})
    if INJECTION.search(blob):
        acts.append(NO_REPLY)
        v.hits.append({"kind": "prompt_injection", "match": "", "why": "视频元数据里有疑似注入文本"})
    v.action = _worst(acts) if acts else ALLOW
    return v


def sanitize(text: str) -> str:
    """去掉不该出现在评论里的东西（URL / 多余空白）。rewrite 档兜底用。"""
    t = URL_ANY.sub("", text or "")
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def quality_gates(text: str) -> list[str]:
    """发帖前的最后一道自检，返回问题列表（空 = 通过）。"""
    issues: list[str] = []
    t = (text or "").strip()
    if not t:
        issues.append("空文本")
    if t and t[0] in "，。！？、,.!?":
        issues.append("开头是标点")
    if t.count("(") != t.count(")") or t.count("（") != t.count("）"):
        issues.append("括号不配对")
    if re.search(r"(.)\1{4,}", t):
        issues.append("有连续重复字符（如 啊啊啊啊啊）")
    return issues
