"""test_server_gates.py —— 发布闸门的回归测试（不联网、不碰 B站）

覆盖这次修的点：
  1. 写权限是「运行时状态」：撤销标记文件一旦存在，写工具立刻拒绝
  2. 风控（-412）会被持久化为 paused，且不再抛穿调用方
  3. 发前复核风格：破人设的正文不许发
  4. 同 UP 主 7 天去重按 owner_mid，覆盖所有发布类型
  5. 原子领取：日限 1 时，两个发布者只有一个能发出去
  6. verify_posted 先按时间排序找（不再只翻热度前 5 页）
  7. safety 的归一化与词表补漏

跑法：
    BILI_PLUS_HOME=<临时家目录> \
    <venv>/python.exe tests/test_server_gates.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# ── 隔离家目录（先用真实 python/ 铺一份，模拟 selfcheck 干的事） ──
_TMP = Path(tempfile.mkdtemp(prefix="bilisee-gates-"))
HOME = _TMP / "bili-see"
HOME.mkdir(parents=True, exist_ok=True)
SRC = Path(__file__).resolve().parent.parent / "python"
for f in sorted(SRC.glob("*.py")):
    (HOME / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
# 真实安装会把 config/persona.yaml 铺进家目录，风格闸（check_style）依赖它。
# 少了它 check_style 没有禁词表，风格闸等于失效 —— 这里照实铺一份。
_persona_src = Path(__file__).resolve().parent.parent / "config" / "persona.yaml"
(HOME / "persona.yaml").write_text(_persona_src.read_text(encoding="utf-8"), encoding="utf-8")
os.environ["BILI_PLUS_HOME"] = str(HOME)
os.environ["BILI_PLUS_WRITE"] = "1"
os.environ["BILI_PLUS_MAX_POSTS_PER_DAY"] = "3"
os.environ["BILI_PLUS_MIN_INTERVAL_MIN"] = "0"
sys.path.insert(0, str(HOME))

import api          # noqa: E402
import mcp_server   # noqa: E402
import safety       # noqa: E402
import wbi          # noqa: E402

PASS = 0
FAIL = 0
POSTS: list[dict] = []
LOCK = threading.Lock()


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  — {detail}")


VIDEO = {
    "bvid": "BV1gate000001",
    "aid": 2001,
    "cid": 3001,
    "title": "AI 工具实测：本地小模型怎么用",
    "author": "闸门UP",
    "author_mid": 7001,
    "duration": 600,
    "description": "中性技术视频简介",
    "stat": {"view": 50000},
    "pubdate": int(time.time()) - 3600,
}

_seq = 0


def use_video(bvid: str, mid: int) -> None:
    """每个用例用自己的视频/UP，避免互相污染去重状态。"""
    VIDEO.update({"bvid": bvid, "author_mid": mid, "aid": 2000 + mid})


def stub_network(delay: float = 0.0) -> None:
    def get_video(c, bvid):
        v = dict(VIDEO)
        v["bvid"] = bvid
        return v

    def get_tags(c, bvid):
        return ["科技"]

    def post_comment(c, bvid, text, *, root=0, parent=0, **kw):
        if delay:
            time.sleep(delay)
        with LOCK:
            POSTS.append({"bvid": bvid, "text": text})
            n = len(POSTS)
        return {"rpid": 322100000000 + n, "root": root, "parent": parent}

    api.get_video = get_video
    api.get_tags = get_tags
    api.post_comment = post_comment
    api.resolve_reply_target = lambda c, bvid, rpid: {
        "rpid": rpid, "is_sub": False, "root": 0, "parent": 0}


def fresh_db():
    POSTS.clear()          # 每个用例从干净的"网络调用记录"开始
    p = HOME / "bili_plus.db"
    for f in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")):
        try:
            f.unlink()
        except OSError:
            pass
    mcp_server._db = None
    return mcp_server.db()


GOOD = "这个思路挺省事的，我也去折腾一下看看效果"


# ── ① 写权限可撤销 ──────────────────────────────────────────────
def test_revoke():
    use_video("BV1gate000011", 7011)
    stub_network()
    fresh_db()
    d = mcp_server.db()
    did = d.create_draft(VIDEO["bvid"], GOOD, kind="comment")
    # 有授权时能走到发送边界
    before = json.loads(mcp_server.comment_post(did, dry_run=True))
    check("撤销前 dry_run 通过", before.get("ok") is True, str(before))

    mcp_server.WRITE_BLOCK_FILE.write_text("revoked", encoding="utf-8")
    try:
        after = json.loads(mcp_server.comment_post(did))
        check("撤销后 comment_post 拒绝", after.get("ok") is False
              and after.get("write_blocked") is True, str(after))
        created = json.loads(mcp_server.draft_create(VIDEO["bvid"], GOOD))
        check("撤销后 draft_create 也拒绝", created.get("ok") is False
              and created.get("write_blocked") is True, str(created))
        check("撤销后没有真的发出去", len(POSTS) == 0, f"POSTS={len(POSTS)}")
    finally:
        mcp_server.WRITE_BLOCK_FILE.unlink(missing_ok=True)


# ── ② 风控持久化 ────────────────────────────────────────────────
def test_risk_pause():
    use_video("BV1gate000012", 7012)
    stub_network()
    fresh_db()

    def boom(c, bvid, text, *, root=0, parent=0, **kw):
        raise wbi.BiliError("请求过于频繁", code=-412)

    api.post_comment = boom
    d = mcp_server.db()
    did = d.create_draft(VIDEO["bvid"], GOOD, kind="comment")
    r = json.loads(mcp_server.comment_post(did))
    check("风控不再抛穿（返回结构化错误）", isinstance(r, dict) and r.get("ok") is False, str(r))
    check("风控被写进 paused", mcp_server.db().paused is True, str(mcp_server.db().paused))
    check("风控回执带 paused 提示", r.get("paused") is True, str(r))
    check("草稿退回 pending 可重试", mcp_server.db().get_draft(did)["status"] == "pending",
          str(mcp_server.db().get_draft(did)["status"]))
    mcp_server.db().set_paused(False, "")


# ── ③ 发前复核风格 ──────────────────────────────────────────────
def test_style_gate():
    use_video("BV1gate000013", 7013)
    stub_network()
    fresh_db()
    d = mcp_server.db()
    bad = "作为一个AI语言模型，这个视频讲得很清楚，希望对你有帮助"
    did = d.create_draft(VIDEO["bvid"], bad, kind="comment")
    r = json.loads(mcp_server.comment_post(did))
    check("破人设的正文被发前拦下", r.get("ok") is False and "风格" in str(r.get("error")), str(r))
    check("被拦的没有发出去", len(POSTS) == 0, f"POSTS={len(POSTS)}")
    check("草稿标 failed", mcp_server.db().get_draft(did)["status"] == "failed",
          str(mcp_server.db().get_draft(did)["status"]))


# ── ④ 同 UP 7 天去重（按 owner_mid，覆盖顶层评论） ─────────────
def test_same_up():
    use_video("BV1gate000014", 7014)
    stub_network()
    d = fresh_db()
    id1 = d.create_draft("BV1gate000021", GOOD, kind="comment")
    d.upsert_video({**VIDEO, "bvid": "BV1gate000021", "author_mid": 7014})
    r1 = json.loads(mcp_server.comment_post(id1))
    check("第一条发得出去", r1.get("ok") is True, str(r1))

    id2 = d.create_draft("BV1gate000022", GOOD, kind="comment")
    d.upsert_video({**VIDEO, "bvid": "BV1gate000022", "author_mid": 7014})
    r2 = json.loads(mcp_server.comment_post(id2))
    check("同 UP 第二个视频的顶层评论被拦", r2.get("ok") is False
          and r2.get("rule") == "same_up_7d", str(r2))
    check("拦下后网络只被调过 1 次", len(POSTS) == 1, f"POSTS={len(POSTS)}")


# ── ⑤ 原子领取：日限 1，两个发布者只发一条 ─────────────────────
def test_atomic_claim():
    use_video("BV1gate000015", 7015)
    stub_network(delay=0.25)
    fresh_db()
    d0 = mcp_server.db()
    # 前面几个用例已经发过几条：把上限设成「已发 + 1」，等价于"只准再发一条"
    used = d0.count_posts_since(24)
    mcp_server.MAX_POSTS_PER_DAY = used + 1
    did = d0.create_draft(VIDEO["bvid"], GOOD, kind="comment")
    d0.conn.commit()

    local = threading.local()

    def per_thread_db():
        if getattr(local, "db", None) is None:
            local.db = mcp_server.DB(str(HOME / "bili_plus.db"))
        return local.db

    mcp_server.db = per_thread_db
    results = []

    def worker():
        results.append(json.loads(mcp_server.comment_post(did)))

    th = [threading.Thread(target=worker) for _ in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()

    ok_count = sum(1 for r in results if r.get("ok"))
    check("并发下只有一条成功", ok_count == 1, f"{ok_count} / {results}")
    check("网络只被调用 1 次", len(POSTS) == 1, f"POSTS={len(POSTS)}")
    conn = mcp_server.DB(str(HOME / "bili_plus.db"))
    posted = conn.conn.execute(
        "SELECT COUNT(*) c FROM drafts WHERE status='posted' AND bvid=?",
        ("BV1gate000015",)).fetchone()["c"]
    check("账本只有 1 条 posted", posted == 1, str(posted))
    check("另一条给出明确原因（额度/占位/同 UP 去重都算合理拦截）",
          any(("上限" in str(r.get("error"))
               or r.get("reason") == "already_claimed"
               or r.get("rule") == "same_up_7d")
              for r in results if not r.get("ok")), str(results))
    mcp_server.MAX_POSTS_PER_DAY = 3
    mcp_server.db = lambda: mcp_server.DB(str(HOME / "bili_plus.db"))


# ── ⑥ verify_posted 按时间查 ────────────────────────────────────
def test_verify_mode():
    use_video("BV1gate000016", 7016)
    stub_network()
    d = fresh_db()
    did = d.create_draft(VIDEO["bvid"], GOOD, kind="comment")
    r = json.loads(mcp_server.comment_post(did))
    rpid = int(r.get("rpid"))
    d.mark_posted(did, {"rpid": rpid})

    calls: list[str] = []

    class FakeClient:
        def ensure_buvid3(self):
            return "buvid"

        def get(self, path, params=None, **kw):
            calls.append(path)
            p = dict(params or {})
            if path == "/x/v2/reply/reply" and int(p.get("root") or 0) == rpid:
                # 精确查：data.root 就是这条评论本身
                return {"code": 0, "data": {"root": {
                    "rpid": rpid, "member": {"uname": "闸门UP"},
                    "content": {"message": GOOD}, "like": 0, "ctime": 1,
                }, "replies": []}}
            if path == "/x/v2/reply/reply":
                return {"code": 0, "data": {"root": None, "replies": []}}
            if path == "/x/v2/reply/wbi/main":
                return {"code": 0, "data": {"replies": [], "cursor": {"is_end": True}}}
            return {"code": 0, "data": {"aid": 2001, "bvid": VIDEO["bvid"],
                                        "owner": {"mid": 7016}}}

    orig_client = mcp_server.client
    mcp_server.client = lambda **kw: FakeClient()
    try:
        out = json.loads(mcp_server.verify_posted(did))
    finally:
        mcp_server.client = orig_client
    check("核验优先按 rpid 精确查（reply/reply）",
          calls and calls[0] == "/x/v2/reply/reply", str(calls[:3]))
    check("精确查能定位到自己的评论", out.get("status") == "found", str(out))
    check("回执里带正文和作者", out.get("text") == GOOD and out.get("author") == "闸门UP", str(out))


# ── ⑦ safety 归一化与词表 ───────────────────────────────────────
def test_safety():
    cases = {
        "动画演出让人破防，这个学生会主席真可爱": safety.ALLOW,
        "我去年亲手做过这个实验，结果真的很稳定": safety.BLOCK,
        "这段动画真好看，傻\u200b逼才看不懂": safety.BLOCK,
        "来 example.org 看更多有趣的动画吧": safety.BLOCK,
    }
    for text, want in cases.items():
        got = safety.check_text(text).action
        check(f"safety「{text[:12]}…」→ {want}", got == want, f"got={got}")
    check("基金会开源模型不再被误杀",
          safety.check_video({"title": "基金会开源模型实测"}).action == safety.ALLOW,
          safety.check_video({"title": "基金会开源模型实测"}).action)
    check("真·金融视频仍然拦",
          safety.check_video({"title": "基金定投入门"}).action == safety.NO_REPLY,
          safety.check_video({"title": "基金定投入门"}).action)


if __name__ == "__main__":
    test_revoke()
    test_risk_pause()
    test_style_gate()
    test_same_up()
    test_atomic_claim()
    test_verify_mode()
    test_safety()
    print(f"\nPASS {PASS} / FAIL {FAIL}")
    sys.exit(1 if FAIL else 0)
