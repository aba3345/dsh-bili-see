"""api.py —— B站业务接口封装。

约定：每个函数返回 **已经整理好的 dict**（不是原始响应），失败时**抛 BiliError**，
由 server.py 统一转成 {"ok": false, ...}。这样 MCP 工具层永远只有一行 try/except。

所有端点的参数名都是实测确认过的，别凭记忆改（见 bili-reverse/04-python-mcp.md）。
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from wbi import (
    BiliClient,
    BiliError,
    clean_title,
    fmt_duration,
    guard_api,
    strip_index,
    unwrap,
)

# ─────────────────────────── 视频 ───────────────────────────


def get_video(c: BiliClient, bvid: str) -> dict[str, Any]:
    """视频详情。pages 是分P数量（不是页码）。"""
    d = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    owner = d.get("owner") or {}
    stat = d.get("stat") or {}
    return {
        "bvid": d.get("bvid"),
        "aid": d.get("aid"),
        "cid": d.get("cid"),
        "title": d.get("title"),
        "description": (d.get("desc") or "")[:1000],
        "author": owner.get("name"),
        "author_mid": owner.get("mid"),
        "duration": d.get("duration"),
        "duration_text": fmt_duration(d.get("duration")),
        "pubdate": d.get("pubdate"),
        "pages": len(d.get("pages") or []),
        "page_list": [
            {"cid": p.get("cid"), "page": p.get("page"), "part": p.get("part"),
             "duration": p.get("duration")}
            for p in (d.get("pages") or [])[:20]
        ],
        "stat": {
            "view": stat.get("view"), "danmaku": stat.get("danmaku"),
            "reply": stat.get("reply"), "favorite": stat.get("favorite"),
            "coin": stat.get("coin"), "like": stat.get("like"),
            "share": stat.get("share"),
        },
        "pic": d.get("pic"),
        "tname": d.get("tname"),
    }


def get_tags(c: BiliClient, bvid: str) -> list[str]:
    """视频标签。失败返回空列表（不是关键信息，别因它让整个工具失败）。"""
    try:
        d = c.get("/x/tag/archive/tags", {"bvid": bvid})
        if d.get("code") != 0:
            return []
        return [t.get("tag_name", "") for t in (d.get("data") or []) if t.get("tag_name")]
    except BiliError:
        return []


def search(c: BiliClient, keyword: str, num: int = 10, order: str = "totalrank",
           page: int = 1) -> dict[str, Any]:
    """搜索视频。order: totalrank=综合 click=播放 pubdate=最新 dm=弹幕 stow=收藏。"""
    num = max(1, min(int(num or 10), 50))
    c.ensure_buvid3()
    d = unwrap(
        c.get("/x/web-interface/wbi/search/type",
              {"search_type": "video", "keyword": keyword, "page": page,
               "page_size": num, "order": order},
              wbi=True, dm=True),
        "搜索",
    )
    rows = d.get("result") or []
    vids = []
    for v in rows[:num]:
        vids.append({
            "bvid": v.get("bvid"),
            "aid": v.get("aid"),
            "title": clean_title(v.get("title") or ""),
            "author": v.get("author"),
            "author_mid": v.get("mid"),
            "play": v.get("play"),
            "danmaku": v.get("video_review"),
            "review": v.get("review"),
            "favorites": v.get("favorites"),
            "duration": v.get("duration"),
            "description": (v.get("description") or "")[:200],
            "pubdate": v.get("pubdate"),
            "tag": v.get("tag"),
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}" if v.get("bvid") else None,
        })
    return {"keyword": keyword, "count": len(vids), "total": d.get("numResults"), "videos": vids}


def popular(c: BiliClient, pn: int = 1, ps: int = 20) -> dict[str, Any]:
    """热门视频。ps 上限 50。

    ⚠️ `pubdate` 必须带上：宿主那层要靠它做「只看最近 N 天」的日期闸
       （index.js#isFresh）。少了这个字段，热门来源的视频会全部绕过日期过滤
       —— 表面上还在筛，实际等于没筛。
    """
    ps = max(1, min(int(ps or 20), 50))
    d = unwrap(c.get("/x/web-interface/popular", {"pn": pn, "ps": ps}), "取热门")
    lst = d.get("list") or []
    return {
        "page": pn,
        "count": len(lst),
        "videos": [{
            "bvid": v.get("bvid"),
            "title": v.get("title"),
            "author": (v.get("owner") or {}).get("name"),
            "author_mid": (v.get("owner") or {}).get("mid"),
            "play": (v.get("stat") or {}).get("view"),
            "like": (v.get("stat") or {}).get("like"),
            "danmaku": (v.get("stat") or {}).get("danmaku"),
            "reply": (v.get("stat") or {}).get("reply"),
            "duration": v.get("duration"),
            "duration_text": fmt_duration(v.get("duration")),
            "pubdate": v.get("pubdate"),
            "desc": (v.get("desc") or "")[:100],
            "tname": v.get("tname"),
            "aid": v.get("aid"),
            "cid": v.get("cid"),
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
        } for v in lst],
    }


def rank(c: BiliClient, category: str = "all", day: int = 3) -> dict[str, Any]:
    """分区排行榜。day: 3=三日 7=一周 30=一月。"""
    d = unwrap(c.get("/x/web-interface/ranking/v2",
                     {"rid": _zone_rid(category), "type": "all"}), "取排行榜")
    lst = d.get("list") or []
    return {
        "category": category,
        "day": day,
        "count": len(lst),
        "videos": [{
            "bvid": v.get("bvid"),
            "title": v.get("title"),
            "author": (v.get("owner") or {}).get("name"),
            "play": (v.get("stat") or {}).get("view"),
            "like": (v.get("stat") or {}).get("like"),
            "coin": (v.get("stat") or {}).get("coin"),
            "score": v.get("score"),
            "tname": v.get("tname"),
        } for v in lst[:100]],
    }


_ZONES = {
    "all": 0, "动画": 1, "番剧": 13, "国创": 168, "音乐": 3, "舞蹈": 129,
    "游戏": 4, "知识": 36, "科技": 188, "运动": 234, "影视": 181, "鬼畜": 119,
    "美食": 76, "生活": 160, "数码": 188,
}


def _zone_rid(category: str) -> int:
    if isinstance(category, int):
        return category
    return _ZONES.get(str(category), 0)


def user_videos(c: BiliClient, uid: int, pn: int = 1, ps: int = 30,
                order: str = "pubdate", keyword: str = "") -> dict[str, Any]:
    """UP 主投稿列表。order: pubdate=最新 click=播放 stow=收藏。"""
    ps = max(1, min(int(ps or 30), 50))
    c.ensure_buvid3()
    params: dict[str, Any] = {
        "mid": uid, "ps": ps, "pn": pn, "order": order,
        "platform": "web", "web_location": "1550101", "order_avoided": "true",
    }
    if keyword:
        params["keyword"] = keyword
    d = unwrap(c.get("/x/space/wbi/arc/search", params, wbi=True, dm=True), "取投稿列表")
    vlist = (d.get("list") or {}).get("vlist") or []
    return {
        "uid": uid,
        "page": pn,
        "total": (d.get("page") or {}).get("count"),
        "count": len(vlist),
        "videos": [{
            "bvid": v.get("bvid"),
            "title": v.get("title"),
            "play": v.get("play"),
            "comment": v.get("comment"),
            "created": v.get("created"),
            "length": v.get("length"),
            "description": (v.get("description") or "")[:150],
        } for v in vlist],
    }


def user_info(c: BiliClient, uid: int) -> dict[str, Any]:
    """UP 主资料。粉丝数走 relation 接口，info 里那份常常是 0。"""
    info = unwrap(c.get("/x/space/wbi/acc/info", {"mid": uid}, wbi=True, dm=True), "取用户资料")
    fans = following = None
    try:
        rel = c.get("/x/relation/stat", {"vmid": uid})
        if rel.get("code") == 0:
            rd = rel.get("data") or {}
            fans = rd.get("follower")
            following = rd.get("following")
    except BiliError:
        pass
    return {
        "uid": info.get("mid"),
        "name": info.get("name"),
        "sign": info.get("sign"),
        "level": info.get("level"),
        "face": info.get("face"),
        "fans": fans,
        "following": following,
        "likes": (info.get("likes") or 0) if isinstance(info.get("likes"), int) else None,
        "archive_view": (info.get("archive_count") or 0),
        "is_senior_member": bool(info.get("is_senior_member")),
    }


# ─────────────────────────── 字幕 ───────────────────────────


def get_subtitle(c: BiliClient, bvid: str) -> dict[str, Any]:
    """取字幕全文。三跳：view 拿 aid/cid → player/wbi/v2 拿 subtitles → 下载 json。

    ⚠️ AAI（AI 生成）字幕**需要登录**才在 subtitles 里出现；未登录只会返回空数组，
    这不是"这个视频没字幕"。所以这里的 message 要区分两种情况。
    """
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    aid, cid = v.get("aid"), v.get("cid")
    if not cid:
        raise BiliError("拿不到 cid")
    c.ensure_buvid3()
    p = unwrap(c.get("/x/player/wbi/v2", {"aid": aid, "cid": cid}, wbi=True, dm=True),
               "取播放器信息")
    sub = p.get("subtitle") or {}
    subs = sub.get("subtitles") or []
    need_login = bool(sub.get("need_login_subtitle"))
    manual = [s for s in subs if (s.get("lan") or "").startswith("zh")]
    pick = (manual or subs)
    if not pick:
        msg = "该视频没有字幕"
        if need_login and not c.logged_in:
            msg = "该视频只有 AI 字幕，需要登录后才能读取（当前未登录）"
        elif need_login and c.logged_in:
            msg = "该视频只有 AI 字幕，接口要求登录但当前凭证可能失效"
        return {"bvid": bvid, "title": v.get("title"), "message": msg,
                "segments": 0, "text": "", "need_login": need_login}

    s = pick[0]
    url = s.get("subtitle_url") or ""
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        raise BiliError("字幕地址为空")
    raw = c.get("", absolute=url, headers={"Referer": "https://www.bilibili.com/"})
    body = (raw or {}).get("body") or []
    text = "\n".join(b.get("content", "") for b in body if b.get("content"))
    return {
        "bvid": bvid,
        "title": v.get("title"),
        "language": s.get("lan_doc") or s.get("lan"),
        "lan": s.get("lan"),
        "segments": len(body),
        "chars": len(text),
        "text": text,
        "candidates": [{"lan": x.get("lan"), "lan_doc": x.get("lan_doc"),
                        "ai": (x.get("lan") or "").startswith("ai")} for x in subs],
    }


# ─────────────────────────── 评论 ───────────────────────────


def _comment_row(r: Mapping[str, Any]) -> dict[str, Any]:
    member = r.get("member") or {}
    content = r.get("content") or {}
    replies = r.get("replies") or []
    return {
        "rpid": r.get("rpid"),
        "rpid_str": r.get("rpid_str") or str(r.get("rpid") or ""),
        "root": r.get("root"),
        "parent": r.get("parent"),
        "user": member.get("uname"),
        "uid": member.get("mid"),
        "content": content.get("message") or "",
        "like": r.get("like"),
        "reply_count": r.get("rcount") or r.get("count") or 0,
        "ctime": r.get("ctime"),
        "is_up": bool((member.get("mid") or 0) == (r.get("_up_mid") or 0)),
        "top_replies": [{
            "rpid": x.get("rpid"),
            "root": x.get("root"),
            "parent": x.get("parent"),
            "user": (x.get("member") or {}).get("uname"),
            "content": ((x.get("content") or {}).get("message") or "")[:200],
            "like": x.get("like"),
        } for x in replies[:2]],
    }


def get_comments(c: BiliClient, bvid: str, num: int = 30, mode: int = 3,
                 with_replies: bool = True) -> dict[str, Any]:
    """拉热门评论。mode: 3=热度 2=时间。置顶评论会插到最前。

    子评论（楼中楼）只对前 with_replies 决定是否抓，且有 0.12s 间隔 —— 这个间隔是
    原插件实测出来的，删掉会被风控。
    """
    num = max(1, min(int(num or 30), 50))
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    aid = v.get("aid")
    up_mid = (v.get("owner") or {}).get("mid")
    c.ensure_buvid3()
    d = unwrap(
        c.get("/x/v2/reply/wbi/main",
              {"type": 1, "oid": aid, "mode": mode, "next": 0, "ps": num}, wbi=True, dm=True),
        "拉评论",
    )
    rows: list[dict[str, Any]] = []
    top = ((d.get("upper") or {}).get("top") or {})
    if top:
        top = dict(top)
        top["_up_mid"] = up_mid
        rows.append(_comment_row(top))
    for r in (d.get("replies") or []):
        r = dict(r)
        r["_up_mid"] = up_mid
        rows.append(_comment_row(r))

    if with_replies:
        for row in rows:
            if not row["reply_count"]:
                continue
            try:
                time.sleep(0.12)  # 实测必须，删了会被风控
                sub = c.get("/x/v2/reply/reply",
                            {"type": 1, "oid": aid, "root": row["rpid"], "ps": 10, "pn": 1},
                            wbi=True)
                if sub.get("code") == 0:
                    row["sub_replies"] = [{
                        "rpid": x.get("rpid"),
                        "root": x.get("root"),
                        "parent": x.get("parent"),
                        "user": (x.get("member") or {}).get("uname"),
                        "uid": ((x.get("member") or {}).get("mid")),
                        "content": ((x.get("content") or {}).get("message") or "")[:300],
                        "like": x.get("like"),
                    } for x in ((sub.get("data") or {}).get("replies") or [])[:10]]
            except BiliError as e:
                row["sub_error"] = e.message
                if e.risk:
                    break

    cursor = d.get("cursor") or {}
    return {
        "bvid": bvid,
        "aid": aid,
        "title": v.get("title"),
        "count": len(rows),
        "is_end": bool(cursor.get("is_end")),
        "next": cursor.get("pagination_reply", {}).get("next_offset") if isinstance(cursor.get("pagination_reply"), dict) else cursor.get("next"),
        "comments": rows,
    }


def resolve_reply_target(c: BiliClient, bvid: str, rpid: int) -> dict[str, Any]:
    """**把任意一条评论 rpid 解析成 B站 要的 (root, parent) 二元组。**

    🔴 为什么必须有这个函数（2026-10-22 实测踩坑）：

      B站 `/x/v2/reply/add` 的楼中楼语义是：
        · `root`   = **楼层根评论**的 rpid（楼层 id）
        · `parent` = 你要回复的**那一条**的 rpid（可以是根，也可以是楼里任意一层）
      只传 `root`、不传 `parent` 时，服务端按 `parent = root` 处理 ——
      回复会挂到楼层根下面，**不是**挂到你心里想回的那条。

      本插件以前就吃了这个亏：模型在楼中楼里看到一条评论（rpid=X，其实
      X 是某楼层的第 N 层），建稿只写了 root_rpid=X、parent 留空。
      结果 B站 把 X 当楼层根、parent 也设成 X，回复直接变成
      **"在自己楼层根底下自言自语"**，被回复的人收不到任何提醒。
      证据：draft 22/23/24/27 的 result.parent 全部 == root。

      ⇒ 建稿/发帖时一律调本函数，从服务端把真实 root 问出来，别再让调用方猜。

    解析顺序（`/x/v2/reply/detail` 的 `root` 字段会原样回显**你传进去的 rpid**，
    所以**不能**用它判断，必须看 `parent` / `root` 是否指向**别人**）：
      ① 直接查 detail：若 `parent != rpid`，说明 rpid 是子评论，
         真实楼层根 = 返回的 `root`；
      ② 若 `parent == rpid`（它本身就是楼层根），root 就用它自己；
      ③ detail 失败 → 退回逐个楼层扫 `/x/v2/reply/reply` 找它属于哪层
         （慢，但不会静默发错地方）。

    返回 `{"rpid","root","parent","user","text","is_sub","resolved_by"}`；
    真的定不了就抛 BiliError —— **宁可报错，也不发到错的地方**。
    """
    if not rpid:
        raise BiliError("rpid 为空，无法解析回复目标")
    rpid = int(rpid)
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    aid = v.get("aid")
    if not aid:
        raise BiliError("拿不到 aid")
    c.ensure_buvid3()

    def _pack(node: Mapping[str, Any], root_id: int, parent_id: int,
              how: str) -> dict[str, Any]:
        member = node.get("member") or {}
        return {
            "rpid": rpid,
            "root": int(root_id),
            "parent": int(parent_id),
            "user": member.get("uname"),
            "uid": member.get("mid"),
            "text": ((node.get("content") or {}).get("message") or "")[:200],
            "is_sub": int(parent_id) != int(root_id),
            "resolved_by": how,
        }

    # ① / ② detail 直查
    try:
        r = c.get("/x/v2/reply/detail",
                  {"type": 1, "oid": aid, "root": rpid, "rpid": rpid}, wbi=True)
        if r.get("code") == 0:
            node = ((r.get("data") or {}).get("root")) or {}
            got = int(node.get("rpid") or 0)
            if got == rpid:
                p = int(node.get("parent") or 0)
                rt = int(node.get("root") or 0)
                if p and p != rpid:
                    # 子评论：真实楼层根是 rt（兜底用 p，两者一般相等）
                    return _pack(node, rt or p, rpid, "detail")
                # 它自己就是楼层根
                return _pack(node, rpid, rpid, "detail-self")
    except BiliError:
        pass  # 落到 ③

    # ③ 兜底：扫楼层找归属
    try:
        top = c.get("/x/v2/reply/wbi/main",
                    {"type": 1, "oid": aid, "mode": 3, "next": 0, "ps": 49},
                    wbi=True, dm=True)
        floors = list(((top.get("data") or {}).get("replies") or []))
        for fl in floors:
            fr = int(fl.get("rpid") or 0)
            if fr == rpid:
                return _pack(fl, rpid, rpid, "scan-floor")
            if not (fl.get("rcount") or 0):
                continue
            time.sleep(0.12)  # 实测必须，删了会被风控
            sub = c.get("/x/v2/reply/reply",
                        {"type": 1, "oid": aid, "root": fr, "ps": 20, "pn": 1},
                        wbi=True)
            for x in ((sub.get("data") or {}).get("replies") or []):
                if int(x.get("rpid") or 0) == rpid:
                    return _pack(x, fr, rpid, "scan-sub")
    except BiliError as e:
        raise BiliError(f"解析回复目标失败：{e.message}") from e

    raise BiliError(
        f"定位不到 rpid={rpid} 属于哪一层楼，为避免回复挂错地方，已中止。"
        "请先确认这条评论还在，或改用楼层根 rpid 回复。")


def post_comment(c: BiliClient, bvid: str, text: str, *,
                 root: int = 0, parent: int = 0) -> dict[str, Any]:
    """发评论 / 回复。**唯一写入口**，调用方必须先过审批与安全检查。

    root=0 → 顶层评论；root>0 且 parent=0 → 回复楼主（parent 自动 = root）；
    root>0 且 parent>0 → 回复某条子评论。

    🔴 传进来的 root 必须是**楼层根**。如果你手上只有"想回的那条评论"的 rpid，
    先过一遍 `resolve_reply_target()` —— 楼中楼里随便一条 rpid 直接当 root
    传进来，B站 会当成新楼层，回复就挂错地方了。
    """
    if not c.logged_in:
        raise BiliError("未登录，无法发评论")
    if not c.csrf:
        raise BiliError("凭证缺少 bili_jct（csrf），无法发评论")
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    aid = v.get("aid")
    if not aid:
        raise BiliError("拿不到 aid")

    data: dict[str, Any] = {
        "oid": aid, "type": 1, "message": text, "csrf": c.csrf,
        "plat": 1, "ordering": "heat", "gaia_source": "main_web",
    }
    if root:
        data["root"] = int(root)
        data["parent"] = int(parent or root)
    # ⚠️ 必须带「具体视频页」的 Referer + Origin，否则 B站 会把评论打成
    # state=17 / attr=6422528（仅自己可见，别人和匿名视角都刷不到）。
    # 实测：wbi.py 默认 Referer 是站首页 https://www.bilibili.com/，
    # 对 /x/v2/reply/add 来说等于「来源不明」，服务端照样返回 code=0 + 真 rpid，
    # 但会静默降权。证据见 _probe_bili_headers.py 的 H1/H2/H3 三组对照。
    headers = {
        "Referer": f"https://www.bilibili.com/video/{bvid}/",
        "Origin": "https://www.bilibili.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = c.post("/x/v2/reply/add", data, headers=headers)
    guard_api(resp, "发评论")
    dd = resp.get("data") or {}
    out: dict[str, Any] = {"ok": True, "message": "评论发送成功", "bvid": bvid, "aid": aid}
    # 按存在性带上标识，方便判重与发后复核
    for k in ("rpid", "rpid_str", "root", "parent"):
        val = dd.get(k)
        if val is not None and str(val).strip() not in ("", "0"):
            out[k] = val
    return out


def delete_comment(c: BiliClient, bvid: str, rpid: int) -> dict[str, Any]:
    """删自己的评论。调用方**必须**先在本地库里确认这条是我们发的。"""
    if not c.logged_in:
        raise BiliError("未登录")
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    aid = v.get("aid")
    if not aid or not rpid:
        raise BiliError("缺少 aid 或 rpid")
    resp = c.post("/x/v2/reply/del",
                  {"oid": aid, "type": 1, "rpid": int(rpid), "csrf": c.csrf},
                  headers={
                      "Referer": f"https://www.bilibili.com/video/{bvid}/",
                      "Origin": "https://www.bilibili.com",
                      "X-Requested-With": "XMLHttpRequest",
                  })
    guard_api(resp, "删除评论")
    return {"ok": True, "message": "已删除", "bvid": bvid, "rpid": rpid}


# ─────────────────────────── AI 总结 ───────────────────────────


def get_ai_conclusion(c: BiliClient, bvid: str, cid: int | None = None,
                      up_mid: int | None = None) -> dict[str, Any]:
    """B站官方 AI 总结（带秒级时间戳的大纲）。

    ⚠️ **接口级风控极严**：实测成功 3 次后持续 HTTP 412，且与其他接口独立。
    调用方必须：低频、失败即放弃、单轮最多 1~2 次。绝不重试。
    """
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    cid = cid or v.get("cid")
    up_mid = up_mid if up_mid is not None else (v.get("owner") or {}).get("mid")
    c.ensure_buvid3()
    resp = c.get("/x/web-interface/view/conclusion/get",
                 {"aid": v.get("aid"), "bvid": bvid, "cid": cid,
                  "up_mid": up_mid, "web_location": "333.788"},
                 wbi=True, dm=True)
    guard_api(resp, "取 AI 总结")
    d = resp.get("data") or {}
    mr = d.get("model_result") or {}
    outline = []
    for seg in (mr.get("outline") or []):
        parts = []
        for p in (seg.get("part_outline") or []):
            parts.append({
                "timestamp": p.get("timestamp"),
                "content": p.get("content"),
            })
        outline.append({
            "title": seg.get("title"),
            "timestamp": seg.get("timestamp"),
            "parts": parts,
        })
    return {
        "bvid": bvid,
        "title": v.get("title"),
        "status": d.get("status"),
        "stid": d.get("stid"),
        "summary": mr.get("summary") or "",
        "outline": outline,
        "outline_count": len(outline),
        "has_summary": bool(mr.get("summary")),
        "note": "AI 总结是转述不是原文；要判断内容仍应读字幕",
    }


# ─────────────────────────── 弹幕 ───────────────────────────


def get_danmaku(c: BiliClient, bvid: str, num: int = 100) -> dict[str, Any]:
    """取弹幕。绕过 XML，直接走 protobuf 太重，这里用简化的 XML 接口。"""
    num = max(1, min(int(num or 100), 1000))
    v = unwrap(c.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    cid = v.get("cid")
    if not cid:
        raise BiliError("拿不到 cid")
    try:
        raw = c.get("/x/v1/dm/list.so", {"oid": cid}, absolute=None, binary=True)
    except BiliError:
        # 老接口常 412，退回 segment 接口
        raw = c.get("", absolute=f"https://api.bilibili.com/x/v2/dm/web/seg.so?oid={cid}&segment_index=1",
                    binary=True)
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    out: list[dict[str, Any]] = []
    import re as _re
    for m in _re.finditer(r'<d p="([^"]+)">([^<]*)</d>', text):
        attrs = m.group(1).split(",")
        try:
            t = float(attrs[0])
        except (ValueError, IndexError):
            t = 0.0
        out.append({"time": round(t, 2), "text": m.group(2)})
        if len(out) >= num:
            break
    return {"bvid": bvid, "cid": cid, "count": len(out), "danmakus": out,
            "note": "" if out else "该接口可能被风控或该分P无弹幕"}


# ─────────────────────────── 同步 ───────────────────────────


def video_brief(c: BiliClient, bvid: str) -> dict[str, Any]:
    """给「已选视频」用的轻量摘要（省一次 tag 请求）。"""
    v = get_video(c, bvid)
    return {
        "bvid": v["bvid"],
        "title": v["title"],
        "author": v["author"],
        "mid": v["author_mid"],
        "duration": v["duration"],
        "play": v["stat"]["view"],
        "like": v["stat"]["like"],
        "reply": v["stat"]["reply"],
        "url": f"https://www.bilibili.com/video/{v['bvid']}",
    }
