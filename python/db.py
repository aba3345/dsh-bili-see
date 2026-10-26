"""db.py —— SQLite 持久层（5 表 + 审批 token 链）。

来自 bili-agent/db.py 的设计，但做了两处**重要修正**：

① **时间比较统一走 epoch 秒**（int），不再混用 ISO 字符串比较和 fromisoformat。
   原项目 `verify_approval` 用字符串比 `iso() > approval_expires`，
   而 `last_post_at()` 用 `datetime.fromisoformat(...)` —— 两条路径口径不同，
   目前恰好没 bug，但只要 isoformat 的 timespec 或时区表示一变就炸。
   这里全部用 `int(time.time())`，比较是纯整数运算，没有歧义。

② **审批 token 明文只出现一次**，库里只存 sha256。这是防「模型自己批准自己」的
   核心机制：模型能创建草稿，但拿不到 token，就无法跳过人工审批。

表：
    videos          —— 见过的视频
    comments_seen   —— 见过的评论（去重）
    drafts          —— 待发/已发评论（**审批 token 在这**）
    actions         —— 审计流水
    settings        —— 键值配置（paused 等）
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Mapping

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;

CREATE TABLE IF NOT EXISTS videos (
    bvid        TEXT PRIMARY KEY,
    aid         INTEGER,
    cid         INTEGER,
    title       TEXT,
    owner       TEXT,
    owner_mid   INTEGER,
    duration    INTEGER,
    description TEXT,
    tags        TEXT,
    stat        TEXT,
    url         TEXT,
    first_seen  INTEGER,
    last_seen   INTEGER
);

CREATE TABLE IF NOT EXISTS comments_seen (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid       TEXT NOT NULL,
    rpid       TEXT NOT NULL,
    author     TEXT,
    text       TEXT,
    ctime      INTEGER,
    fetched_at INTEGER,
    UNIQUE(bvid, rpid)
);

CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid             TEXT NOT NULL,
    text             TEXT NOT NULL,
    rationale        TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    kind             TEXT NOT NULL DEFAULT 'comment',
    root_rpid        TEXT,
    parent_rpid      TEXT,
    target_author    TEXT,
    created_at       INTEGER,
    approved_at      INTEGER,
    approval_hash    TEXT,
    approval_expires INTEGER,
    approval_consumed INTEGER,
    posted_at        INTEGER,
    result           TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    target     TEXT,
    payload    TEXT,
    result     TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS ix_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS ix_drafts_bvid   ON drafts(bvid);
CREATE INDEX IF NOT EXISTS ix_actions_kind  ON actions(kind, created_at);
"""

# 允许写进 actions.payload / result 的白名单键 —— 防止原始 API 回复几 KB 撑爆审计表
_SAFE_KEYS = {
    "ok", "api_code", "api_message", "rpid", "rpid_str", "root", "parent",
    "paused", "pause_reason", "dry_run", "error", "draft_id", "kind",
    "bvid", "aid", "text", "action", "risk_control", "need_captcha",
    # approve 用：只记"批了哪条、有效多久"，**绝不记 token 明文**
    "ttl_min",
}


def _now() -> int:
    return int(time.time())


def _compact(obj: Any, *, depth: int = 0) -> Any:
    """把任意结构压成「审计够用」的大小：白名单键 + 字符串截断 + 深度限制。"""
    if depth > 2:
        return "…"
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k not in _SAFE_KEYS:
                continue
            out[k] = _compact(v, depth=depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_compact(x, depth=depth + 1) for x in obj[:5]]
    if isinstance(obj, str):
        return obj[:120]
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return str(obj)[:120]


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('paused','0')")
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _migrate(self) -> None:
        """轻量迁移：老库缺列就补。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(drafts)")}
        for name, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'comment'"),
            ("root_rpid", "TEXT"),
            ("parent_rpid", "TEXT"),
            ("target_author", "TEXT"),
            ("approval_hash", "TEXT"),
            ("approval_expires", "INTEGER"),
            ("approval_consumed", "INTEGER"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE drafts ADD COLUMN {name} {ddl}")
        self.conn.commit()

    # ───────────────────── 视频 / 评论 ─────────────────────

    def upsert_video(self, v: Mapping[str, Any]) -> None:
        now = _now()
        tags = v.get("tags")
        stat = v.get("stat")
        self.conn.execute(
            """INSERT INTO videos(bvid,aid,cid,title,owner,owner_mid,duration,description,
                                  tags,stat,url,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bvid) DO UPDATE SET
                 aid=excluded.aid, cid=excluded.cid, title=excluded.title,
                 owner=excluded.owner, owner_mid=excluded.owner_mid,
                 duration=excluded.duration, description=excluded.description,
                 tags=excluded.tags, stat=excluded.stat, last_seen=excluded.last_seen""",
            (
                v.get("bvid"), v.get("aid"), v.get("cid"), v.get("title"),
                v.get("author") or v.get("owner"), v.get("author_mid") or v.get("owner_mid"),
                v.get("duration"), (v.get("description") or "")[:1000],
                json.dumps(tags, ensure_ascii=False) if tags is not None else None,
                json.dumps(stat, ensure_ascii=False) if stat is not None else None,
                v.get("url") or f"https://www.bilibili.com/video/{v.get('bvid')}",
                now, now,
            ),
        )
        self.conn.commit()

    def seen_video(self, bvid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM videos WHERE bvid=?", (bvid,)).fetchone() is not None

    def remember_comment(self, bvid: str, c: Mapping[str, Any]) -> bool:
        """记一条已见评论。返回 True 表示是新评论。"""
        rpid = str(c.get("rpid") or c.get("rpid_str") or "")
        if not rpid:
            return False
        try:
            self.conn.execute(
                """INSERT INTO comments_seen(bvid,rpid,author,text,ctime,fetched_at)
                   VALUES(?,?,?,?,?,?)""",
                (bvid, rpid, c.get("user"), (c.get("content") or "")[:500],
                 c.get("ctime"), _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # ───────────────────── 草稿 + 审批 ─────────────────────

    def create_draft(self, bvid: str, text: str, *, rationale: str = "",
                     kind: str = "comment", root_rpid: int | None = None,
                     parent_rpid: int | None = None,
                     target_author: str = "") -> int:
        cur = self.conn.execute(
            """INSERT INTO drafts(bvid,text,rationale,status,kind,root_rpid,parent_rpid,
                                  target_author,created_at)
               VALUES(?,?,?,'pending',?,?,?,?,?)""",
            (bvid, text, rationale, kind,
             str(root_rpid) if root_rpid else None,
             str(parent_rpid) if parent_rpid else None,
             target_author or None, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(r) if r else None

    def update_draft_text(self, draft_id: int, text: str, *,
                          rationale: str | None = None) -> bool:
        """改一条还没发出去的草稿正文。

        ⚠️ 只允许 pending / approved(旧数据)。posted / failed / rejected 一律不动 ——
           posted 的改了会让人以为 B站 上那条也跟着变了（实际不会），
           这是「界面显示和线上实际不一致」里最难查的一类。

        审批已下线（2026-10-22）：这里不再作废 token（没有 token 了），
        但**仍然把旧数据里的 approval_* 字段清空**，免得重构后残留的旧签名
        在将来重新启用审批时被误认为有效。
        """
        cur = self.conn.execute(
            """UPDATE drafts SET text=?, rationale=COALESCE(?, rationale),
                                 status='pending', approved_at=NULL,
                                 approval_hash=NULL, approval_expires=NULL
               WHERE id=? AND status IN ('pending','approved')""",
            (text, rationale, draft_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_drafts(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM drafts WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, min(int(limit), 100)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (min(int(limit), 100),)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 审批功能已于 2026-10-22 下线（主人要求重构，暂时下线） ──
    # approve_draft / verify_approval / consume_approval 三个方法已删除。
    # 建表的 approval_* 列**保留不动**：SQLite 里删列要重建整张表，而历史草稿
    # （含已 posted 的那几条）还有审计价值，不值得为清几列冒数据损坏的风险。
    # 这些列现在已经没人读、没人写；将来重构审批时可以直接复用。

    def mark_posted(self, draft_id: int, result: Mapping[str, Any]) -> None:
        self.conn.execute(
            "UPDATE drafts SET status='posted', posted_at=?, result=? WHERE id=?",
            (_now(), json.dumps(_compact(result), ensure_ascii=False), draft_id),
        )
        self.conn.commit()

    def mark_failed(self, draft_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE drafts SET status='failed', result=? WHERE id=?",
            (json.dumps({"error": str(reason)[:200]}, ensure_ascii=False), draft_id),
        )
        self.conn.commit()

    def reject_draft(self, draft_id: int, reason: str = "") -> bool:
        cur = self.conn.execute(
            "UPDATE drafts SET status='rejected', result=? WHERE id=? AND status IN ('pending','approved')",
            (json.dumps({"reason": reason[:200]}, ensure_ascii=False), draft_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ───────────────────── 限流 / 去重 ─────────────────────

    def count_posts_since(self, hours: int = 24) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) c FROM drafts WHERE status='posted' AND posted_at >= ?",
            (_now() - hours * 3600,),
        ).fetchone()
        return int(r["c"] or 0)

    def last_post_at(self) -> int | None:
        r = self.conn.execute(
            "SELECT MAX(posted_at) m FROM drafts WHERE status='posted'"
        ).fetchone()
        return int(r["m"]) if r and r["m"] else None

    def minutes_since_last_post(self) -> float | None:
        last = self.last_post_at()
        return None if last is None else (time.time() - last) / 60.0

    def has_posted_bvid(self, bvid: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM drafts WHERE bvid=? AND status='posted' AND kind='comment'",
            (bvid,),
        ).fetchone() is not None

    def has_posted_reply(self, bvid: str, root_rpid: int | str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM drafts WHERE bvid=? AND root_rpid=? AND status='posted' AND kind='reply'",
            (bvid, str(root_rpid)),
        ).fetchone() is not None

    def has_posted_reply_to(self, bvid: str, root_rpid: int | str,
                            parent_rpid: int | str) -> bool:
        """精确到**某一条子评论**的去重（2026-10-22 加）。

        以前只有楼层级 `has_posted_reply()`：同一层楼里冒出第 2、3 条新评论时，
        会被误判成"这层回过了"，该回的被静默吞掉。楼中楼回复落库时
        parent_rpid 就是被回复的那条评论，所以按 (root, parent) 能判准。
        """
        return self.conn.execute(
            """SELECT 1 FROM drafts WHERE bvid=? AND root_rpid=? AND parent_rpid=?
               AND status='posted' AND kind='reply'""",
            (bvid, str(root_rpid), str(parent_rpid)),
        ).fetchone() is not None

    def owner_posted_within_days(self, owner: str, days: int = 7) -> bool:
        if not owner:
            return False
        r = self.conn.execute(
            """SELECT COUNT(*) c FROM drafts d JOIN videos v ON d.bvid = v.bvid
               WHERE v.owner=? AND d.status='posted' AND d.posted_at >= ?""",
            (owner, _now() - days * 86400),
        ).fetchone()
        return int(r["c"] or 0) > 0

    def find_posted_draft_by_rpid(self, bvid: str, rpid: int | str) -> dict[str, Any] | None:
        """删评论前的凭证校验：必须是**我们自己发过**的那条，防模型猜 rpid 删别人评论。"""
        want = str(rpid)
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE bvid=? AND status='posted'", (bvid,)
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["result"] or "{}")
            except ValueError:
                continue
            for path in (("rpid",), ("rpid_str",), ("data", "rpid"), ("data", "rpid_str")):
                cur: Any = d
                for k in path:
                    cur = cur.get(k) if isinstance(cur, Mapping) else None
                if cur is not None and str(cur) == want:
                    return dict(r)
        return None

    # ───────────────────── 审计 ─────────────────────

    def record_action(self, kind: str, target: str = "", payload: Any = None,
                      result: Any = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO actions(kind,target,payload,result,created_at) VALUES(?,?,?,?,?)",
            (kind, str(target)[:200],
             json.dumps(_compact(payload), ensure_ascii=False) if payload is not None else None,
             json.dumps(_compact(result), ensure_ascii=False) if result is not None else None,
             _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (min(int(limit), 200),)
        ).fetchall()
        return [dict(r) for r in rows]

    # ───────────────────── settings ─────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    @property
    def paused(self) -> bool:
        return self.get_setting("paused", "0") == "1"

    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.set_setting("paused", "1" if paused else "0")
        if paused and reason:
            self.set_setting("pause_reason", reason)

    @property
    def pause_reason(self) -> str:
        return self.get_setting("pause_reason", "")

    # ───────────────────── 统计 ─────────────────────

    def stats(self) -> dict[str, Any]:
        def one(sql: str, *a: Any) -> int:
            r = self.conn.execute(sql, a).fetchone()
            return int((r[0] if r else 0) or 0)

        return {
            "videos": one("SELECT COUNT(*) FROM videos"),
            "comments_seen": one("SELECT COUNT(*) FROM comments_seen"),
            "drafts_pending": one("SELECT COUNT(*) FROM drafts WHERE status='pending'"),
            "drafts_approved": one("SELECT COUNT(*) FROM drafts WHERE status='approved'"),
            "drafts_posted": one("SELECT COUNT(*) FROM drafts WHERE status='posted'"),
            "posts_24h": self.count_posts_since(24),
            "last_post_at": self.last_post_at(),
            "minutes_since_last_post": (
                round(self.minutes_since_last_post(), 1)
                if self.minutes_since_last_post() is not None else None
            ),
            "paused": self.paused,
            "pause_reason": self.pause_reason,
        }
