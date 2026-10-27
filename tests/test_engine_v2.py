"""引擎侧改动的最小验证（不联网、不碰 B站、不跑模型）。

覆盖：
  1. 配置解析：三块配额 / other_prefs 行解析 / 配额溢出标记
  2. 质量门槛：播放量、粉丝数、关注 UP 豁免、粉丝查不到时的降级
  3. 三块配额装配 + 剩余额度自主浏览
  4. 每日浏览额度：跨轮累计、跨天归零
  5. 身份口径：评论文本里出现 qwen/4B 等必须被拦
  6. 视频总结：本地模型不可用时的规则兜底（不编造）

跑法：
    cd python/autopilot && PYTHONPATH=. <venv>/python.exe ../../tests/test_engine_v2.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bilisee-test-")
os.environ["BILI_AUTOPILOT_HOME"] = str(Path(TMP) / "auto-home")
os.environ["BILI_PLUS_HOME"] = str(Path(TMP) / "plus-home")
Path(os.environ["BILI_AUTOPILOT_HOME"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["BILI_PLUS_HOME"]).mkdir(parents=True, exist_ok=True)
os.environ.pop("BILI_AUTOPILOT_CONFIG_FILE", None)
# 这些用例测的是「已经授权写入」之后的链路（note → 草稿 → 发 → 核验）。
# 写权限现在要求**当前**环境也授权（防止用旧快照续跑时把权限带回来），
# 所以这里显式模拟宿主已授权；撤销授权的行为由 test_write_revoked 单独覆盖。
os.environ["BILI_AUTOPILOT_ALLOW_WRITE"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python" / "autopilot"))

from bili_autopilot import budget  # noqa: E402
from bili_autopilot.agent import identity_violation  # noqa: E402
from bili_autopilot.config import default_config, make_run_config, parse_other_prefs  # noqa: E402
from bili_autopilot.scout import bucket_specs, is_fresh, scout  # noqa: E402
from bili_autopilot import summary as summary_mod  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}   {detail}")


# ─────────────────────── 1. 配置 ───────────────────────

s = """
AI 编程 | Cursor,Copilot | 2
  数码  | 手机,相机 | 1

坏行没有竖线
"""
prefs = parse_other_prefs(s)
check("other_prefs 解析出 2 条", len(prefs) == 2, str(prefs))
check("other_prefs 名称/关键词/配额正确",
      prefs[0] == {"name": "AI 编程", "keywords": ["Cursor", "Copilot"], "quota": 2}, str(prefs[0]))
check("坏行被跳过而不是抛错", all(p.get("name") for p in prefs))

cfg = make_run_config({
    "daily_video_max": 4,
    "ai_tech_quota": 3,
    "ai_fun_quota": 2,
    "other_prefs": "数码 | 手机 | 2",
})
check("配额溢出被标记", cfg["quota_overflow"] is True, str(cfg["quota_wanted"]))
check("溢出时也保留用户填的值",
      (cfg["ai_tech_quota"], cfg["ai_fun_quota"], cfg["quota_wanted"]) == (3, 2, 7),
      str((cfg["ai_tech_quota"], cfg["ai_fun_quota"], cfg["quota_wanted"])))
check("单块配额被夹到总上限内", cfg["ai_tech_quota"] <= cfg["daily_video_max"])

cfg2 = make_run_config({"daily_video_max": 6, "ai_tech_quota": 2, "ai_fun_quota": 2})
check("没溢出时不报警", cfg2["quota_overflow"] is False)

check("质量门槛默认 1 万", (cfg["min_up_fans"], cfg["min_play"]) == (10000, 10000))
check("summary 默认开", cfg["summary_enabled"] is True)

specs = bucket_specs(cfg2)
check("bucket_specs 顺序 = AI技术 > AI二创", [x["name"] for x in specs][:2] == ["AI技术", "AI二创"], str(specs))

# ─────────────────────── 2/3. 门槛与装配 ───────────────────────


class FakeBridge:
    def __init__(self, fans: dict[int, int | None]):
        self.fans = fans
        self.calls: list[str] = []

    async def call(self, name: str, args: dict) -> dict:
        self.calls.append(name)
        if name == "status":
            return {"ok": True, "logged_in": True, "uname": "测试号", "quota_left": 5}
        if name == "user_info":
            mid = int(args["mid"])
            fans = self.fans.get(mid)
            if fans is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "mid": mid, "name": f"UP{mid}", "fans": fans}
        if name == "user_videos":
            return {"ok": True, "videos": [
                {"bvid": "BVwatch00001", "title": "关注UP的新视频", "play": 10,
                 "created": int(__import__("time").time()) - 3600, "length": "05:00"},
            ]}
        if name == "search":
            kw = str(args.get("keyword"))
            n = self.seq = getattr(self, "seq", 0) + 1
            now = int(__import__("time").time())
            tag = f"{n:02d}".rjust(2, "0")
            return {"ok": True, "videos": [
                {"bvid": f"BVsrch{tag}0001", "title": f"{kw} 实测", "author": f"UP-{kw}",
                 "author_mid": 2001, "play": 500000, "pubdate": now - 7200},
                {"bvid": f"BVsrch{tag}0002", "title": f"{kw} 小号视频", "author": "小UP",
                 "author_mid": 2002, "play": 800, "pubdate": now - 7200},
                {"bvid": f"BVsrch{tag}0003", "title": f"{kw} 大UP低播放", "author": "大UP",
                 "author_mid": 2003, "play": 900, "pubdate": now - 7200},
            ]}
        if name == "popular":
            now = int(__import__("time").time())
            return {"ok": True, "videos": [
                {"bvid": "BVpop0000001", "title": "钓鱼佬的日常", "author": "UPpop",
                 "author_mid": 2001, "play": 900000, "pubdate": now - 3600},
                {"bvid": "BVpop0000002", "title": "AI 大模型又赢了", "author": "UPpop2",
                 "author_mid": 2001, "play": 800000, "pubdate": now - 3600},
            ]}
        return {"ok": False, "error": f"unknown tool {name}"}


class FakeMemory:
    def __init__(self):
        self.data = {"candidates": [], "bucket_plan": {}, "bili_status": {}}

    def set_bili_status(self, status):
        self.data["bili_status"] = status

    def set_candidates(self, rows):
        self.data["candidates"] = rows

    def set_bucket_plan(self, plan):
        self.data["bucket_plan"] = plan


async def test_scout() -> None:
    budget.note_viewed([])  # 确保今天从零开始
    cfg = make_run_config({
        "daily_video_max": 5,
        "ai_tech_quota": 2,
        "ai_fun_quota": 0,
        "ai_tech_keywords": ["AI 大模型", "开源模型"],
        "ai_fun_keywords": ["AI 绘画"],
        "watch_mids": [1001],
        "fresh_days": 2,
        "min_up_fans": 10000,
        "min_play": 10000,
        "candidate_limit": 12,
    })
    # 1001 是关注 UP（粉丝很少也要放行）；2001 粉丝 5 万；2002 粉丝 300；2003 粉丝 20 万
    bridge = FakeBridge({1001: 50, 2001: 50000, 2002: 300, 2003: 200000})
    mem = FakeMemory()
    rows, _ = await scout(bridge, cfg, mem)

    bvids = [r["bvid"] for r in rows]
    check("关注 UP 的低播放视频被放行", "BVwatch00001" in bvids, str(bvids))
    check("播放不足的被丢掉", not any("大UP低播放" in (r.get("title") or "") for r in rows), str(bvids))
    check("粉丝不足的被丢掉", not any("小号" in (r.get("title") or "") for r in rows), str(bvids))
    check("高播放高粉丝的进候选", any(r.get("play") == 500000 for r in rows), str(bvids))

    plan = mem.data["bucket_plan"]
    check("记录了一天上限", plan.get("daily_max") == 5, str(plan))
    check("记录了门槛", (plan.get("gate") or {}).get("min_up_fans") == 10000, str(plan.get("gate")))
    check("AI二创配额为 0 时不出现该板块",
          all("AI二创" != b.get("name") for b in plan.get("buckets") or []), str(plan.get("buckets")))

    # 剩余额度：daily=5，AI技术给 2，关注 UP 占 1 ⇒ 还剩 2 条给自主浏览
    explore = plan.get("explore") or {}
    check("剩余配额转成自主浏览", explore.get("quota", 0) >= 1, str(explore))
    check("自主浏览优先非 AI 标题",
          any("钓鱼" in (r.get("title") or "") for r in rows), str([r.get("title") for r in rows]))

    # 额度用尽后：再跑一次直接返回空
    budget.note_viewed([r["bvid"] for r in rows])
    full = len(budget.load()["viewed"])
    mem2 = FakeMemory()
    rows2, _ = await scout(bridge, make_run_config({**cfg, "daily_video_max": full}), mem2)
    check("额度用尽后不再返回候选", rows2 == [], str(rows2))
    check("额度用尽时不烧模型调用", len(bridge.calls) < 40, str(len(bridge.calls)))


# ─────────────────────── 4. 每日额度 ───────────────────────

def test_budget() -> None:
    data = budget.load()
    budget.note_viewed(["BVx1", "BVx2", "BVx1"])
    data = budget.load()
    check("同一条视频不重复计数", data["viewed"].count("BVx1") == 1, str(data["viewed"]))
    check("remaining 递减", budget.remaining(100) == 100 - len(data["viewed"]), str(budget.summary(100)))
    # 伪造昨天的账，应自动归零
    d = budget.load()
    d["date"] = "2020-01-01"
    budget.save(d)
    check("跨天自动归零", budget.load()["viewed"] == [], str(budget.load()["viewed"]))
    check("归零后 remaining 恢复", budget.remaining(6) == 6)


# ─────────────────────── 5. 身份口径 ───────────────────────

def test_identity() -> None:
    for bad in ("我是 qwen 4B", "通义千问路过", "本地小模型报道", "参数量 4b 而已"):
        check(f"拦住身份泄露：{bad}", identity_violation(bad) != "", bad)
    for good in ("我是大肥鱼呀", "鲸鱼娘来啦", "deepseek 最爱吃 token"):
        check(f"放行正常自称：{good}", identity_violation(good) == "", good)


# ─────────────────── 5.5 「关注的 UP」不能被写死 ───────────────────

def test_watch_mids() -> None:
    """回归：清空「关注的 UP」必须真的清空。

    这里的环境是隔离的（BILI_PLUS_HOME 指向临时目录、没有 config.json），
    以前这种情况下 default_config() 会带出三个内置 UID；而且 normalize 里
    写着 `[...] or cfg[key]`，于是显式传空也会被顶回默认 —— 两条都要断住。
    """
    base = default_config()
    check("没有配置时不自带关注名单", base["watch_mids"] == [], str(base["watch_mids"]))

    cleared = make_run_config({"watch_mids": []})["watch_mids"]
    check("显式清空生效（不被顶回默认）", cleared == [], str(cleared))

    cleared_csv = make_run_config({"watch_mids": ""})["watch_mids"]
    check("传空串也清空", cleared_csv == [], str(cleared_csv))

    # normalize_mid 会把 UID 归一成整数，所以按字符串比，别绑死类型
    filled = [str(x) for x in make_run_config({"watch_mids": "123, 456"})["watch_mids"]]
    check("填了就按填的来", filled == ["123", "456"], str(filled))

    # 同一类静默回落，关键词也要能清空
    kw = make_run_config({"ai_tech_keywords": []})["ai_tech_keywords"]
    check("关键词也能清空", kw == [], str(kw))


# ─────────────────── 5.6 「只看最近几天」的 0 要真的不限 ───────────────────

def test_fresh_days() -> None:
    """回归：设置页写着「0 表示不限」，实现就必须是不限。

    改之前 0 会被抬成 2（default_config 里 <=0→2，normalize 里 max(1, …)），
    页面说一套、跑起来另一套。
    """
    check("fresh_days: 0 不被抬成 2", make_run_config({"fresh_days": 0})["fresh_days"] == 0,
          str(make_run_config({"fresh_days": 0})["fresh_days"]))
    check("fresh_days: 上限仍为 14", make_run_config({"fresh_days": 99})["fresh_days"] == 14,
          str(make_run_config({"fresh_days": 99})["fresh_days"]))
    check("fresh_days: 没填时仍是 2", make_run_config({})["fresh_days"] == 2,
          str(make_run_config({})["fresh_days"]))

    now = int(time.time())
    old = now - 300 * 86400          # 300 天前
    check("0 = 不限新旧（老视频放行）", is_fresh(old, 0, now) is True, str(is_fresh(old, 0, now)))
    check("2 天：老视频被挡", is_fresh(old, 2 * 86400, now) is False, str(is_fresh(old, 2 * 86400, now)))
    check("2 天：当天放行", is_fresh(now - 3600, 2 * 86400, now) is True, str(is_fresh(now - 3600, 2 * 86400, now)))
    check("拿不到发布时间时放行", is_fresh(None, 2 * 86400, now) is True, "None")


# ─────────────────────── 6. 视频总结兜底 ───────────────────────
def test_summary() -> None:
    class NoModel:
        def chat(self, *a, **kw):
            raise RuntimeError("ollama down")

    class Mem:
        def __init__(self):
            self.data = {
                "videos": [{
                    "bvid": "BV1xx411c7mD", "title": "测试视频", "author": "UP甲",
                    "play": 23456, "comment_action": "comment",
                    "comment_text": "这条我真的很喜欢", "comment_status": "posted",
                    "summary": "讲了一个 demo",
                }],
                "bucket_plan": {"daily_max": 6, "remaining": 6, "buckets": []},
            }
            self.saved = {}

        def add_usage(self, *a):
            pass

        def set_summary(self, value):
            self.saved = value

    mem = Mem()
    out = summary_mod.build(mem, NoModel(), make_run_config({"daily_video_max": 6}))
    check("模型挂了也能出总结（规则兜底）", bool(out.get("text")), str(out.get("text"))[:80])
    check("兜底被标记 degraded", out.get("degraded") is True)
    check("兜底里带 BV 号", "BV1xx411c7mD" in out.get("text", ""))
    check("兜底里带播放量（万）", "2.3万" in out.get("text", ""), out.get("text"))
    check("兜底里带评论原文", "这条我真的很喜欢" in out.get("text", ""))
    check("记录了 raw_chars 用于算节省", out.get("raw_chars", 0) > 0, str(out.get("raw_chars")))
    check("总结被写回记忆", bool(mem.saved.get("text")))

    # 发了评论但总结漏写 → 用记忆里的事实补上
    class Mem2(Mem):
        def __init__(self):
            self.data = {
                "videos": [{
                    "bvid": "BV1xx411c7mD", "title": "测试视频", "author": "UP甲",
                    "play": 23456, "comment_action": "comment",
                    "comment_text": "这条我真的很喜欢", "comment_status": "posted",
                    "post_result": {"rpid": "123"}, "summary": "讲了一个 demo",
                }],
                "bucket_plan": {"daily_max": 6, "remaining": 6, "buckets": []},
            }
            self.saved = {}

    class WrongModel:
        def chat(self, *a, **kw):
            class R:
                content = "视频：BV1xx411c7mD 讲了一个 demo。\n\n评论：本轮未评论。\n\n推荐：暂无。"
                thinking = ""
                prompt_eval_count = 100
                eval_count = 50
            return R()

    m2 = Mem2()
    out2 = summary_mod.build(m2, WrongModel(), make_run_config({"daily_video_max": 6}))
    check("本轮发了评论，总结写「未评论」会被纠正",
          "这条我真的很喜欢" in out2.get("text", "") and "本轮未评论" not in out2.get("text", ""),
          out2.get("text"))
    check("纠正过会标 repaired", out2.get("repaired") is True, str(out2.get("repaired")))
    check("纠正后带 rpid", "123" in out2.get("text", ""), out2.get("text"))

    # 场景 B：本轮没发评论，但模型把上一轮的评论抄进来了 → 必须改成"未评论"
    class StaleModel:
        def chat(self, *a, **kw):
            class R:
                content = "视频：BV1xx411c7mD 讲了一个 demo。\n\n评论：AI 生模真香！（这是上一轮的）\n\n推荐：暂无。"
                thinking = ""
                prompt_eval_count = 100
                eval_count = 50
            return R()

    class Mem3(Mem):
        def __init__(self):
            self.data = {
                "videos": [{"bvid": "BV1xx411c7mD", "title": "测试视频", "play": 23456,
                            "comment_action": "inspected", "comment_text": "", "comment_status": "none"}],
                "bucket_plan": {"daily_max": 6, "remaining": 6, "buckets": []},
            }
            self.saved = {}

    m3 = Mem3()
    out3 = summary_mod.build(m3, StaleModel(), make_run_config({"daily_video_max": 6}))
    check("本轮没评论时不会被上一轮评论串台",
          "上一轮的" not in out3.get("text", "") and "本轮未评论" in out3.get("text", ""),
          out3.get("text"))
    check("串台被纠正也标 repaired", out3.get("repaired") is True, str(out3.get("repaired")))



# ─────────────────────── 7. 写评论链路（无预演） ───────────────────────

async def test_comment_flow() -> None:
    """真发链路：note → draft_create → comment_post(不带 dry_run) → 核验。

    这里不是"跑着看看"，是把 dry_run 删干净之后必须仍然成立的契约：
    comment_post 只要一个 draft_id 就该发出去。
    """
    from bili_autopilot.agent import Agent
    from bili_autopilot.config import paths_for
    from bili_autopilot.memory import MemoryStore
    from bili_autopilot.state import StateStore

    class Bridge:
        def __init__(self):
            self.posted: list[dict] = []

        def model_tool_specs(self):
            return []

        async def call(self, name, args):
            if name == "safety_check":
                return {"ok": True, "text": {"action": "allow"}, "video": {"action": "allow"}}
            if name == "style_check":
                return {"ok": True, "issues": []}
            if name == "draft_create":
                return {"ok": True, "draft_id": 77}
            if name == "comment_post":
                self.posted.append(dict(args))
                return {"ok": True, "rpid": "321000000001", "status": "posted"}
            if name == "verify_posted":
                return {"ok": True, "status": "found"}
            return {"ok": False, "error": f"unexpected {name}"}

    class NoModel:
        def chat(self, *a, **kw):
            raise AssertionError("写评论链路不该调模型")

    run_id = "test-comment-flow"
    paths = paths_for(run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    cfg = make_run_config({"write": True, "max_comments": 1})
    store = StateStore(paths)
    store.create(run_id, cfg)
    memory = MemoryStore(paths, cfg)
    memory.reset()

    bridge = Bridge()
    cand = {"bvid": "BV1xx411c7mD", "title": "测试视频", "author": "UP甲",
            "play": 50000, "fans": 50000, "bucket": "AI技术"}
    agent = Agent(bridge=bridge, ollama=NoModel(), memory=memory, config=cfg,
                  state=store, paths=paths, candidates=[cand],
                  bili_status={"logged_in": True, "quota_left": 3}, seen_bvids=set())

    agent._cache_tool_result("subtitle", cand["bvid"], {
        "text": "UP主演示用工具从一张图生成三维角色模型并绑定骨骼，导出后可放进游戏里驱动动画。",
    })
    good = "建模师看完这条估计想转行了吧，我这只鲸鱼还在啃白米饭呢"
    note = await agent._note_video({"bvid": cand["bvid"], "summary": "讲了开源模型实测",
                                    "action": "comment", "comment_text": good})
    check("note_video(comment) 通过", note.get("ok") is True, str(note))

    draft = await agent._draft_create({"bvid": cand["bvid"], "text": good})
    check("draft_create 拿到 draft_id", int(draft.get("draft_id") or 0) == 77, str(draft))

    sent = await agent._comment_post({"draft_id": 77})
    check("comment_post 不带 dry_run 也能发", sent.get("ok") is True, str(sent))
    check("发送参数里没有 dry_run", all("dry_run" not in a for a in bridge.posted), str(bridge.posted))
    check("发完自动核验", (sent.get("verify") or {}).get("status") == "found", str(sent.get("verify")))
    row = memory.get_video(cand["bvid"]) or {}
    check("记忆里标成 posted", row.get("comment_status") == "posted", str(row.get("comment_status")))
    check("记忆里带 rpid", bool((row.get("post_result") or {}).get("rpid")), str(row.get("post_result")))
    check("记忆里记了播放/粉丝（总结要用）",
          row.get("play") == 50000 and row.get("fans") == 50000, f"{row.get('play')}/{row.get('fans')}")

    # 额度口径：读了字幕就算"看过"，同一视频不重复计数
    before = len(budget.load()["viewed"])
    agent._cache_tool_result("subtitle", cand["bvid"], {"text": "字幕正文"})
    after = budget.load()["viewed"]
    check("读字幕算看过、记进当天额度", len(after) > 0, str(after))
    check("读字幕最多新增 1 条额度", 0 <= len(after) - before <= 1, f"{before}->{len(after)}")
    check("note + 字幕不对同一条重复计数",
          after.count(cand["bvid"]) == 1, str(after))
    check("候选元数据（播放/粉丝/板块）落进记忆",
          (memory.get_video(cand["bvid"]) or {}).get("play") == 50000, str(memory.get_video(cand["bvid"])))
    check("videos_seen 按看过内容计数", agent._viewed_count() >= 1, str(agent._viewed_count()))

    # 看不够不许提前收工
    agent.finish_rejections = 0
    agent.config["max_videos"] = 3
    agent.candidates.append({"bvid": "BV1yy411c7mE", "title": "另一条", "play": 50000, "fans": 50000})
    early = agent._finish({"summary": "看完了"})
    check("没看满就 finish 会被拒", early.get("ok") is False and "还没看满" in str(early.get("error")), str(early))
    agent.finish_rejections = 2
    ok_finish = agent._finish({"summary": "确实看完了"})
    check("坚持两次后允许收工（不死循环）", ok_finish.get("ok") is True, str(ok_finish))

    # 身份口径：评论里自称 qwen 必须被拦
    bad = await agent._note_video({"bvid": cand["bvid"], "summary": "x", "action": "comment",
                                   "comment_text": "我是 qwen 4B 本地小模型，这条视频真好看"})
    check("评论自曝 qwen 被拦", bad.get("ok") is False and "qwen" in str(bad.get("error", "")).lower(), str(bad))


def test_comment_gates() -> None:
    """评论三道闸：没依据不许评、AI 腔不许、复读不许。"""
    from bili_autopilot.agent import Agent

    class Dummy:
        pass

    agent = Agent.__new__(Agent)          # 只测纯函数，不跑 __init__
    row = {
        "title": "震惊瘫坐！真的可以一张图直接生成游戏用角色了？！",
        "summary": "UP主用工具一张图生成3D角色并绑定骨骼导出VRM。",
        "subtitle_excerpt": "一张图直接生成游戏用角色，还能自动绑定骨骼导出。",
        "keyframes": ["/tmp/a.png"],
    }

    # ① 没字幕没画面 → 不许评
    empty_row = {"title": "只有标题", "summary": "", "subtitle_excerpt": "", "keyframes": []}
    msg = agent._comment_problem("这条真的好厉害我也想试试看", empty_row)
    check("没有字幕/画面时不许评论", "字幕或关键帧" in msg, msg)

    # ② AI 腔
    msg = agent._comment_problem("总的来说这个技术很有前景，希望对你有所帮助", row)
    check("AI 腔评论被拦", "AI 腔" in msg, msg)

    # ③ 复读
    msg = agent._comment_problem("一张图直接生成游戏用角色真的很震撼", row)
    check("复读视频内容被拦", "复读" in msg, msg)

    # ④ 正常的人设口吻
    msg = agent._comment_problem("建模师看完这条估计想转行了吧，我这只鲸鱼还在啃白米饭呢", row)
    check("正常人设评论放行", msg == "", msg)

    # ⑤ 只有关键帧也算有依据
    only_frames = {"title": "t", "summary": "", "subtitle_excerpt": "", "keyframes": ["/tmp/b.png"]}
    msg = agent._comment_problem("画面里那个模型笑死我了，这是要给游戏塞什么鬼东西", only_frames)
    check("只有关键帧也能作为评论依据", msg == "", msg)


def test_entertainment() -> None:
    from bili_autopilot.agent import is_entertainment

    check("娱乐类识别：二创", is_entertainment("AI二创", "赵本山 Dangerous MV") is True)
    check("娱乐类识别：整活", is_entertainment("", "AI 整活：某不存在的联欢晚会（笑）") is True)
    check("娱乐类识别：AI MV", is_entertainment("", "AI MV 大赛参赛作品", ["AI 绘画"]))
    check("科技视频不算娱乐", is_entertainment("AI技术", "腾讯开源世界模型 NorMous 实测") is False)
    check("空标题不误判", is_entertainment("", "") is False)


def test_at_least_one() -> None:
    """整轮 0 评论 + 看过 ≥3 条 → finish 被拒，提示从娱乐类补一条。"""
    from bili_autopilot.agent import Agent
    from bili_autopilot.config import paths_for
    from bili_autopilot.memory import MemoryStore
    from bili_autopilot.state import StateStore

    class Bridge:
        def model_tool_specs(self): return []
        async def call(self, name, args): return {"ok": True}

    class NoModel:
        def chat(self, *a, **kw): raise AssertionError("不该调模型")

    run_id = "test-at-least-one"
    paths = paths_for(run_id); paths.run_dir.mkdir(parents=True, exist_ok=True)
    cfg = make_run_config({"write": True, "max_comments": 1, "max_videos": 5})
    store = StateStore(paths); store.create(run_id, cfg)
    memory = MemoryStore(paths, cfg); memory.reset()

    # 预填 3 条看过的视频，其中一条是娱乐类
    memory.add_video("BV1aaaa00001", title="GPU 解锁补丁实测", comment_action="inspected",
                     bucket="AI技术", inspected=True)
    memory.add_video("BV1bbbb00002", title="一张图生成游戏角色", comment_action="inspected",
                     bucket="AI技术", inspected=True)
    memory.add_video("BV1cccc00003", title="AI MV 赵本山 Dangerous", comment_action="inspected",
                     bucket="AI二创", inspected=True)

    cands = [
        {"bvid": "BV1aaaa00001", "title": "GPU 解锁补丁实测", "bucket": "AI技术"},
        {"bvid": "BV1bbbb00002", "title": "一张图生成游戏角色", "bucket": "AI技术"},
        {"bvid": "BV1cccc00003", "title": "AI MV 赵本山 Dangerous", "bucket": "AI二创"},
    ]
    agent = Agent(bridge=Bridge(), ollama=NoModel(), memory=memory, config=cfg,
                  state=store, paths=paths, candidates=cands,
                  bili_status={"logged_in": True, "quota_left": 3}, seen_bvids=set())
    agent.finish_rejections = 0

    r = agent._finish({"summary": "看完了"})
    check("整轮 0 评论时 finish 被拒", r.get("ok") is False and "还没发评论" in str(r.get("error")), str(r))
    check("提示从娱乐类补评论", "BV1cccc00003" in str(r.get("error")), str(r))

    # 已经有评论了就不再拦
    agent.posted.add("BV1cccc00003")
    agent.finish_rejections = 0
    r2 = agent._finish({"summary": "看完了"})
    check("已有评论时 finish 放行", r2.get("ok") is True, str(r2))


# ─────────────────────── 写授权撤销 / 限额 / 熔断语义 ───────────────────────


async def test_write_revoked() -> None:
    """当前没有写授权时，就算 run 快照是 write=true 也不能写。"""
    from bili_autopilot.agent import Agent
    from bili_autopilot.config import paths_for
    from bili_autopilot.memory import MemoryStore
    from bili_autopilot.state import StateStore

    class Bridge:
        def model_tool_specs(self):
            return []

        async def call(self, name, args):
            if name == "status":
                return {"ok": True, "logged_in": True, "quota_left": 5}
            if name == "safety_check":
                return {"ok": True, "text": {"action": "allow"}, "video": {"action": "allow"}}
            if name == "style_check":
                return {"ok": True, "issues": []}
            return {"ok": True}

    class NoModel:
        def chat(self, *a, **kw):
            raise AssertionError("不该调模型")

    run_id = "test-write-revoked"
    paths = paths_for(run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    cfg = make_run_config({"write": True, "max_comments": 1, "max_videos": 1})
    store = StateStore(paths)
    store.create(run_id, cfg)
    memory = MemoryStore(paths, cfg)
    memory.reset()
    cand = {"bvid": "BV1rev0000001", "title": "测试视频", "author": "UP甲",
            "play": 50000, "fans": 50000, "bucket": "AI技术"}

    os.environ.pop("BILI_AUTOPILOT_ALLOW_WRITE", None)
    try:
        agent = Agent(bridge=Bridge(), ollama=NoModel(), memory=memory, config=cfg,
                      state=store, paths=paths, candidates=[cand],
                      bili_status={"logged_in": True, "quota_left": 5}, seen_bvids=set())
        check("没有当前授权 → write_allowed=False", agent.write_allowed is False,
              str(agent.write_allowed))
        agent._cache_tool_result("subtitle", cand["bvid"], {"text": "普通技术视频字幕内容。"})
        r = await agent._note_video({"bvid": cand["bvid"], "summary": "讲了工具用法",
                                     "action": "comment",
                                     "comment_text": "这个思路确实省事我也去折腾看看"})
        check("没有当前授权 → 不能记 comment", r.get("ok") is False, str(r))
        sent = await agent._comment_post({"draft_id": 1})
        check("没有当前授权 → 发不出去", sent.get("ok") is False, str(sent))
    finally:
        os.environ["BILI_AUTOPILOT_ALLOW_WRITE"] = "1"


async def test_caps_at_post() -> None:
    """max_comments 必须在**发送时**也数一遍，不能只在 note 时数。"""
    from bili_autopilot.agent import Agent
    from bili_autopilot.config import paths_for
    from bili_autopilot.memory import MemoryStore
    from bili_autopilot.state import StateStore

    class Bridge:
        def __init__(self):
            self.posted = []
            self._n = 200

        def model_tool_specs(self):
            return []

        async def call(self, name, args):
            if name == "status":
                return {"ok": True, "logged_in": True, "quota_left": 9}
            if name == "safety_check":
                return {"ok": True, "text": {"action": "allow"}, "video": {"action": "allow"}}
            if name == "style_check":
                return {"ok": True, "issues": []}
            if name == "draft_create":
                self._n += 1
                return {"ok": True, "draft_id": self._n}
            if name == "comment_post":
                self.posted.append(dict(args))
                return {"ok": True, "rpid": f"3220{len(self.posted):08d}"}
            if name == "verify_posted":
                return {"ok": True, "status": "found"}
            return {"ok": True}

    class NoModel:
        def chat(self, *a, **kw):
            raise AssertionError("不该调模型")

    run_id = "test-caps-at-post"
    paths = paths_for(run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    cfg = make_run_config({"write": True, "max_comments": 1, "max_videos": 2})
    store = StateStore(paths)
    store.create(run_id, cfg)
    memory = MemoryStore(paths, cfg)
    memory.reset()
    cands = [
        {"bvid": "BV1cap000001", "title": "测试一", "author": "UP甲", "play": 50000,
         "fans": 50000, "bucket": "AI技术"},
        {"bvid": "BV1cap000002", "title": "测试二", "author": "UP乙", "play": 60000,
         "fans": 60000, "bucket": "AI技术"},
    ]
    bridge = Bridge()
    agent = Agent(bridge=bridge, ollama=NoModel(), memory=memory, config=cfg,
                  state=store, paths=paths, candidates=cands,
                  bili_status={"logged_in": True, "quota_left": 9}, seen_bvids=set())
    good = "这个思路确实省事，我也去折腾一下看看效果"
    for c in cands:
        agent._cache_tool_result("subtitle", c["bvid"], {"text": "普通技术视频字幕内容。"})
        await agent._note_video({"bvid": c["bvid"], "summary": "讲了工具用法",
                                 "action": "comment", "comment_text": good})
    ids = []
    for c in cands:
        d = await agent._draft_create({"bvid": c["bvid"], "text": good})
        ids.append(d.get("draft_id"))
    r1 = await agent._comment_post({"draft_id": ids[0]})
    r2 = await agent._comment_post({"draft_id": ids[1]})
    check("第一条发得出去", r1.get("ok") is True, str(r1))
    check("第二条被本轮额度拦住", r2.get("ok") is False and "上限" in str(r2.get("error")), str(r2))
    check("实际只发出去 1 条", len(bridge.posted) == 1, str(bridge.posted))


def test_auto_finish_partial() -> None:
    """熔断收尾必须标 partial，不能报成正常完成。"""
    from bili_autopilot.agent import Agent
    from bili_autopilot.config import paths_for
    from bili_autopilot.memory import MemoryStore
    from bili_autopilot.state import StateStore

    class Dummy:
        def model_tool_specs(self):
            return []

        async def call(self, name, args):
            return {"ok": True}

    class NoModel:
        def chat(self, *a, **kw):
            raise AssertionError("不该调模型")

    run_id = "test-auto-finish"
    paths = paths_for(run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    cfg = make_run_config({"write": True, "max_comments": 1, "max_videos": 1})
    store = StateStore(paths)
    store.create(run_id, cfg)
    memory = MemoryStore(paths, cfg)
    memory.reset()
    agent = Agent(bridge=Dummy(), ollama=NoModel(), memory=memory, config=cfg,
                  state=store, paths=paths, candidates=[],
                  bili_status={"logged_in": True, "quota_left": 3}, seen_bvids=set())
    agent._auto_finish("同一个工具连续失败 5 次")
    check("熔断后 partial=True", memory.data.get("partial") is True, str(memory.data.get("partial")))
    check("熔断不报 completed", memory.data.get("status") != "completed",
          str(memory.data.get("status")))
    check("熔断原因写进 issues", "连续失败" in str(memory.data.get("finish", {}).get("issues")),
          str(memory.data.get("finish", {}).get("issues")))


async def main() -> int:
    await test_scout()
    await test_comment_flow()
    test_comment_gates()
    test_entertainment()
    test_at_least_one()
    test_budget()
    test_identity()
    test_watch_mids()
    test_fresh_days()
    test_summary()
    await test_write_revoked()
    await test_caps_at_post()
    test_auto_finish_partial()
    print()
    print(f"PASS {PASS} / FAIL {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
