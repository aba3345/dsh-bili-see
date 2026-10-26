#!/usr/bin/env python3
"""mcp_server.py —— dsh-bili-plus 的 MCP 服务端（stdio）。

架构说明
========
本插件是「JS 宿主层 + Python MCP 层」两段式：

    DSH 宿主 ──ctx.tools.register──> index.js（工具定义 / 设置页 / 定时窗口）
                                        │
                                        └── stdio JSON-RPC ──> 本文件（真正的 B站 访问）

为什么不让 index.js 直接发 HTTP？
  1. wbi 签名 / 雪碧图切片 / Pillow 这些都更适合 Python；
  2. 这一层**可以单独跑**（Claude Code、Cursor、Cline 都能接同一个 MCP），
     不会因为宿主换了一代就整个作废；
  3. 写操作能在这里做**最后一道**闸（下面的 `_write()` 装饰器），
     宿主层就算被提示词注入了，也过不来。

工具白名单（照抄原插件 2026-10-17 审查里的 H3）
==============================================
宿主通过环境变量 `BILI_PLUS_TOOLS`（逗号分隔）下发名单：
  · 不给  ⇒ 全放（手工跑 / 老配置，行为不变）
  · 给了  ⇒ 名单外的工具**连 list_tools 都不出现**，模型那边等于根本不存在。
    这才是真闸门 —— 光靠提示词说"别用"不算。
写类工具（draft_create / draft_update / comment_post / comment_delete）额外受
`BILI_PLUS_WRITE=1` 控制：没显式开写权限，它们**不会被注册**。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import api  # noqa: E402
import persona  # noqa: E402
import safety  # noqa: E402
import shots  # noqa: E402
import wbi  # noqa: E402
from db import DB  # noqa: E402

# ─────────────────────────── 运行环境 ───────────────────────────

HOME = Path(os.environ.get("BILI_PLUS_HOME") or (_HERE.parent / "runtime"))
HOME.mkdir(parents=True, exist_ok=True)

# 自己的凭证文件：登录后写这里
CRED_FILE = Path(
    os.environ.get("BILI_PLUS_CREDENTIAL")
    or (HOME / "bili_credential.json")
)


def _borrow_candidates() -> list[Path]:
    """别人（已装的 dsh-bili-scroll）那份凭证的候选位置。

    为什么要借读：两个插件都是同一个 B站 账号，没必要逼用户重新扫一次码。
    **只读不写** —— 我们把东西写进自己的 CRED_FILE，绝不改别人的文件；
    别人哪天换了账号，我们的回退读取自然就跟着变，不存在两边状态不一致。
    """
    out: list[Path] = []
    dsh_home = os.environ.get("DSH_HOME") or ""
    if not dsh_home:
        # Windows 默认 %USERPROFILE%\.dsh；别的地方靠 DSH_HOME 覆盖
        dsh_home = str(Path.home() / ".dsh")
    homes = [
        Path(dsh_home) / "bili-duty",              # dsh-bili-scroll 的插件家目录
        Path(dsh_home) / "bili-scroll",
        Path(dsh_home) / "dsh-bili-scroll",
        # dsh-bili-see 是从 dsh-bili-plus / dsh-bili-scroll 合并来的：
        # 同一个 B站 账号没必要让用户重扫一次码，先借读它们那份（只读，不写）。
        Path(dsh_home) / "bili-plus",
    ]
    for h in homes:
        p = h / "bili_credential.json"
        if p not in out:
            out.append(p)
    return out


def active_cred_file() -> Path | None:
    """真正要读的那个文件：自己的优先，没有就借别人的（只读）。没有就 None。"""
    if CRED_FILE.exists():
        return CRED_FILE
    for p in _borrow_candidates():
        try:
            if p.exists() and p.stat().st_size > 10:
                return p
        except OSError:
            continue
    return None


def credential_status() -> dict:
    """给 status / credential_check 用：说清楚在用哪份、是不是借的。"""
    own = CRED_FILE.exists()
    active = active_cred_file()
    return {
        "credential_file": str(CRED_FILE),
        "has_own": own,
        "active_file": str(active) if active else None,
        "borrowed": bool(active and active != CRED_FILE),
    }


DB_PATH = str(HOME / "bili_plus.db")
SHOT_DIR = HOME / "shots"

WRITE_ENABLED = os.environ.get("BILI_PLUS_WRITE", "").strip() in ("1", "true", "yes", "on")
_ALLOWED = {t.strip() for t in os.environ.get("BILI_PLUS_TOOLS", "").split(",") if t.strip()}

MAX_POSTS_PER_DAY = int(os.environ.get("BILI_PLUS_MAX_POSTS_PER_DAY", "5"))
MIN_POST_INTERVAL_MIN = int(os.environ.get("BILI_PLUS_MIN_INTERVAL_MIN", "30"))

# 测试通道（评论测试）。默认关闭、默认空 —— 开源代码里不带任何视频号。
# 打开后，所有评论只能发到 BILI_PLUS_TEST_BVID 这一条视频，别的一律拒。
# 🔴 这个闸必须在**这里**做，不能只在 JS 侧做：JS 侧拿不到 SQLite 里的草稿，
#    它只能读到 state.json（那里面根本没有 drafts 字段），于是 bvid 永远是空的，
#    等于把所有发送都误杀。真正的 bvid 只有这里 dr["bvid"] 拿得到。
TEST_MODE = os.environ.get("BILI_PLUS_TEST_MODE", "").strip() in ("1", "true", "yes", "on")
TEST_BVID = os.environ.get("BILI_PLUS_TEST_BVID", "").strip()


def _normalize_bvid(raw: str) -> str:
    """把可能粘进来的完整链接/带空格的串，收敛成纯 BV 号。"""
    import re as _re
    m = _re.search(r"BV[0-9A-Za-z]{10}", str(raw or ""))
    return m.group(0) if m else ""


def _test_mode_error(bvid: str) -> Optional[str]:
    """测试模式闸门：返回错误文案 = 拒绝；返回 None = 放行。"""
    if not TEST_MODE:
        return None
    want = _normalize_bvid(TEST_BVID)
    if not want:
        return "测试模式开着，但没配置测试视频（BILI_PLUS_TEST_BVID 为空），任何评论都不会发。"
    got = _normalize_bvid(bvid)
    if got != want:
        return f"测试模式只允许往 {want} 发，这条是 {got or '(空)'}。想发别的请先关掉测试模式。"
    return None

# 审批功能已下线（2026-10-22）：不再有 APPROVAL_BYPASS 这个开关。
# 发送路径 = draft_create（过安全审）→ comment_post（过 4 道闸门）→ 真发。
# 老版本（带审批 + 免审批旁路）备份见 _backup_approval_20261022/。

mcp = FastMCP("bili-plus")

_db: DB | None = None


def db() -> DB:
    global _db
    if _db is None:
        _db = DB(DB_PATH)
    return _db


def client(*, need_login: bool = False) -> wbi.BiliClient:
    """每次现读凭证（用户在设置页重扫了码，下一次调用就生效，不用重启宿主）。

    读的是 active_cred_file()：自己的优先，没登录过就先借同机的 dsh-bili-scroll
    那份（只读）。这样装完插件不用重新扫码也能直接用。
    """
    f = active_cred_file()
    cred = wbi.credential_from_env_or_file(str(f)) if f else {}
    c = wbi.BiliClient(cred)
    if need_login and not c.logged_in:
        raise wbi.BiliError(
            "还没登录 B站（或凭证已失效）。先在「B站增强」设置页点「扫码登录」，"
            f"凭证会存到 {CRED_FILE}",
            code="not_logged_in",
        )
    return c


# ─────────────────────────── 门 / 包装器 ───────────────────────────

def tool():
    """白名单替身：名单外的函数原样返回、不注册。"""
    def deco(fn):
        if _ALLOWED and fn.__name__ not in _ALLOWED:
            return fn
        return mcp.tool()(fn)
    return deco


def write_tool():
    """写类工具：**没开 BILI_PLUS_WRITE 就完全不注册**（不是注册了再拒绝）。"""
    def deco(fn):
        if not WRITE_ENABLED:
            print(f"[bili-plus] 写工具 {fn.__name__} 未注册（BILI_PLUS_WRITE 没开）", file=sys.stderr)
            return fn
        return tool()(fn)
    return deco


def _ok(obj: Any) -> str:
    return json.dumps({"ok": True, **(obj if isinstance(obj, dict) else {"data": obj})},
                      ensure_ascii=False)


def _err(e: Exception, **extra: Any) -> str:
    if isinstance(e, wbi.BiliError):
        body = e.as_dict()
    else:
        body = {"error": f"{type(e).__name__}: {e}"}
    body.update(extra)
    body["ok"] = False
    return json.dumps(body, ensure_ascii=False)


def _guard(fn, *a, **kw) -> str:
    """统一异常出口。**风控命中的还顺手把插件停掉** —— 这是原插件踩出来的规矩：
    收到 -412 还继续发，号就没了。"""
    try:
        return _ok(fn(*a, **kw))
    except wbi.BiliError as e:
        if e.risk:
            try:
                db().set_paused(True, f"风控 {e.code}: {e.message}")
                db().record_action("risk_control", str(e.code), {"message": e.message})
            except Exception:
                pass
            return _err(e, paused=True,
                        hint="命中 B站 风控，插件已自动暂停。过几小时再手动恢复，"
                             "别立刻重试（连续失败会升级成更重的限制）")
        return _err(e)
    except Exception as e:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        return _err(e)


# ─────────────────────────── 凭证 / 登录 ───────────────────────────

@tool()
def credential_check() -> str:
    """看 B站 那边登录的是谁：凭证是否有效、昵称、mid、cookie 齐不齐。

    不传任何参数。第一次用要先扫码登录（login_qr_start）。"""
    c = client()
    cs = credential_status()
    if not c.logged_in:
        return _ok({"logged_in": False, **cs,
                    "hint": "还没登录：调 login_qr_start 拿二维码"})
    info = (c.nav().get("data") or {})
    return _ok({
        "logged_in": True,
        "uname": info.get("uname"),
        "mid": info.get("mid"),
        "vip": (info.get("vipStatus") == 1),
        "level": (info.get("level_info") or {}).get("current_level"),
        **cs,
    })


@tool()
def login_qr_start() -> str:
    """开始扫码登录：返回二维码图片路径（PNG）和轮询用的 key。

    拿着 key 每隔 2 秒调 login_qr_poll 一次；二维码大约 180 秒过期。"""
    import qrcode  # 延迟导入：没装也不影响其它工具

    # 这个接口不需要 cookie 也不需要 wbi，直接用底层请求
    import urllib.request
    req = urllib.request.Request(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers={"User-Agent": wbi.UA, "Referer": "https://www.bilibili.com/"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise wbi.BiliError(f"取二维码失败: {payload.get('message')}", code=payload.get("code"))
    d = payload["data"]
    qr_url = d["url"]
    key = d["qrcode_key"]

    img = qrcode.make(qr_url)
    out = HOME / "qrcode_login.png"
    img.save(str(out))
    return _ok({
        "key": key,
        "qrcode_path": str(out),
        "qrcode_url": qr_url,
        "expires_in": 180,
        "next": "拿 key 轮询 login_qr_poll；手机 B站「我的 → 扫一扫」",
    })


@tool()
def login_qr_poll(key: str) -> str:
    """查扫码结果。

    - `waiting`  还没扫
    - `scanned`  扫了，等手机确认
    - `ok`       成功，凭证已落盘（之后所有工具都能用了）
    - `expired`  过期，重新 login_qr_start
    """
    import urllib.request

    url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
           f"?qrcode_key={key}&source=main-fe-header")
    req = urllib.request.Request(url, headers={
        "User-Agent": wbi.UA, "Referer": "https://www.bilibili.com/",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))
        set_cookies = r.headers.get_all("Set-Cookie") or []
    if payload.get("code") != 0:
        raise wbi.BiliError(f"轮询失败: {payload.get('message')}", code=payload.get("code"))

    d = payload["data"]
    code = d.get("code")
    if code == 0:
        # 从 Set-Cookie 里抠凭证（比 follow redirect 更直接）
        jar: dict[str, str] = {}
        for raw in set_cookies:
            head = raw.split(";", 1)[0]
            if "=" in head:
                k, v = head.split("=", 1)
                jar[k.strip()] = v.strip()
        cred = {
            "sessdata": jar.get("SESSDATA", ""),
            "bili_jct": jar.get("bili_jct", ""),
            "buvid3": jar.get("buvid3", ""),
            "dedeuserid": jar.get("DedeUserID", ""),
        }
        CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CRED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CRED_FILE)  # 原子写：写一半被打断不会留个坏文件
        try:
            os.chmod(CRED_FILE, 0o600)
        except OSError:
            pass
        c = wbi.BiliClient(cred)
        return _ok({"status": "ok", "uname": c.uname(), "mid": c.uid(),
                    "credential_file": str(CRED_FILE)})

    return _ok({
        "status": {86101: "waiting", 86090: "scanned", 86038: "expired"}.get(code, f"unknown_{code}"),
        "raw_code": code,
        "message": d.get("message"),
    })


# ─────────────────────────── 读：视频 / 搜索 ───────────────────────────

@tool()
def search(keyword: str, num: int = 10, order: str = "totalrank") -> str:
    """搜 B站 视频。

    - `order`: totalrank=综合（默认）/ click=播放最多 / pubdate=最新 / dm=弹幕最多
    - `num`: 1~50
    返回标题 / BV号 / UP主 / 播放量 / 时长 / 发布时间。"""
    c = client()
    n = max(1, min(int(num or 10), 50))
    r = api.search(c, keyword, num=n, order=order)
    return _ok(r)


@tool()
def video_info(bvid: str) -> str:
    """取一个视频的完整信息：标题 / UP主 / 时长 / 简介 / 分P / 播放点赞投币 / 标签。

    想快速判断"这视频讲什么"，先看 ai_conclusion（有 AI 总结时），
    没有就读 subtitle 原文 —— **别看标题猜**。"""
    c = client()
    v = api.get_video(c, bvid)
    try:
        v["tags"] = api.get_tags(c, bvid)
    except wbi.BiliError:
        v["tags"] = []
    db().upsert_video({**v, "owner_mid": v.get("author_mid")})
    return _ok(v)


@tool()
def subtitle(bvid: str, max_chars: int = 8000) -> str:
    """取视频字幕**原文**（优先人工字幕，没有就用 AI 字幕）。

    **判断内容就靠这个接口** —— 摘要会丢细节，标题会误导。
    返回 `segments`（带时间戳的句子）和 `text`（拼好的全文）。
    没有字幕时 message 会说明原因，并提示改用 keyframes 看画面。"""
    c = client()
    r = api.get_subtitle(c, bvid)
    text = r.get("text") or ""
    lim = max(200, min(int(max_chars or 8000), 60000))
    if len(text) > lim:
        r["truncated"] = True
        r["text"] = text[:lim]
        r["note"] = f"全文 {len(text)} 字，已截到 {lim} 字；需要更多就调大 max_chars"
    return _ok(r)


@tool()
def ai_conclusion(bvid: str) -> str:
    """取 B站 官方 AI 视频总结（摘要 + 带时间戳的大纲）。

    ⚠️ 这是**转述**不是原文，而且不是每个视频都有（`status=0` 表示还没生成）。
    要下判断仍然应该读 subtitle。"""
    c = client(need_login=True)
    return _ok(api.get_ai_conclusion(c, bvid))


@tool()
def comments(bvid: str, num: int = 20, with_replies: bool = False,
             reply_num: int = 3) -> str:
    """取视频的热门评论（按热度）。

    - `with_replies`: 是否顺带抓子评论（每条最多 `reply_num` 条）
    - 返回里 `is_end` / `next` 供翻页
    刷一轮时用它看观众在聊什么 —— 也是判断"这条视频值不值得回"的依据。"""
    c = client()
    r = api.get_comments(c, bvid, num=max(1, min(int(num or 20), 50)),
                         with_replies=bool(with_replies))
    keep = max(0, min(int(reply_num or 3), 20))
    for row in r.get("comments") or []:
        subs = row.get("sub_replies")
        if isinstance(subs, list) and len(subs) > keep:
            row["sub_replies"] = subs[:keep]
            row["sub_replies_truncated"] = len(subs)
        row["is_new"] = db().remember_comment(bvid, row)
    return _ok(r)


@tool()
def popular(num: int = 20) -> str:
    """B站 综合热门榜（按当前热度）。`num` 1~50。"""
    c = client()
    return _ok(api.popular(c, ps=max(1, min(int(num or 20), 50))))


@tool()
def rank(zone: str = "全站", num: int = 20, day: int = 3) -> str:
    """排行榜。

    - `zone`: 全站 / 动画 / 国创 / 音乐 / 舞蹈 / 游戏 / 科技 / 生活 / 鬼畜 / 时尚 /
      影视 / 娱乐 / 知识 / 美食 / 运动 / 汽车 / 旅行 / 记录 / 电影 / 电视剧 / 综艺
    - `day`: 3=三日榜（默认）/ 7=一周 / 30=一月
    - `num`: 1~50"""
    c = client()
    r = api.rank(c, category=zone or "全站",
                 day=day if day in (3, 7, 30) else 3)
    vids = (r.get("videos") or [])[:max(1, min(int(num or 20), 50))]
    return _ok({**r, "videos": vids, "count": len(vids)})


@tool()
def user_info(mid: int) -> str:
    """看一个 UP 主：昵称 / 粉丝数 / 关注数 / 签名 / 等级 / 认证。"""
    c = client()
    return _ok(api.user_info(c, int(mid)))


@tool()
def user_videos(mid: int, num: int = 20, order: str = "pubdate") -> str:
    """看一个 UP 主的投稿。`order`: pubdate=最新 / click=播放最多 / stow=收藏最多。"""
    c = client()
    r = api.user_videos(c, int(mid), ps=max(1, min(int(num or 20), 50)),
                        order=order or "pubdate")
    vids = (r.get("videos") or [])[:max(1, min(int(num or 20), 50))]
    return _ok({**r, "videos": vids, "count": len(vids)})


# ─────────────────────────── 关键帧（雪碧图） ───────────────────────────

@tool()
def keyframes(bvid: str, times: str = "", count: int = 4,
              contact_sheet: bool = True) -> str:
    """从视频里抽关键帧图（**纯 HTTP，不需要浏览器**）。

    - `times`: 逗号分隔的秒数（如 `"60,300,900"`）；给了就按这些时间点取，忽略 `count`
    - `count`: 没给 `times` 时，按时长均匀取几张（1~12）
    - `contact_sheet`: 是否额外拼一张带时间戳的接触表（一次看全片节奏，省 token）

    B站 对每个视频都预生成了 10×10 的雪碧图（实测 44 分钟的视频有 463 帧、间隔约 6 秒），
    本工具直接从里面裁 —— 比"打开浏览器播放再截图"快几十倍，也不怕播放器改版。
    返回每张图的路径、时间戳、尺寸。"""
    c = client()
    si = shots.fetch_shot_index(c, bvid)
    if times.strip():
        want = []
        for part in times.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                want.append(float(part))
            except ValueError:
                continue
    else:
        want = shots.pick_times(si, count)
    if not want:
        raise wbi.BiliError("没解析出任何时间点")

    out_dir = str(SHOT_DIR / bvid)
    rows = shots.save_frames(c, si, want, out_dir, prefix=bvid)

    result: dict[str, Any] = {
        "bvid": bvid,
        "index": {k: v for k, v in si.as_dict().items() if k != "timeline"},
        "frames": rows,
        "out_dir": out_dir,
    }
    if contact_sheet:
        try:
            result["contact_sheet"] = shots.contact_sheet(
                c, si, want, str(SHOT_DIR / f"{bvid}_contact.png"), cols=3
            )
        except wbi.BiliError as e:
            result["contact_sheet_error"] = e.message
    return _ok(result)


@tool()
def video_timeline(bvid: str) -> str:
    """拿视频的关键帧时间轴（纯元数据，很快）。

    判断"这个视频多长、B站抽了多少帧、间隔多少秒"时用；
    想真看图调 keyframes。"""
    c = client()
    return _ok(shots.fetch_shot_index(c, bvid).as_dict())


# ─────────────────────────── 安全审 ───────────────────────────

@tool()
def safety_check(text: str = "", bvid: str = "") -> str:
    """**发之前先审一遍**。

    - 给 `text`: 审这段评论能不能发（返回 allow/warn/rewrite/block/no_reply + 原因）
    - 给 `bvid`: 审这个视频本身值不值得碰（标题/简介/标签里有没有政治经济等话题）

    `no_reply` 和 `block` **不能靠人工审批放行** —— 换话题或放弃。"""
    out: dict[str, Any] = {}
    if text.strip():
        out["text"] = safety.check_text(text).as_dict()
        out["quality"] = safety.quality_gates(text)
        out["sanitized"] = safety.sanitize(text)
        # 风格审跟着一起给 —— 安全管"能不能发"，风格管"像不像这个人"。
        # 两个都过才叫真的能发，但**只有安全能硬拦**（style 不阻止发帖）。
        out["style"] = persona.check_style(text)
    if bvid.strip():
        c = client()
        v = api.get_video(c, bvid)
        try:
            v["tags"] = api.get_tags(c, bvid)
        except wbi.BiliError:
            v["tags"] = []
        out["video"] = safety.check_video(v).as_dict()
        out["video_title"] = v.get("title")
    if not out:
        return _err(ValueError("text 和 bvid 至少给一个"))
    return _ok(out)


# ─────────────────────────── 写：草稿 / 审批 / 发 ───────────────────────────

@write_tool()
def draft_create(bvid: str, text: str, rationale: str = "",
                 kind: str = "comment", root_rpid: int = 0,
                 parent_rpid: int = 0, target_author: str = "",
                 reply_to_rpid: int = 0) -> str:
    """**建一条待发评论**。

    流程（2026-10-22 起审批已下线）：draft_create → 过安全审 → comment_post 直接发。
    没有 token、不用人点批准；但 comment_post 会再过一遍 safety + 日限 + 去重 + 视频级安全审。

    - `kind`: `comment`（发在视频下）或 `reply`（回复某条评论）
    - 回复时给 `root_rpid`；**楼中楼回复必须再给 `parent_rpid`**
    - 传了 `reply_to_rpid` 的话，root/parent 会自动从服务端解析出来，
      优先级高于手填的 root_rpid/parent_rpid —— **推荐用它，别自己猜**。
    会在写入前跑一遍 safety，硬拦截会直接拒绝建稿。"""
    # 测试通道闸门：建稿时就拦，别等发了才发现发错地方。
    _tm = _test_mode_error(bvid)
    if _tm:
        return json.dumps({
            "ok": False, "error": _tm, "test_mode_blocked": True,
            "draft_not_created": True,
        }, ensure_ascii=False)

    t = safety.sanitize(text or "")
    v = safety.check_text(t)
    if not v.can_approve:
        return json.dumps({
            "ok": False, "action": v.action, "hits": v.hits,
            "error": "内容被硬拦截，不能建稿（block/no_reply 不可审批绕过）",
        }, ensure_ascii=False)
    if v.action == safety.REWRITE:
        return json.dumps({
            "ok": False, "action": v.action, "fixes": v.fixes,
            "error": "内容需要先改一改再建稿",
        }, ensure_ascii=False)

    # 🔴 楼中楼修复（2026-10-22）：只给 root、不给 parent 时，B站 会把回复
    # 挂到**楼层根**下面 —— 如果那层根恰好是自己发的，就成了"自己回自己"，
    # 被回复的人收不到提醒（draft 22/23/24/27 全部踩中）。
    # 所以：只要给了 reply_to_rpid，就以服务端解析出的 (root, parent) 为准。
    resolved: dict[str, Any] | None = None
    if reply_to_rpid:
        try:
            resolved = api.resolve_reply_target(client(), bvid, int(reply_to_rpid))
        except wbi.BiliError as e:
            return json.dumps({
                "ok": False,
                "error": f"解析回复目标失败，已拒绝建稿（不猜、不发错地方）：{e.message}",
                "draft_not_created": True,
            }, ensure_ascii=False)
        kind = "reply"
        root_rpid = resolved["root"]
        parent_rpid = resolved["parent"]
        if not target_author and resolved.get("user"):
            target_author = resolved["user"]
    elif kind == "reply" and root_rpid and not parent_rpid:
        # 老调用方只给 root 的情形：仍然要问一次服务端，把 parent 补齐。
        # 补不上就按"回复楼层根"处理（parent=root 本来就是合法用法），
        # 但要把这件事写进回执，让人看得见。
        try:
            _rs = api.resolve_reply_target(client(), bvid, int(root_rpid))
            if _rs["is_sub"]:
                root_rpid, parent_rpid = _rs["root"], _rs["parent"]
        except wbi.BiliError:
            pass

    did = db().create_draft(
        bvid, t, rationale=rationale, kind=kind or "comment",
        root_rpid=int(root_rpid) if root_rpid else None,
        parent_rpid=int(parent_rpid) if parent_rpid else None,
        target_author=target_author or "",
    )
    # 审计要连**建稿**一起记 —— 只记 post 的话，事后追查
    # "这条评论哪来的" 会发现草稿凭空出现，看不到是模型在哪个视频下想发的。
    db().record_action("draft", bvid, {
        "draft_id": did, "kind": kind or "comment", "text": t,
        "root_rpid": int(root_rpid) if root_rpid else None,
        "parent_rpid": int(parent_rpid) if parent_rpid else None,
    }, {"ok": True, "action": v.action, "resolved": resolved})
    # 风格问题**不拦**（安全才拦），但要在人批准的时候看得见 ——
    # 否则一条"作为一个AI语言模型…"发出去，事后才知道人设崩了。
    style = persona.check_style(t)
    return _ok({
        "draft_id": did,
        "status": "pending",
        "text": t,
        "reply_target": resolved,
        "warnings": v.warnings,
        "style": {
            "ok": style["ok"],
            "issues": style["issues"],
            "warnings": style["warnings"],
            "score": style["score"],
        },
        "next": f"确认没问题就直接 comment_post(draft_id={did})；先预演用 dry_run=true",
    })


@tool()
def draft_update(draft_id: int, text: str, rationale: str = "") -> str:
    """**改一条还没发出去的草稿正文**（人在设置页里手改，或让模型改）。

    只是改本地那张纸条，不会碰 B站 上任何东西。

    ⚠️ 两个硬约束：
       ① 只有 pending / approved 能改。posted / failed / rejected 会直接拒绝 ——
          posted 改了会让人以为线上那条也变了，其实不会。
       ② 改完**审批 token 作废**、状态退回 pending：token 是为旧正文签的，
          不重新批准就能发出去的话，审批就是摆设。

    - `draft_id`: 草稿 id
    - `text`: 新的正文（会被 sanitize 一遍，但**不跑硬拦截** —— 人自己写的，发的时候 comment_post 还会再拦一次）
    - `rationale`: 新的理由（留空 = 保留原来的）
    """
    d = db()
    did = int(draft_id)
    row = d.get_draft(did)
    if not row:
        return json.dumps({"ok": False, "error": f"草稿 {did} 不存在"}, ensure_ascii=False)
    if row["status"] not in ("pending", "approved"):
        return json.dumps({
            "ok": False,
            "error": f"这条草稿是 {row['status']}，不能改。只有待批(pending)/已批准(approved)能改",
            "status": row["status"],
        }, ensure_ascii=False)
    t = safety.sanitize(text or "")
    if not t.strip():
        return json.dumps({"ok": False, "error": "正文不能为空"}, ensure_ascii=False)
    if t == row["text"] and not rationale:
        return _ok({"draft_id": did, "changed": False, "text": t,
                    "note": "跟原来一样，没动"})
    okupd = d.update_draft_text(did, t, rationale=(rationale or None))
    if not okupd:
        return json.dumps({
            "ok": False,
            "error": "更新失败（状态可能刚被改过，刷新看看）",
            "status": row["status"],
        }, ensure_ascii=False)
    d.record_action("draft_update", row["bvid"] or "", {
        "draft_id": did, "old_text": row["text"], "new_text": t,
    }, {"ok": True})
    # 风格问题照旧只提示、不拦 —— 人自己写的东西，决定权在人。
    style = persona.check_style(t)
    return _ok({
        "draft_id": did,
        "changed": True,
        "text": t,
        "status": "pending",
        "token_voided": True,
        "note": "已改。正文变了 ⇒ 之前的批准 token 作废，要重新点「批准」",
        "style": {"ok": style["ok"], "issues": style["issues"],
                  "warnings": style["warnings"], "score": style["score"]},
    })


@tool()
def draft_list(status: str = "pending", limit: int = 20) -> str:
    """列草稿。`status` 空字符串 = 全部；否则 pending/approved/posted/failed/rejected。"""
    rows = db().list_drafts(status or None, limit=int(limit or 20))
    for r in rows:
        r.pop("approval_hash", None)  # 哈希也没必要给模型看
    return _ok({"count": len(rows), "drafts": rows})


@tool()
def draft_delete(draft_id: int, reason: str = "") -> str:
    """**删掉一条草稿**（不是删 B站 上已发的评论）。

    只作用于本地草稿表：把状态改成 `rejected`，顺带记一条审计。
    已经发出去的（status=posted）**删不了** —— 那条得用 comment_delete 去 B站 删。

    ⚠️ 为什么不是真删行：草稿表是审计链的一部分，直接 DELETE 会让
       "这条评论哪来的" 事后无从追查。软删（rejected）既满足"从待批列表消失"，
       又保留痕迹。真要物理清理，家目录里那个 sqlite 自己删。

    - `draft_id`: 要删的草稿 id
    - `reason`: 删的原因（写进审计，可空）
    """
    d = db()
    did = int(draft_id)
    row = d.get_draft(did)
    if not row:
        return json.dumps({"ok": False, "error": f"草稿 {did} 不存在"}, ensure_ascii=False)
    if row["status"] == "posted":
        return json.dumps({
            "ok": False,
            "error": "这条已经发出去了，草稿表里删不掉。要撤就去 B站 删评论（comment_delete）",
            "status": row["status"],
        }, ensure_ascii=False)
    if row["status"] == "rejected":
        return _ok({"draft_id": did, "status": "rejected", "note": "这条之前就删过了"})
    okdel = d.reject_draft(did, reason or "手动删除")
    if not okdel:
        return json.dumps({
            "ok": False,
            "error": "删除失败（状态可能已经被改过，刷新看看）",
            "status": row["status"],
        }, ensure_ascii=False)
    d.record_action("draft_delete", row["bvid"] or "", {
        "draft_id": did, "text": row["text"],
    }, {"ok": True, "reason": (reason or "手动删除")[:120]})
    return _ok({
        "draft_id": did,
        "status": "rejected",
        "deleted_text": row["text"],
        "note": "已从待批列表移除（软删，留审计痕迹）",
    })


# 审批功能已下线（2026-10-22 主人要求重构）。
# 原来的 `draft_approve` 工具（签 token / 有效期 / 免审批旁路）整段删除，
# 老版本备份在 python/mcp_server.py.bak-20261022 与 _backup_approval_20261022/。
# 现在写路径只有一个：draft_create（过安全审）→ comment_post（过 4 道闸门）→ 真发。


@write_tool()
def comment_post(draft_id: int, token: str = "", dry_run: bool = False) -> str:
    """**真发出去**。

    审批功能已下线（2026-10-22）。现在**不需要 token**：
    草稿建好之后，只要过完下面这几道闸门就直接发。

    发之前会依次检查（**顺序不能改**）：
      1. 插件没被暂停（风控/手动）
      2. 草稿没发过
      3. 文案重跑一遍 safety（可能被人改过）
      4. 日限 / 冷却 / 同视频同楼去重 / 同 UP 主 7 天去重
      5. 视频本身要不要碰（safety.check_video）

    `token` 参数留着只为兼容旧调用方，**已经不再校验**。
    `dry_run=true` 只走检查不真发 —— 建议先用它确认一遍。"""
    d = db()
    did = int(draft_id)

    if d.paused:
        return json.dumps({"ok": False, "error": "插件处于暂停状态",
                           "pause_reason": d.pause_reason}, ensure_ascii=False)

    dr = d.get_draft(did)
    if not dr:
        return json.dumps({"ok": False, "error": f"草稿 {did} 不存在"}, ensure_ascii=False)
    if dr["status"] == "posted":
        return json.dumps({"ok": False, "error": "这条已经发过了",
                           "result": dr.get("result")}, ensure_ascii=False)

    # 审批闸门已下线（2026-10-22）：不再校验 token，状态只看「没发过且没被删」。
    if dr.get("status") not in ("pending", "approved"):
        return json.dumps({"ok": False, "error": f"草稿状态 {dr.get('status')}，不能发"},
                          ensure_ascii=False)

    # 测试通道闸门：真 bvid 只有这里读得到（SQLite），JS 侧读 state.json 是空的。
    _tm = _test_mode_error(dr.get("bvid", ""))
    if _tm:
        return json.dumps({"ok": False, "error": _tm, "test_mode_blocked": True},
                          ensure_ascii=False)

    text = dr["text"]
    v = safety.check_text(text)
    if not v.ok:
        d.mark_failed(did, f"safety:{v.action}")
        return json.dumps({"ok": False, "error": f"发前复核没过: {v.action}",
                           "hits": v.hits, "fixes": v.fixes}, ensure_ascii=False)

    used = d.count_posts_since(24)
    if used >= MAX_POSTS_PER_DAY:
        return json.dumps({"ok": False, "error": f"今天已经发了 {used} 条（上限 {MAX_POSTS_PER_DAY}）"},
                          ensure_ascii=False)
    gap = d.minutes_since_last_post()
    # MIN_POST_INTERVAL_MIN == 0 表示**不冷却**（主人在设置页关掉时才会是 0）
    if MIN_POST_INTERVAL_MIN > 0 and gap is not None and gap < MIN_POST_INTERVAL_MIN:
        return json.dumps({"ok": False,
                           "error": f"距上次发帖 {gap:.0f} 分钟，冷却 {MIN_POST_INTERVAL_MIN} 分钟未过"},
                          ensure_ascii=False)
    if dr["kind"] == "comment" and d.has_posted_bvid(dr["bvid"]):
        return json.dumps({"ok": False, "error": "这个视频已经发过评论了"}, ensure_ascii=False)
    if dr["kind"] == "reply" and dr["root_rpid"]:
        # 🔴 去重粒度修正（2026-10-22）：以前只按**楼层**去重，
        # 楼里冒出第 2、3 条新评论时会被判"这层回过了"直接吞掉 —— 该回的没回。
        # 现在：给了 parent（真·楼中楼）就按 (root, parent) 精确去重；
        # 只给 root（回复楼主）才退回楼层级。
        _pr = dr.get("parent_rpid")
        if _pr and str(_pr) != str(dr["root_rpid"]):
            if d.has_posted_reply_to(dr["bvid"], dr["root_rpid"], _pr):
                return json.dumps({"ok": False,
                                   "error": "这条子评论已经回复过了"}, ensure_ascii=False)
        elif d.has_posted_reply(dr["bvid"], dr["root_rpid"]):
            return json.dumps({"ok": False, "error": "这个楼层已经回复过了"}, ensure_ascii=False)

    c = client(need_login=True)
    vid = api.get_video(c, dr["bvid"])
    try:
        tags = api.get_tags(c, dr["bvid"])
    except wbi.BiliError:
        tags = []
    sv = safety.check_video({**vid, "tags": tags})
    if sv.action == safety.NO_REPLY:
        d.mark_failed(did, "video_no_reply")
        return json.dumps({"ok": False, "error": "这条视频本身不该碰（见 hits）",
                           "hits": sv.hits}, ensure_ascii=False)

    if dr["target_author"] and dr["kind"] == "reply":
        if d.owner_posted_within_days(dr["target_author"], 7):
            return json.dumps({"ok": False,
                               "error": f"7 天内已经回过 @{dr['target_author']} 了"},
                              ensure_ascii=False)

    if dry_run:
        return _ok({"dry_run": True, "would_post": text,
                    "bvid": dr["bvid"], "kind": dr["kind"],
                    "checks": "全部通过"})

    # 🔴 发前最后一道：把 (root, parent) 再问一次服务端。
    # 草稿可能是老代码建的（parent 空），也可能这期间楼层结构变了。
    # 只给 root 时，resolve 会告诉我们它到底是楼层根还是楼中楼 ——
    # 是楼中楼就补上 parent，别再把回复挂到楼层根（=自己）底下。
    root_id = int(dr["root_rpid"]) if dr["root_rpid"] else 0
    parent_id = int(dr["parent_rpid"]) if dr["parent_rpid"] else 0
    resolved_note = ""
    if root_id and not parent_id:
        try:
            _rs = api.resolve_reply_target(c, dr["bvid"], root_id)
            if _rs["is_sub"]:
                root_id, parent_id = _rs["root"], _rs["parent"]
                resolved_note = (f"发前补正：{_rs['rpid']} 其实是楼中楼，"
                                 f"已挂到 root={root_id}/parent={parent_id}")
        except wbi.BiliError as e:
            return json.dumps({"ok": False,
                               "error": f"发前解析回复目标失败，已中止：{e.message}",
                               "draft_id": did}, ensure_ascii=False)

    res = api.post_comment(
        c, dr["bvid"], text,
        root=root_id,
        parent=parent_id,
    )
    d.mark_posted(did, res)
    d.record_action("post", dr["bvid"], {"draft_id": did, "kind": dr["kind"]}, res)
    out = {
        "draft_id": did,
        "bvid": dr["bvid"],
        "text": text,
        "result": res,
        "note": "回执成功 ≠ 立刻公开可见（新号/大 UP 下可能先压审）。"
                "看不到就写「没查到」，不要写「没发出去」——两者不一样。",
    }
    if resolved_note:
        out["resolved"] = resolved_note
    # 把 rpid / root 提升到顶层：调用方不用再翻 result 那一层
    for k in ("rpid", "rpid_str", "root", "parent"):
        val = res.get(k)
        if val is not None and str(val).strip() not in ("", "0"):
            out[k] = val
    return _ok(out)


@write_tool()
def comment_delete(bvid: str, rpid: int) -> str:
    """删自己发过的评论。

    ⚠️ **只删本插件发过的**：rpid 必须能在本地记录里查到，
    否则拒绝（防模型猜一个 rpid 去删别人的评论）。"""
    d = db()
    found = d.find_posted_draft_by_rpid(bvid, rpid)
    if not found:
        return json.dumps({
            "ok": False,
            "error": "本地没有这条评论的记录，拒绝删除（只能删本插件发过的评论）",
        }, ensure_ascii=False)
    c = client(need_login=True)
    res = api.delete_comment(c, bvid, int(rpid))
    d.record_action("delete", bvid, {"rpid": rpid, "draft_id": found["id"]}, res)
    return _ok({"bvid": bvid, "rpid": rpid, "result": res})


# ─────────────────────────── 状态 / 配置 ───────────────────────────

@tool()
def verify_posted(draft_id: int) -> str:
    """**发后复核**：去 B 站真查这条评论/回复还在不在。

    只为本插件发过的草稿服务（rpid 从本地记录里取，不接受外部传入）。

    查法分两种（`main` 那条按热度排序，新回复常被压到很后面，查不到**不代表没发**）：
      - 顶层评论 → `/x/v2/reply/wbi/main` 翻页找 rpid
      - 楼中楼回复 → `/x/v2/reply/reply?root=<楼层>`，这个是准的，不受热度排序影响

    状态含义：
      - `found`     真查到了，B 站上确实有
      - `not_found` 查不到（可能压审/被删/还在排队，**不等于没发出去**）
      - `error`     接口出错，什么都别下结论
    """
    d = db()
    did = int(draft_id)
    dr = d.get_draft(did)
    if not dr:
        return json.dumps({"ok": False, "error": f"草稿 {did} 不存在"}, ensure_ascii=False)
    if dr.get("status") != "posted":
        return json.dumps({"ok": False,
                           "error": f"这条还没发（状态 {dr.get('status')}），没什么可复核的"},
                          ensure_ascii=False)

    res = {}
    try:
        res = json.loads(dr.get("result") or "{}")
    except Exception:
        res = {}
    rpid = res.get("rpid") or res.get("rpid_str")
    if not rpid:
        return json.dumps({"ok": False, "error": "本地记录里没有 rpid，没法定位"},
                          ensure_ascii=False)
    rpid = int(rpid)
    bvid = dr["bvid"]
    kind = dr.get("kind") or "comment"
    root = int(dr["root_rpid"]) if dr.get("root_rpid") else 0

    c = client()
    try:
        vid = api.get_video(c, bvid)
        aid = vid["aid"]
    except wbi.BiliError as e:
        return json.dumps({"ok": False, "error": f"取视频详情失败: {e}"}, ensure_ascii=False)

    found = None
    checked_by = ""
    guard_api = wbi.guard_api

    # 楼中楼：root 接口最准（但也要 wbi 签名）
    if kind == "reply" and root:
        checked_by = "reply/reply"
        try:
            c.ensure_buvid3()
            r = c.get("/x/v2/reply/reply",
                      {"type": 1, "oid": aid, "root": root, "rpid": rpid,
                       "ps": 20, "pn": 1}, wbi=True)
            guard_api(r, "查楼中楼")
            for x in ((r.get("data") or {}).get("replies") or []):
                if int(x.get("rpid") or 0) == rpid:
                    found = x
                    break
        except wbi.BiliError as e:
            return json.dumps({"ok": False, "error": f"查楼中楼失败: {e}"}, ensure_ascii=False)

    # 顶层评论：翻几页找（热度排序，新评论在后面）
    if found is None and kind == "comment":
        checked_by = "reply/wbi/main"
        try:
            c.ensure_buvid3()
            for pn in range(1, 6):
                r = c.get("/x/v2/reply/wbi/main",
                          {"type": 1, "oid": aid, "mode": 3, "ps": 20, "pn": pn},
                          wbi=True, dm=True)
                guard_api(r, "查评论列表")
                data = r.get("data") or {}
                tr = list(data.get("top_replies") or [])
                up_top = ((data.get("upper") or {}).get("top"))
                pool = ([up_top] if up_top else []) + tr + list(data.get("replies") or [])
                for x in pool:
                    if int(x.get("rpid") or 0) == rpid:
                        found = x
                        break
                if found:
                    break
                if (data.get("cursor") or {}).get("is_end"):
                    break
                time.sleep(0.12)  # 和 get_comments 同一节奏，翻页太快会被风控
        except wbi.BiliError as e:
            return json.dumps({"ok": False, "error": f"查评论列表失败: {e}"}, ensure_ascii=False)

    if found:
        out = _ok({
            "status": "found",
            "draft_id": did, "bvid": bvid, "rpid": rpid,
            "checked_by": checked_by,
            "author": (found.get("member") or {}).get("uname"),
            "text": (found.get("content") or {}).get("message", "")[:200],
            "like": found.get("like"),
            "reply_count": found.get("rcount"),
            "ctime": found.get("ctime"),
        })
    else:
        out = _ok({
            "status": "not_found",
            "draft_id": did, "bvid": bvid, "rpid": rpid,
            "checked_by": checked_by,
            "note": "查不到**不等于没发出去** —— 可能压审/被删/还在排队。"
                    "下结论请写「没查到」。",
        })
    d.record_action("verify", bvid, {"draft_id": did, "rpid": rpid}, {"ok": True,
                                                                      "status": json.loads(out)["status"]})
    return out


@tool()
def status() -> str:
    """看插件现在什么状态：登录没、暂停没、今天发了几条、额度还剩多少、草稿积压多少。"""
    d = db()
    st = d.stats()
    cred_ok = False
    uname = None
    try:
        c = client()
        cred_ok = c.logged_in
        if cred_ok:
            uname = c.uname()
    except Exception:
        pass
    return _ok({
        **st,
        "logged_in": cred_ok,
        "uname": uname,
        "limits": {"max_posts_per_day": MAX_POSTS_PER_DAY,
                   "min_interval_min": MIN_POST_INTERVAL_MIN},
        "quota_left": max(0, MAX_POSTS_PER_DAY - st["posts_24h"]),
        "write_enabled": WRITE_ENABLED,
        "home": str(HOME),
        "db": DB_PATH,
        **credential_status(),
    })


@tool()
def pause(reason: str = "手动暂停") -> str:
    """停掉所有发帖（读工具不受影响）。风控或想静静的时候用。"""
    db().set_paused(True, reason)
    db().record_action("pause", reason, {})
    return _ok({"paused": True, "reason": reason})


@tool()
def resume() -> str:
    """恢复发帖。**建议风控后至少隔几小时再调**。"""
    db().set_paused(False)
    db().record_action("resume", "", {})
    return _ok({"paused": False})


@tool()
def audit(limit: int = 20) -> str:
    """看最近的审计流水（发帖、删帖、暂停、风控事件）。"""
    return _ok({"actions": db().recent_actions(int(limit or 20))})


@tool()
def reply_templates() -> str:
    """返回当前的安全策略摘要（词表命中什么会被拦），方便自查。"""
    return _ok({
        "no_reply_topics": [name for name, _, _ in safety.NO_REPLY_TOPICS],
        "block": ["insult", "links", "fake_human"],
        "rewrite": ["too_short", "too_long", "markdown", "hashtag", "emoji"],
        "warn": ["exclamations", "preaching", "ai_tone", "model_ranking"],
        "allowlist_context": "刀/破防/便当/胃疼/糖 等动画语境词只放行 warn，不放行 block/no_reply",
        "hard_block_cannot_be_approved": True,
    })


@tool()
def persona_prompt() -> str:
    """**写评论之前先看这个**：当前人设是谁、怎么说话。

    返回提示块（给模型读）+ 结构化字段。人设文件改了会自动重载，不用重启。

    ⚠️ 这**不是安全规则**。安全在 `safety_check` 里硬执行，
       提示词只是建议 —— 模型一句"忽略之前的设定"绕不过代码闸门。"""
    data = persona.load_persona()
    return _ok({
        "name": persona.persona_name(),
        "account": persona.account_name(),
        "catchphrase": persona.catchphrase(),
        "catchphrase_meta": persona.catchphrase_meta(),
        "prompt": persona.persona_prompt(),
        "persona_path": persona._persona_path(),
        "loaded": bool(data),
    })


@tool()
def persona_read() -> str:
    """**读人设原文**（设置页编辑器用）。

    返回 `text`（原文，含注释和缩进）、`path`、`summary`（摘要）。
    这是给人改的，所以给原文 —— 不是重新序列化的 YAML。"""
    return _ok({
        "text": persona.read_raw(),
        "path": persona._persona_path(),
        "summary": persona.persona_summary(),
    })


@tool()
def persona_save(text: str, backup: bool = True) -> str:
    """**保存人设原文**（整段写回，写前自动留 .bak）。

    校验：写之前先解析一遍，解析不出来直接拒绝；
          写完再回读解析一遍，还不行就**自动回滚**。
    这就是为什么设置页敢让人在这里直接改文件：
    一次写坏不会让人设静默变空（那会让禁词/长度检查跟着失效）。

    - `text`: 完整的人设 YAML 原文
    - `backup`: 是否留 `.bak`（默认留）
    """
    r = persona.save_raw(text, backup=bool(backup))
    if not r.get("ok"):
        return json.dumps(r, ensure_ascii=False)
    r["summary"] = persona.persona_summary()
    r["text"] = persona.read_raw()
    return _ok(r)


@tool()
def style_check(text: str) -> str:
    """查这段文字**像不像这个人写的**（纯风格，不做安全判断）。

    返回 `ok / issues / warnings / score`。
    issues 非空就该改 —— 但**这不阻止发帖**（安全才阻止）。
    安全请用 `safety_check`。"""
    return _ok(persona.check_style(text))


def main() -> None:
    print(f"[bili-plus] home={HOME} write={WRITE_ENABLED} "
          f"tools={'ALL' if not _ALLOWED else sorted(_ALLOWED)}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
