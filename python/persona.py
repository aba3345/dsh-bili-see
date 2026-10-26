"""
persona.py —— 人设：把 persona.yaml 读成提示块 + 风格检查
============================================================================
两个职责，别混：

  ① **提示**：给宿主模型一个「你以谁的身份说话」的段落（`persona_prompt()`）。
     只管语气和身份，**不含任何安全判断** —— 安全在 safety.py。
  ② **检查**：模型写完了，看看像不像这个人（`check_style()`）。

两条实现约束：

  · `banned_phrases` 用**子串**匹配，不用正则。词表是给人改的，
    正则会被改出语法错误；子串慢一点，但改不坏。
  · **没有 PyYAML 也要能跑**。人设是插件启动就要用的东西，
    所以带一个只认本文件 schema 的极简解析器兜底。
============================================================================
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

# ── 缓存：文件改了自动重载（不重启 dsh 就能改人设）─────────────
_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": None, "raw": ""}


def _persona_path() -> str:
    """人设文件在哪：环境变量优先，其次家目录，最后包内 config/。

    🔴 别把最后一步写成 `dirname(here)/config/persona.yaml`：
       生产环境 `python/*.py` 是被 `lib/state.mjs#syncPythonFiles()` **平铺**进家目录的，
       那时 `here` 就是家目录本身，`dirname(here)` 指向 `<DSH_HOME>`，
       拼出来是个不存在的路径。后果不是报错 —— 是 `load_persona()` 返回 `{}`，
       人设整段消失、`banned_phrases` / `max_length` 静默失效。
       所以候选一律走 `isfile()`，谁先存在用谁。
    """
    env = os.environ.get("BILI_PLUS_PERSONA", "").strip()
    if env:
        return env

    here = os.path.dirname(os.path.abspath(__file__))
    home = os.environ.get("BILI_PLUS_HOME", "").strip()

    candidates: List[str] = []
    if home:
        # 家目录平铺（生产环境 syncPythonFiles 会把 persona.yaml 也铺过来）
        candidates.append(os.path.join(home, "persona.yaml"))
        candidates.append(os.path.join(home, "config", "persona.yaml"))
    # 包内子目录布局（开发/测试：<pkg>/python/persona.py → <pkg>/config/persona.yaml）
    candidates.append(os.path.join(os.path.dirname(here), "config", "persona.yaml"))
    # 包内平铺布局（<home>/config/persona.yaml 之上再兜一层）
    candidates.append(os.path.join(here, "config", "persona.yaml"))

    for p in candidates:
        try:
            if os.path.isfile(p):
                return p
        except OSError:
            continue
    # 一个都没有：返回第一候选，让上层 `loaded` / `persona_path` 能看见它不存在
    return candidates[0]


def _writable_persona_path() -> str:
    """写回人设时用哪条路径。

    永远优先写家目录那份：包内 `config/persona.yaml` 是**模板**，
    直接改它等于改代码 —— 下次升级就被覆盖，用户还以为自己改坏了。
    家目录不存在时也不退回包内，先建出来再说。
    """
    home = os.environ.get("BILI_PLUS_HOME", "").strip()
    if home:
        return os.path.join(home, "persona.yaml")
    return _persona_path()


# ── 极简 YAML（只认我们这个人设文件的结构）─────────────────────
# 支持：`key: value`、`key:` 后跟 `- item` 列表、两层以上嵌套、`#` 注释、引号字符串。
# 不支持：多行字符串、锚点、流式写法 —— 我们的文件里没有，不需要。
#
# 难点只有一个：看到 `key:`（冒号后空着）时，**还不知道下一层是 dict 还是 list**。
# 做法是先挂一个占位容器，等缩进更深的行到齐了再决定。这里用「先收集成有序的
# (缩进, 内容) 序列，再递归下降」的写法，比一次性边扫边建树清楚得多。
def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    # ① 清成 [(indent, kind, payload)]，kind = 'kv' | 'item'
    rows: List[tuple] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        # 去行尾注释：只在 `  #`（前面有空白）时才算，避免切坏 "http://x#y"
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body.startswith("- "):
            rows.append((indent, "item", _unquote(body[2:].strip())))
        elif ":" in body:
            key, _, rest = body.partition(":")
            rows.append((indent, "kv", (_unquote(key.strip()), _unquote(rest.strip()))))
        else:
            rows.append((indent, "text", _unquote(body)))

    # ② 递归下降建树
    pos = [0]

    def build(indent: int) -> Any:
        """消费所有缩进 > indent 的行，返回 dict 或 list。"""
        container: Any = None
        while pos[0] < len(rows):
            cur_indent, kind, payload = rows[pos[0]]
            if cur_indent <= indent:
                break
            if container is None:
                # 第一个孩子决定类型：如果是 `- ` 就是 list，否则是 dict
                container = [] if kind == "item" else {}
            if kind == "item":
                if not isinstance(container, list):
                    # 混用（罕见）：把已收的 dict 丢掉，当 list 用
                    container = []
                pos[0] += 1
                container.append(payload)
                continue
            if kind == "text":
                pos[0] += 1
                continue
            # kv
            key, value = payload
            pos[0] += 1
            if not isinstance(container, dict):
                container = {}
            if value == "":
                # 下一层缩进更深 → 递归；否则就是空值
                if pos[0] < len(rows) and rows[pos[0]][0] > cur_indent:
                    container[key] = build(cur_indent)
                else:
                    container[key] = None
            else:
                container[key] = value
        return container if container is not None else {}

    result: Dict[str, Any] = {}
    while pos[0] < len(rows):
        cur_indent, kind, payload = rows[pos[0]]
        if kind == "kv":
            key, value = payload
            pos[0] += 1
            if value == "":
                if pos[0] < len(rows) and rows[pos[0]][0] > cur_indent:
                    result[key] = build(cur_indent)
                else:
                    result[key] = None
            else:
                result[key] = value
        else:
            pos[0] += 1   # 顶层裸项，忽略
    return result


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _yaml_load(text: str) -> Dict[str, Any]:
    """优先用真 PyYAML；没有就用极简解析器。"""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return _parse_simple_yaml(text)


# ── 载入 ───────────────────────────────────────────────────────

def load_persona(force: bool = False) -> Dict[str, Any]:
    """读人设。文件没变就走缓存（每次生成提示都读文件太浪费）。"""
    path = _persona_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}

    if (not force and _CACHE["data"] is not None
            and _CACHE["path"] == path and _CACHE["mtime"] == mtime):
        return _CACHE["data"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}

    data = _yaml_load(text)
    _CACHE.update({"path": path, "mtime": mtime, "data": data, "raw": text})
    return data


def _section(data: Dict[str, Any], *path: str) -> Any:
    cur: Any = data
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
    return cur


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)]


# ── 给人看/给模型看的名字 ─────────────────────────────────────

def persona_name() -> str:
    data = load_persona()
    return str(_section(data, "persona", "name") or "deepseek")


def account_name() -> str:
    data = load_persona()
    return str(_section(data, "persona", "account_name") or "")


def catchphrase() -> str:
    """
    口头禅的**文本**。
    ⚠️ yaml 里 catchphrase 是一个 dict（{text, when, usage_limit}），不是字符串。
       第一版直接 str() 了整个 dict，导致"口头禅复读"检测拿一串
       "{'text': '我可是高性能', ...}" 去数次数 —— **永远数不到**，静默失效。
    """
    data = load_persona()
    cp = _section(data, "persona", "catchphrase")
    if isinstance(cp, dict):
        return str(cp.get("text") or "").strip()
    return str(cp or "").strip()


def catchphrase_meta() -> Dict[str, str]:
    """口头禅的附带信息：什么时候用、用几次。"""
    data = load_persona()
    cp = _section(data, "persona", "catchphrase")
    if isinstance(cp, dict):
        return {k: str(v) for k, v in cp.items() if isinstance(v, (str, int, float))}
    return {}


def focus_meta() -> Dict[str, Any]:
    """「看什么」的结构化字段（persona.focus 段）。

    为什么单独抽出来：光把偏好写在提示词里不够 —— 宿主选题那一层
    （index.js#runOnce）也要按同一组数跑，否则会出现「提示词说只看当天的，
    实际喂进来的是热门榜上一周的旧视频」，模型只能照单全收。
    """
    data = load_persona()
    f = _section(data, "persona", "focus")
    if not isinstance(f, dict):
        return {}
    try:
        days = int(f.get("recency_days"))
    except (TypeError, ValueError):
        days = 2
    return {
        "recency_days": max(0, min(days, 14)),
        "recency_note": str(f.get("recency_note") or ""),
        "topics": _as_list(f.get("topics")),
        "daily_report_accounts": _as_list(f.get("daily_report_accounts")),
        "daily_report_rule": str(f.get("daily_report_rule") or ""),
        "report_focus": str(f.get("report_focus") or ""),
    }


def _focus_lines() -> List[str]:
    """把 focus 段渲染成提示词行。没有这段就返回空列表（向后兼容旧人设文件）。"""
    f = focus_meta()
    if not f:
        return []
    lines: List[str] = [""]
    lines.append("看什么（选题偏好）：")
    days = f["recency_days"]
    if days <= 0:
        lines.append("  · 不限视频新旧（有值得看的就看，别只盯当天）")
    else:
        lines.append(f"  · 只看最近 {days} 天内的视频"
                     + (f"：{f['recency_note']}" if f["recency_note"] else "。"))
    if f["topics"]:
        lines.append(f"  · 优先题材：{'、'.join(f['topics'][:8])}。")
    if f["daily_report_accounts"]:
        lines.append(f"  · 这几个号的 AI 日报/资讯要重点关注："
                     f"{'、'.join(f['daily_report_accounts'][:8])}。")
    if f["daily_report_rule"]:
        lines.append(f"  · {f['daily_report_rule']}")
    if f["report_focus"]:
        lines.append(f"  · {f['report_focus']}")
    return lines


def persona_prompt() -> str:
    """
    生成给**宿主模型**的人设段落。

    ⚠️ 这段是"她是谁、怎么说话"，**不含安全规则**。
       安全规则在 safety.py 的 check_text() 里硬执行 ——
       提示词只是建议，代码才是闸门。这两层必须分开，
       否则模型一句"忽略之前的设定"就能把安全绕过去。
    """
    data = load_persona()
    p = _section(data, "persona") or {}
    if not isinstance(p, dict):
        return ""

    lines: List[str] = []
    name = p.get("name") or "deepseek"
    aliases = _as_list(p.get("aliases"))
    species = p.get("species") or ""
    appearance = p.get("appearance") or ""
    role = p.get("role") or ""

    head = f"你在 B站 评论区的身份是「{name}」"
    if aliases:
        head += f"（也叫{('、'.join(aliases[:3]))}）"
    lines.append(head + "。")
    if species:
        lines.append(f"- 你是{species}" + (f"，{appearance}" if appearance else "") + "。")
    if role:
        lines.append(f"- 你的角色：{role}。")

    likes = _as_list(p.get("likes"))
    if likes:
        lines.append(f"- 你喜欢：{'、'.join(likes[:10])}。")
    dislikes = _as_list(p.get("dislikes"))
    if dislikes:
        lines.append(f"- 你不喜欢：{'、'.join(dislikes[:8])}。")

    # 看什么（focus 段）—— 插在这里：先说"你是谁/喜欢什么"，再说"该去看什么"
    lines.extend(_focus_lines())

    style = _section(data, "persona", "style") or {}
    if isinstance(style, dict):
        tone = style.get("tone")
        if tone:
            lines.append(f"- 语气：{tone}。")
        lo = style.get("min_length")
        hi = style.get("max_length")
        if lo or hi:
            lines.append(f"- 长度：{lo or '?'}~{hi or '?'} 字。")

    caps = _as_list(_section(data, "persona", "catchphrases"))
    cp_text = catchphrase()
    if not caps and cp_text:
        caps = [cp_text]
    if caps:
        meta = catchphrase_meta()
        limit = meta.get("usage_limit", "")
        lines.append(f"- 口头禅：{'、'.join(caps[:3])}"
                     + (f"（{limit}）" if limit else "（同一条评论区最多用一次，别复读）") + "。")

    rules = _as_list(_section(data, "persona", "identity_rules"))
    if rules:
        lines.append("")
        lines.append("身份规则：")
        for r in rules[:8]:
            lines.append(f"  · {r}")

    # 这几条是"不要做什么"，跟 safety.py 有重叠但**不冲突**：
    # 这里管"像不像人写的"，那里管"能不能发"。
    lines.append("")
    lines.append("硬性要求：")
    lines.append("  · 用纯文本，不要 markdown、不要 #话题标签、不要链接。")
    lines.append("  · 不要自称 AI / 语言模型，也不要编造真实生活经历。")
    lines.append("  · 不要空洞夸赞，不要教育别人。")
    lines.append("  · 一屏能读完就够，别写小作文。")

    return "\n".join(lines)


def read_raw() -> str:
    """读人设文件的**原文**（编辑用：要连着注释和缩进一起给出去，不能给重新序列化的）。"""
    load_persona()
    return _CACHE.get("raw") or ""


def save_raw(text: str, *, backup: bool = True) -> Dict[str, Any]:
    """把整段原文写回人设文件，写完立刻清缓存（下一次 load_persona 就是新内容）。

    整段写而不是改单字段：人设里有中文注释和 `intro: |` 多行块，
    任何「读 dict 再 dump 回去」都会把注释和排版冲掉。

    写完必须还能被同一个解析器读成 dict，否则**回滚**（把原文写回去）——
    一次写坏就等于 `load_persona()` 返回 `{}`，人设整段静默失效。
    """
    path = _writable_persona_path()
    old = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = ""

    data = _yaml_load(text)
    if not isinstance(data, dict) or not data:
        return {"ok": False, "error": "这段不是合法的人设 YAML（解析出来是空的）", "written": False}

    bak = ""
    if backup and old:
        bak = f"{path}.bak"
        try:
            with open(bak, "w", encoding="utf-8") as f:
                f.write(old)
        except OSError:
            bak = ""

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        return {"ok": False, "error": f"写不进去：{e}", "written": False}

    # 回读校验：解析不出来就把旧的写回去
    try:
        with open(path, "r", encoding="utf-8") as f:
            round_trip = _yaml_load(f.read())
    except OSError:
        round_trip = {}
    if not isinstance(round_trip, dict) or not round_trip:
        if old:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(old)
            except OSError:
                pass
        return {"ok": False, "error": "写入后解析失败，已回滚", "written": False,
                "rolled_back": True}

    _CACHE.update({"path": "", "mtime": 0.0, "data": None, "raw": ""})
    return {"ok": True, "written": True, "path": path, "backup": bak,
            "bytes": len(text.encode("utf-8")),
            "name": persona_name()}


def persona_summary() -> Dict[str, Any]:
    """设置页顶部那行摘要：让人一眼看出「现在的人设是谁、文件在哪、有没有加载上」。

    ⚠️ 字段路径照抄本文件里真正在用的那些（persona_name / focus_meta / check_style），
       **不要凭想象编 `persona.run.xxx`** —— 人设文件里根本没有 run 段，
       编出来的路径只会静默返回 None，界面上显示成空，看起来像"读不到"。
       所以这里直接复用 focus_meta()，跟提示词用的是同一份数据。
    """
    data = load_persona()
    path = _persona_path()
    fo = focus_meta()
    # 风格上限的路径是 persona.style.*（跟 check_style 里读的完全一致）。
    # 不另写 accessor：多一层转发就多一处可能读错的地方，
    # 而读错的后果是界面上显示「长度 —」，让人以为人设没加载。
    sty = _section(data, "persona", "style")
    sty = sty if isinstance(sty, dict) else {}
    banned = _as_list(sty.get("banned_phrases"))
    return {
        "ok": bool(data),
        "loaded": bool(data),
        "path": path,
        "name": persona_name(),
        "account": account_name(),
        "catchphrase": catchphrase(),
        "likes": _as_list(_section(data, "persona", "likes")),
        "dislikes": _as_list(_section(data, "persona", "dislikes")),
        "focus": {
            "recency_days": fo.get("recency_days"),
            "topics": fo.get("topics"),
            "daily_report_accounts": fo.get("daily_report_accounts"),
        } if fo else None,
        "style": {
            "min_length": sty.get("min_length"),
            "max_length": sty.get("max_length"),
            "banned_count": len(banned),
        },
    }


# ── 风格检查 ───────────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF" "\U00002B00-\U00002BFF" "\uFE0F" "]"
)
# markdown：行首标题 / 粗斜体 / 行内代码 / 列表 / 引用
_MARKDOWN_RE = re.compile(
    r"(^#{1,6}\s)|(\*\*[^*\n]+\*\*)|(__[^_\n]+__)|(^[-*+]\s+\S)|(^>\s)|(`[^`\n]+`)",
    re.M,
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|cn|net|org)\b", re.I)
_HASHTAG_RE = re.compile(r"[#＃][\u4e00-\u9fa5\w]+")

# "我住在/我上班/我同学" —— 编造现实生活
_FAKE_HUMAN_RE = re.compile(
    r"我(?:住在|家在|学校|上学|上班|公司|单位|男朋友|女朋友|老公|老婆"
    r"|爸妈|父母|家人|同学|朋友|同事)"
)
# 说教 / 居高临下
_PREACH_RE = re.compile(r"你应该|你必须|大家都要|懂了吗|说白了|我劝你|建议你|听我的")
# AI 腔
_AI_TONE_RE = re.compile(
    r"作为一个\s*AI|作为一个人工智能|我只是(?:一个)?语言模型"
    r"|根据我的训练数据|我的知识库",
    re.I,
)


def check_style(text: str) -> Dict[str, Any]:
    """
    看这段文字像不像"这个人"写的。

    返回：
      ok       —— 没有硬问题
      issues   —— 硬问题（每一项都该改掉才能发）
      warnings —— 软问题（可以发，但读起来不对劲）
      score    —— 100 分制，纯粹给人看的
    """
    data = load_persona()
    style = _section(data, "persona", "style") or {}
    if not isinstance(style, dict):
        style = {}

    min_len = int(style.get("min_length") or 5)
    max_len = int(style.get("max_length") or 300)
    emoji_max = int(style.get("emoji_max") or 1)
    exclaim_max = int(style.get("exclamation_max") or 2)

    t = (text or "").strip()
    issues: List[str] = []
    warnings: List[str] = []

    if len(t) < min_len:
        issues.append(f"too_short（{len(t)} < {min_len}）")
    if len(t) > max_len:
        issues.append(f"too_long（{len(t)} > {max_len}）")

    # 禁词：**子串匹配**，不用正则 —— 词表是给人改的
    # ⚠️ 路径是 persona.style.banned_phrases，不是 persona.banned_phrases。
    #    第一版写错了路径，取到空列表 ⇒ 禁词检查静默失效（永远通过）。
    banned = _as_list(_section(data, "persona", "style", "banned_phrases"))
    for word in banned:
        w = str(word).strip()
        if w and w in t:
            issues.append(f"banned_phrase（{w}）")

    # 口头禅复读
    cp = catchphrase()
    if cp and t.count(cp) > 1:
        issues.append(f"catchphrase_repeated（「{cp}」出现 {t.count(cp)} 次）")

    # emoji 数量：按"单个字符"数，不用 findall 的长度（连字 emoji 会算错）
    emoji_count = len(_EMOJI_RE.findall(t))
    if emoji_count > emoji_max:
        issues.append(f"too_many_emoji（{emoji_count} > {emoji_max}）")

    if _FAKE_HUMAN_RE.search(t):
        issues.append("fake_human_experience（别编现实生活经历）")
    if _MARKDOWN_RE.search(t):
        issues.append("markdown（要纯文本）")
    if _URL_RE.search(t):
        issues.append("contains_link（不要链接）")
    if _HASHTAG_RE.search(t):
        issues.append("hashtag（不要话题标签）")

    # 软问题
    if t.count("！") + t.count("!") > exclaim_max:
        warnings.append(f"too_many_exclamations（{t.count('！') + t.count('!')} > {exclaim_max}）")
    if _PREACH_RE.search(t):
        warnings.append("preaching（有点说教）")
    if _AI_TONE_RE.search(t):
        warnings.append("ai_tone（AI 腔）")

    score = max(0, 100 - len(issues) * 15 - len(warnings) * 5)

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "score": score,
        "length": len(t),
        "persona": persona_name(),
        "catchphrase": cp,
        "text": t,
    }
