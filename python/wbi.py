"""wbi.py —— B站 HTTP 底座：wbi 签名 + 设备指纹 + 统一请求/错误处理。

设计要点（都是实测出来的，改之前先看 _probe_wbi*.py 的记录）：
1. **零第三方依赖**：只用 stdlib（urllib）。这样插件的 venv 只需要 mcp + Pillow。
2. **wbi 签名**：mix key = (img_key + sub_key) 按固定表重排取前 32 位。
   img_key/sub_key 来自 /x/web-interface/nav 的 wbi_img，**匿名也返回**，
   所以签名本身不需要登录（但部分接口仍要 cookie，见下）。
3. **设备指纹**：部分接口（space/wbi/arc/search 等）不带 buvid3 会直接 HTTP 412。
   buvid3 从 /x/frontend/finger/spi 匿名领取，必须写进 Cookie。
4. **dm 参数**：spwu 系列接口还要求 dm_img_list / dm_img_str / dm_cover_img_str /
   dm_img_inter 四个「伪指纹」参数，值可以是固定字符串（不参与签名校验的语义）。
5. 颜色：**错误一律转成 dict 返回**，永不抛异常给 MCP 层 —— 模型拿到结构化错误
   比拿到 "Tool execution failed" 有用得多。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# B站前端写死的 mixin key 重排表（64 项）。改动这个表 = 签名全错。
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# 风控码：命中就说明被限流/风控了，调用方要停手，别重试。
RISK_CODES = frozenset({-412, -352, -509, -799})

_API = "https://api.bilibili.com"
_PASSPORT = "https://passport.bilibili.com"

# dm 系列伪指纹参数。值是固定串，B站不校验其语义，只校验「有」。
_DM_PARAMS = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": (
        "QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgR1RYIDE2NTAgKDB4MDAwMDFGOTEp"
        "IERpcmVjdDNEMTEgdnNfNV8wIHBzXzVfMCwgRDNEMTEpR29vZ2xlIEluYy4gKE5WSURJQS"
    ),
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


class BiliError(Exception):
    """业务错误。code 是 B站返回码，http 是 HTTP 状态码（若走的是 HTTP 错误）。"""

    def __init__(self, message: str, *, code: int | None = None, http: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http = http

    @property
    def risk(self) -> bool:
        return self.code in RISK_CODES or self.http == 412

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": False, "error": self.message}
        if self.code is not None:
            d["api_code"] = self.code
        if self.http is not None:
            d["http_status"] = self.http
        if self.risk:
            d["risk_control"] = True
            d["hint"] = "触发风控，请停手等 5-10 分钟，不要重试同一接口"
        return d


# ─────────────────────────── wbi 签名 ───────────────────────────


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign(params: Mapping[str, Any], img_key: str, sub_key: str) -> dict[str, str]:
    """给参数加上 wts + w_rid，返回全部为 str 的 dict。"""
    mk = _mixin_key(img_key, sub_key)
    p = {k: ("" if v is None else str(v)) for k, v in params.items()}
    p["wts"] = str(int(time.time()))
    items = sorted((k, v) for k, v in p.items() if v != "")
    q = urllib.parse.urlencode(items, quote_via=urllib.parse.quote)
    # B站要求 query 里不能出现这几个字符（连编码后的都不行）
    for ch in "!'()*":
        q = q.replace(ch, "")
    p["w_rid"] = hashlib.md5((q + mk).encode("utf-8")).hexdigest()
    return p


def _url(base: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return base
    return base + "?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, "")},
        quote_via=urllib.parse.quote,
    )


# ─────────────────────────── 客户端 ───────────────────────────


class BiliClient:
    """一个 client 实例 = 一份 cookie + 一份 wbi key 缓存。

    用法：
        c = BiliClient(credential={...})   # 或 credential=None 匿名
        c.get(api_url, params)             # 自动判 wbi
        c.get(api_url, params, wbi=True)   # 强制签
    """

    _KEY_TTL = 600.0  # wbi key 10 分钟有效，官方说每天会变，缓存 10 分钟足够安全

    def __init__(self, credential: Mapping[str, str] | None = None, *, timeout: float = 20.0):
        self.credential = dict(credential or {})
        self.timeout = timeout
        self._img_key = ""
        self._sub_key = ""
        self._key_at = 0.0
        self._buvid3 = ""
        self._nav_cache: dict[str, Any] | None = None
        self._nav_at = 0.0
        if self.credential.get("buvid3"):
            self._buvid3 = self.credential["buvid3"]

    # ── cookie ──
    @property
    def logged_in(self) -> bool:
        return bool(self.credential.get("sessdata"))

    def cookie_header(self) -> str:
        c = self.credential
        parts: list[str] = []
        if c.get("sessdata"):
            parts.append(f"SESSDATA={c['sessdata']}")
        if c.get("bili_jct"):
            parts.append(f"bili_jct={c['bili_jct']}")
        if c.get("dedeuserid"):
            parts.append(f"DedeUserID={c['dedeuserid']}")
        if self._buvid3:
            parts.append(f"buvid3={self._buvid3}")
        if c.get("buvid4"):
            parts.append(f"buvid4={c['buvid4']}")
        return "; ".join(parts)

    @property
    def csrf(self) -> str:
        """发帖要用的 csrf，就是 cookie 里的 bili_jct。"""
        return self.credential.get("bili_jct", "")

    # ── 底层请求 ──
    def _raw(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        binary: bool = False,
    ) -> Any:
        h = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        }
        ck = self.cookie_header()
        if ck:
            h["Cookie"] = ck
        if headers:
            h.update(headers)

        body = None
        if data is not None:
            body = urllib.parse.urlencode(
                {k: v for k, v in data.items() if v is not None}, quote_via=urllib.parse.quote
            ).encode("utf-8")
            h["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                if binary:
                    return raw
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
                if e.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            except Exception:
                pass
            # 412 = 风控；再往里捞一次 B站自己的 code
            code = None
            try:
                j = json.loads(raw.decode("utf-8", "replace"))
                code = j.get("code")
            except Exception:
                code = None
            raise BiliError(f"HTTP {e.code} {e.reason}", code=code, http=e.code) from None
        except urllib.error.URLError as e:
            raise BiliError(f"网络错误: {e.reason}") from None
        except TimeoutError:
            raise BiliError("请求超时") from None

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        wbi: bool = False,
        dm: bool = False,
        absolute: str | None = None,
        headers: Mapping[str, str] | None = None,
        binary: bool = False,
    ) -> Any:
        """GET 一个 api.bilibili.com 接口。path 以 / 开头；absolute 可直接给完整 URL。"""
        base = absolute or (_API + path)
        p: dict[str, Any] = dict(params or {})
        if dm:
            p.update(_DM_PARAMS)
        if wbi:
            ik, sk = self.wbi_keys()
            p = sign(p, ik, sk)
        return self._raw(_url(base, p), headers=headers, binary=binary)

    def post(
        self,
        path: str,
        data: Mapping[str, Any] | None = None,
        *,
        absolute: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST（发评论等写操作）。csrf 由调用方显式放进 data，避免忘传。"""
        base = absolute or (_API + path)
        return self._raw(base, method="POST", data=dict(data or {}), headers=headers)

    # ── wbi key ──
    def wbi_keys(self) -> tuple[str, str]:
        if self._img_key and (time.time() - self._key_at) < self._KEY_TTL:
            return self._img_key, self._sub_key
        nav = self._raw(_API + "/x/web-interface/nav")
        wbi = ((nav or {}).get("data") or {}).get("wbi_img") or {}
        img = wbi.get("img_url") or ""
        sub = wbi.get("sub_url") or ""
        if not img or not sub:
            raise BiliError("拿不到 wbi key（nav 接口异常）", code=(nav or {}).get("code"))
        self._img_key = img.rsplit("/", 1)[-1].split(".")[0]
        self._sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
        self._key_at = time.time()
        return self._img_key, self._sub_key

    # ── 设备指纹 ──
    def ensure_buvid3(self) -> str:
        """匿名领 buvid3。**必须**在打 space 系列接口前调用，否则 412。"""
        if self._buvid3:
            return self._buvid3
        try:
            d = self._raw(_API + "/x/frontend/finger/spi")
            self._buvid3 = ((d or {}).get("data") or {}).get("b_3") or ""
        except BiliError:
            self._buvid3 = ""
        return self._buvid3

    # ── 登录态 ──
    def nav(self, *, fresh: bool = False) -> dict[str, Any]:
        if not fresh and self._nav_cache is not None and (time.time() - self._nav_at) < 60:
            return self._nav_cache
        d = self._raw(_API + "/x/web-interface/nav")
        self._nav_cache = d
        self._nav_at = time.time()
        return d

    def is_login(self) -> bool:
        try:
            return bool((self.nav(fresh=True).get("data") or {}).get("isLogin"))
        except BiliError:
            return False

    def uid(self) -> str:
        d = (self.nav().get("data") or {})
        if d.get("mid"):
            return str(d["mid"])
        return self.credential.get("dedeuserid", "")

    def uname(self) -> str:
        return (self.nav().get("data") or {}).get("uname") or ""


# ─────────────────────────── 工具函数 ───────────────────────────


def ok(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"ok": True}
    d.update(kw)
    return d


def err(e: Exception | str, **kw: Any) -> dict[str, Any]:
    if isinstance(e, BiliError):
        d = e.as_dict()
    else:
        d = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    d.update(kw)
    return d


def guard_api(resp: Mapping[str, Any], what: str) -> None:
    """把 code != 0 的响应转成 BiliError。调用方不再自己判 code。"""
    code = resp.get("code", 0)
    if code == 0:
        return
    msg = resp.get("message") or resp.get("msg") or "未知错误"
    raise BiliError(f"{what}失败: {msg}", code=code)


def unwrap(resp: Mapping[str, Any], what: str) -> Any:
    """校验并取出 data 字段。"""
    guard_api(resp, what)
    return resp.get("data")


def clean_title(s: str) -> str:
    """去掉搜索结果里的 <em class='keyword'> 高亮标签。"""
    if not s:
        return ""
    out = s
    for a in ('<em class="keyword">', '</em>', "<em class='keyword'>"):
        out = out.replace(a, "")
    return out.strip()


def fmt_duration(sec: int | str | None) -> str:
    try:
        n = int(sec or 0)
    except (TypeError, ValueError):
        return "0:00"
    if n >= 3600:
        return f"{n // 3600}:{(n % 3600) // 60:02d}:{n % 60:02d}"
    return f"{n // 60}:{n % 60:02d}"


def strip_index(arr: Iterable[Any]) -> list[int]:
    """videoshot 的 index 偶尔不是升序（开头有重复的 0），这里只保证是 int 列表。"""
    out: list[int] = []
    for x in arr:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            out.append(0)
    return out


def credential_from_env_or_file(path: str) -> dict[str, str]:
    """凭证四字段，文件名沿用插件生态里的 bili_credential.json。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict[str, str] = {}
    # 内部一律用**小写**键名（BiliClient / cookie_header / logged_in 都读小写），
    # 这里只负责把文件里可能的 B站原始大小写归一化过来。
    alias = {
        "sessdata": "sessdata", "SESSDATA": "sessdata",
        "bili_jct": "bili_jct", "csrf": "bili_jct", "BILI_JCT": "bili_jct",
        "buvid3": "buvid3", "BUVID3": "buvid3",
        "buvid4": "buvid4", "BUVID4": "buvid4",
        "dedeuserid": "dedeuserid", "DedeUserID": "dedeuserid",
    }
    for k, v in d.items():
        canon = alias.get(k)
        if canon and isinstance(v, (str, int)) and str(v).strip():
            out[canon] = str(v).strip()
    return out
