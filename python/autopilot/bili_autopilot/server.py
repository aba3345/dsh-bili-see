from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import budget
from .config import (
    allow_write_env,
    default_bili_plus_home,
    default_config,
    default_home,
    ensure_home,
    load_run_config,
    make_run_config,
    paths_for,
    save_run_config,
    write_permission_error,
)
from .digest import read_digest, write_outputs
from .memory import MemoryStore
from .ollama_client import OllamaClient, OllamaError
from .state import ACTIVE, TERMINAL, StateStore
from .util import (
    clip,
    json_dumps,
    new_run_id,
    now_iso,
    pid_alive,
    project_root,
    python_exe,
    runner_exe,
    tail_jsonl,
    terminate_pid,
)

mcp = FastMCP(
    "bili-autopilot",
    instructions=(
        "Local qwen Bilibili autopilot over dsh-bili-plus. "
        "Call run once, then wait or continue_run only if needed. "
        "The completed run returns a digest for DeepSeek to summarize."
    ),
)


def _json(value: Any) -> str:
    return json_dumps(value)


def _state_summary(state: dict[str, Any], paths) -> dict[str, Any]:
    pid = state.get("pid")
    return {
        "ok": True,
        "run_id": state.get("run_id") or paths.run_dir.name,
        "status": state.get("status") or "unknown",
        "phase": state.get("phase"),
        "model": state.get("model"),
        "write": state.get("write"),
        "created_at": state.get("created_at"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "updated_at": state.get("updated_at"),
        "steps": state.get("steps") or state.get("step") or 0,
        "continues": state.get("continues") or 0,
        "auto_continues": state.get("auto_continues") or 0,
        "videos_seen": state.get("videos_seen") or 0,
        "comments_posted": state.get("comments_posted") or 0,
        "partial": bool(state.get("partial")),
        "last_error": state.get("last_error"),
        "current_bvid": state.get("current_bvid"),
        "pid": pid,
        "pid_alive": pid_alive(pid) if pid else False,
    }


def _run_dirs() -> list[Path]:
    root = default_home() / "runs"
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def _active_runs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_dir in _run_dirs():
        paths = paths_for(run_dir.name)
        state = StateStore(paths).load()
        if state.get("status") in ACTIVE:
            out.append(_state_summary(state, paths))
    out.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return out


def _wait(paths, wait_seconds: int) -> dict[str, Any]:
    store = StateStore(paths)
    deadline = time.monotonic() + max(1, min(int(wait_seconds or 1), 1800))
    dead_checks = 0
    while True:
        state = store.load()
        if not state:
            return {}
        if state.get("status") in TERMINAL:
            return state
        pid = state.get("pid")
        if pid and not pid_alive(pid):
            dead_checks += 1
            if dead_checks >= 2:
                store.update(
                    status="failed",
                    phase="done",
                    last_error="runner 进程已退出但没有写回终态（查看 runner.log）",
                    finished_at=now_iso(),
                )
                return store.load()
        else:
            dead_checks = 0
        if time.monotonic() >= deadline:
            return state
        time.sleep(1.0)


def _effective_wait(wait_seconds: int, config: dict[str, Any]) -> int:
    """wait_seconds<=0 means "auto": one MCP call should outlast the whole run."""
    if wait_seconds and int(wait_seconds) > 0:
        return max(30, min(int(wait_seconds), 1800))
    minutes = int(config.get("max_minutes", 20) or 20)
    return max(120, min(minutes * 60 + 180, 1800))


def _wait_and_continue(
    paths,
    wait_seconds: int,
    *,
    auto_continue: bool | None = None,
    auto_continue_max: int | None = None,
) -> dict[str, Any]:
    """Block until the run is genuinely done, restarting it when it needs to.

    This is the part that keeps DeepSeek silent: a local model that runs out of
    context (needs_continue) or dies (failed) is resumed here, inside the same
    MCP call, so the orchestrating model never spends tokens babysitting it.

    ⚠️ 「最多续几次」是**单次调用**的额度，不是一天的额度：
       每次工具调用都从 0 开始数，最多续 auto_continue_max 次；
       `auto_continues` 只是累计值，用来在汇报里说明这一轮总共续了几次。
       上一版从这里接着 state 里的累计数继续数，导致同一天第二次调用直接
       "额度已用完"、一次都不续 —— 表现就是"续跑功能时好时坏"。
    """
    store = StateStore(paths)
    config = load_run_config(paths)
    wait_seconds = _effective_wait(wait_seconds, config)
    if auto_continue is None:
        auto_continue = bool(config.get("auto_continue", True))
    if auto_continue_max is None:
        auto_continue_max = int(config.get("auto_continue_max", 2) or 0)
    state = _wait(paths, wait_seconds)
    if not state or not auto_continue:
        return state

    allowance = max(0, int(auto_continue_max))
    used_before = int(state.get("auto_continues") or 0)
    spawned = 0
    seen_errors: list[str] = []

    while state.get("status") in {"needs_continue", "failed"} and spawned < allowance:
        error = str(state.get("last_error") or "")
        if error and error in seen_errors:
            # Same failure twice: another resume would just burn local time.
            break
        if error:
            seen_errors.append(error)
        spawned += 1
        store.clear_cancel()
        store.update(
            status="resuming",
            phase="queued",
            pid=None,
            last_error=None,
            finished_at=None,
            partial=False,
            auto_continues=used_before + spawned,
        )
        try:
            pid = _spawn_runner(paths.run_dir.name, resume=True)
            store.update(pid=pid)
        except Exception as exc:  # noqa: BLE001
            store.finish(
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                partial=True,
            )
            break
        state = _wait(paths, wait_seconds)
        if not state:
            break
    return state


def _spawn_runner(run_id: str, *, resume: bool) -> int:
    paths = paths_for(run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    # pythonw.exe：GUI 子系统，不会弹出命令框（详见 util.runner_exe 的注释）
    cmd = [runner_exe(), "-m", "bili_autopilot.runner", "--run-id", run_id]
    if resume:
        cmd.append("--resume")

    env = os.environ.copy()
    root = str(project_root())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["BILI_AUTOPILOT_PYTHON"] = python_exe()
    env.setdefault("BILI_AUTOPILOT_HOME", str(default_home()))
    env.setdefault("BILI_PLUS_HOME", str(default_bili_plus_home()))

    # ⚠️ 不要和 DETACHED_PROCESS 一起用 CREATE_NO_WINDOW：
    #    MSDN 明确写「CREATE_NO_WINDOW 与 DETACHED_PROCESS / CREATE_NEW_CONSOLE
    #    同时出现时会被忽略」。之前三个标志一起给，等于关窗口那一位被丢掉，
    #    于是跑本地模型时闪黑框。
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    with paths.runner_log.open("ab") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=flags,
        )
    return int(proc.pid)


def _payload(
    paths,
    state: dict[str, Any],
    *,
    include_digest: bool = True,
    max_chars: int = 14000,
) -> dict[str, Any]:
    payload = _state_summary(state, paths)
    payload["run_ok"] = state.get("status") == "completed"
    if state.get("status") in TERMINAL:
        payload["result"] = state.get("result") or {}
        payload["files"] = paths.as_dict()
        if include_digest:
            payload["digest"] = read_digest(paths, max_chars=max_chars)
        summary = _read_summary(paths)
        if summary:
            payload["summary"] = summary
        payload["next"] = (
            "运行已结束。优先用 summary（本地模型整合过，最省 token）汇报；"
            "summary 缺失或明显不足时再看 digest。不要编造未记录的视频或评论。"
        )
    else:
        payload["next"] = (
            f"还在运行。调用 wait(run_id='{payload['run_id']}', wait_seconds=900) 继续等待，"
            "不要反复调用 status。"
        )
    return payload


def _read_summary(paths) -> dict[str, Any]:
    """读本地模型整合好的视频总结（runner 在收尾时写入 memory.json）。"""
    from .util import read_json

    data = read_json(paths.memory_json, {})
    summary = (data or {}).get("summary") or {}
    if not isinstance(summary, dict) or not summary.get("text"):
        return {}
    return {
        "text": summary.get("text"),
        "degraded": bool(summary.get("degraded")),
        "repaired": bool(summary.get("repaired")),
        "chars": summary.get("summary_chars"),
        "raw_chars": summary.get("raw_chars"),
        "saved_ratio": summary.get("saved_ratio"),
        "usage": summary.get("usage") or {},
    }


def _spawn_failure(
    paths,
    memory: MemoryStore,
    store: StateStore,
    message: str,
) -> str:
    memory.add_error("spawn", message)
    memory.mark_finished(f"启动失败：{message}", issues=[message], partial=True)
    memory.set_status("failed")
    store.finish("failed", error=message, partial=True)
    state = store.load()
    try:
        write_outputs(paths, state, memory)
    except Exception:  # noqa: BLE001
        pass
    return _json(
        {
            "ok": False,
            "error": message,
            "run_id": paths.run_dir.name,
            "status": "failed",
            "files": paths.as_dict(),
        }
    )


@mcp.tool()
def run(
    task: str = "",
    write: bool = False,
    max_videos: int = 1,
    max_comments: int = 1,
    max_minutes: int = 20,
    wait_seconds: int = 0,
    force: bool = False,
    model: str = "",
    include_seen: bool = False,
    auto_continue: bool | None = None,
    auto_continue_max: int | None = None,
) -> str:
    """启动一轮本地模型 B站巡航，并阻塞到真正结束（一次调用走完全程）。

    只读/写入：write=false 时 max_comments 会被强制为 0。真发评论必须
    同时给 write=true，且 MCP 环境里 BILI_AUTOPILOT_ALLOW_WRITE=1。
    **没有预演模式** —— comment_post 一调用就是真发。

    wait_seconds=0（默认）表示按 max_minutes 自动等满。
    auto_continue=true 时，本地模型上下文耗尽(needs_continue)或进程失败(failed)
    会在本次调用内部自动续跑，最多 auto_continue_max 次（**单次调用**的额度）。
    完成后返回里直接带 digest + 视频总结；DeepSeek 不需要再轮询 status。
    """
    if write and not allow_write_env():
        return _json(
            {
                "ok": False,
                "error": "write_not_allowed",
                "message": write_permission_error(),
            }
        )

    if not write:
        max_comments = 0
    elif max_comments < 0:
        max_comments = 1

    active = _active_runs()
    if active and not force:
        return _json(
            {
                "ok": False,
                "error": "already_running",
                "message": "已有一轮在跑；先 wait/continue_run，或传 force=true 强制换新任务。",
                "runs": active,
            }
        )
    if active and force:
        for item in active:
            paths = paths_for(item["run_id"])
            store = StateStore(paths)
            store.request_cancel()
            if item.get("pid"):
                terminate_pid(item.get("pid"))
            store.finish(
                "cancelled",
                error="被新的 force run 终止",
                result={"reason": "force_replaced"},
            )

    overrides: dict[str, Any] = {
        "task": task or default_config().get("task"),
        "write": bool(write),
        "max_videos": int(max_videos),
        "max_comments": int(max_comments),
        "max_minutes": int(max_minutes),
        "include_seen": bool(include_seen),
    }
    if model.strip():
        overrides["model"] = model.strip()
    config = make_run_config(overrides)

    # ── 一天的浏览额度还够吗 ──
    daily = budget.summary(int(config.get("daily_video_max") or 6))
    if daily["remaining"] <= 0:
        return _json(
            {
                "ok": False,
                "error": "daily_video_budget_exhausted",
                "message": (
                    f"今天已经看了 {daily['viewed']} 条，到上限 {daily['daily_max']} 条了。"
                    "要接着跑就把「视频详细设置 → 一天最多浏览」调大。"
                ),
                "daily": daily,
            }
        )
    config["max_videos"] = min(int(config["max_videos"]), daily["remaining"])

    ensure_home()
    run_id = new_run_id()
    paths = paths_for(run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    save_run_config(paths, config)
    store = StateStore(paths)
    store.create(run_id, config)
    memory = MemoryStore(paths, config)
    memory.reset()
    budget.note_run()

    try:
        pid = _spawn_runner(run_id, resume=False)
        store.update(pid=pid)
    except Exception as exc:  # noqa: BLE001
        return _spawn_failure(paths, memory, store, f"{type(exc).__name__}: {exc}")

    state = _wait_and_continue(
        paths,
        wait_seconds,
        auto_continue=auto_continue,
        auto_continue_max=auto_continue_max,
    )
    if not state:
        return _json({"ok": False, "error": "run_state_missing", "run_id": run_id})
    return _json(_payload(paths, state))


@mcp.tool()
def wait(
    run_id: str,
    wait_seconds: int = 0,
    max_chars: int = 14000,
    auto_continue: bool | None = None,
    auto_continue_max: int | None = None,
) -> str:
    """继续等待一个正在运行的 run；结束时返回 digest。

    wait_seconds=0 表示按该 run 的 max_minutes 自动等满。需要接着跑时
    自动续跑，不要高频轮询。
    """
    paths = paths_for(run_id)
    store = StateStore(paths)
    state = store.load()
    if not state:
        return _json({"ok": False, "error": "run_not_found", "run_id": run_id})
    state = _wait_and_continue(
        paths,
        wait_seconds,
        auto_continue=auto_continue,
        auto_continue_max=auto_continue_max,
    )
    if not state:
        return _json({"ok": False, "error": "run_state_missing", "run_id": run_id})
    return _json(_payload(paths, state, max_chars=max(1000, min(int(max_chars), 40000))))


@mcp.tool()
def status(run_id: str = "") -> str:
    """轻量查看 run 状态。DeepSeek 正常流程不需要反复调用。"""
    if not run_id:
        runs = _active_runs()
        return _json({"ok": True, "active": runs, "count": len(runs)})
    paths = paths_for(run_id)
    state = StateStore(paths).load()
    if not state:
        return _json({"ok": False, "error": "run_not_found", "run_id": run_id})
    payload = _state_summary(state, paths)
    payload["has_digest"] = paths.digest_md.exists()
    return _json(payload)


@mcp.tool()
def digest(run_id: str, max_chars: int = 14000) -> str:
    """读取本地模型跑完后生成的 digest（记忆、评论、思维链要点）。"""
    paths = paths_for(run_id)
    state = StateStore(paths).load()
    if not state:
        return _json({"ok": False, "error": "run_not_found", "run_id": run_id})
    text = read_digest(paths, max_chars=max(1000, min(int(max_chars), 40000)))
    return _json(
        {
            "ok": True,
            "run_id": run_id,
            "status": state.get("status"),
            "digest": text,
            "files": paths.as_dict(),
        }
    )


@mcp.tool()
def summarize(run_id: str = "", refresh: bool = False) -> str:
    """视频总结：本地模型先把这一轮的记忆压成一段结论，再交给云端读。

    优先返回 runner 收尾时已经整合好的那份（不再调模型，零额外开销）。
    refresh=true 或那份不存在时，才现场让本地模型再跑一次整合。
    返回里带 raw_chars / chars / saved_ratio —— 用来判断到底省了多少。
    """
    from . import summary as summary_mod
    from .util import read_json

    if not run_id:
        runs = _active_runs()
        rows = sorted([p.name for p in (default_home() / "runs").glob("*")], reverse=True) if (default_home() / "runs").exists() else []
        run_id = rows[0] if rows else ""
        if not run_id:
            return _json({"ok": False, "error": "no_runs", "message": "还没有任何一轮记录", "active": runs})
    paths = paths_for(run_id)
    state = StateStore(paths).load()
    if not state:
        return _json({"ok": False, "error": "run_not_found", "run_id": run_id})
    config = load_run_config(paths)

    cached = (read_json(paths.memory_json, {}) or {}).get("summary") or {}
    if cached.get("text") and not refresh:
        return _json({
            "ok": True, "run_id": run_id, "cached": True,
            "text": cached.get("text"),
            "degraded": bool(cached.get("degraded")),
            "repaired": bool(cached.get("repaired")),
            "chars": cached.get("summary_chars"),
            "raw_chars": cached.get("raw_chars"),
            "saved_ratio": cached.get("saved_ratio"),
            "usage": cached.get("usage") or {},
            "daily": budget.summary(int(config.get("daily_video_max") or 6)),
        })

    memory = MemoryStore(paths, config)
    memory.load()
    if not memory.data.get("videos"):
        return _json({"ok": False, "error": "no_videos", "run_id": run_id,
                      "message": "这一轮没有视频记录，没什么可总结的"})
    ollama = OllamaClient(
        config.get("ollama_url", "http://127.0.0.1:11434"),
        config.get("model", "qwen3.5:4b-32k"),
        num_ctx=int(config.get("num_ctx", 32768)),
        num_predict=min(900, int(config.get("num_predict", 1200))),
        temperature=0.2,
        top_p=0.9,
        timeout=float(config.get("request_timeout", 600)),
    )
    try:
        built = summary_mod.build(
            memory, ollama, config,
            previous=summary_mod.recent_summaries(run_id, limit=2),
        )
    finally:
        ollama.close()
    return _json({
        "ok": True, "run_id": run_id, "cached": False,
        "text": built.get("text"),
        "degraded": built.get("degraded"),
        "repaired": built.get("repaired"),
        "chars": built.get("summary_chars"),
        "raw_chars": built.get("raw_chars"),
        "saved_ratio": built.get("saved_ratio"),
        "usage": built.get("usage") or {},
        "daily": budget.summary(int(config.get("daily_video_max") or 6)),
    })


@mcp.tool()
def trace(run_id: str, count: int = 40, max_chars: int = 12000) -> str:
    """查看最近的事件轨迹。只在 digest 不够时用，避免把完整轨迹塞进上下文。"""
    paths = paths_for(run_id)
    if not paths.trace.exists():
        return _json({"ok": False, "error": "trace_not_found", "run_id": run_id})
    events = tail_jsonl(paths.trace, max(1, min(int(count), 200)))
    text = clip(json_dumps(events), max(1000, min(int(max_chars), 30000)))
    return _json({"ok": True, "run_id": run_id, "events": events, "text": text})


@mcp.tool()
def cancel(run_id: str, force: bool = False) -> str:
    """请求取消一个 run。force=true 会直接结束 runner 进程。"""
    paths = paths_for(run_id)
    store = StateStore(paths)
    state = store.load()
    if not state:
        return _json({"ok": False, "error": "run_not_found", "run_id": run_id})
    if state.get("status") in TERMINAL:
        return _json({"ok": True, "run_id": run_id, "status": state.get("status")})
    store.request_cancel()
    if force:
        terminate_pid(state.get("pid"))
        store.finish("cancelled", error="force cancel", result={"reason": "force_cancel"})
        return _json({"ok": True, "run_id": run_id, "status": "cancelled", "forced": True})
    store.update(phase="cancelling")
    return _json(
        {
            "ok": True,
            "run_id": run_id,
            "status": "cancelling",
            "message": "已写取消标记；runner 会在下一步检查时停下。",
        }
    )


@mcp.tool()
def list_runs(limit: int = 10) -> str:
    """列出最近的 run。"""
    rows: list[dict[str, Any]] = []
    for run_dir in _run_dirs():
        paths = paths_for(run_dir.name)
        state = StateStore(paths).load()
        if not state:
            continue
        row = _state_summary(state, paths)
        row["has_digest"] = paths.digest_md.exists()
        rows.append(row)
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    rows = rows[: max(1, min(int(limit), 50))]
    return _json({"ok": True, "count": len(rows), "runs": rows})


@mcp.tool()
def continue_run(
    run_id: str,
    wait_seconds: int = 0,
    max_minutes: int = 0,
    max_chars: int = 14000,
    auto_continue: bool | None = None,
    auto_continue_max: int | None = None,
) -> str:
    """手动让 needs_continue / failed / cancelled 的 run 带着记忆继续跑。

    正常流程不需要它：run/wait 已经会在本地模型上下文耗尽或进程失败时
    自动续跑。这个工具只用于 auto_continue=false 或手动接管的情况。
    """
    paths = paths_for(run_id)
    store = StateStore(paths)
    state = store.load()
    if not state:
        return _json({"ok": False, "error": "run_not_found", "run_id": run_id})
    current = state.get("status")
    if current in ACTIVE:
        return _json({"ok": False, "error": "already_running", "run_id": run_id})
    if current == "completed":
        return _json({"ok": False, "error": "already_completed", "run_id": run_id})
    if current not in {"needs_continue", "failed", "cancelled"}:
        return _json(
            {"ok": False, "error": "not_continuable", "run_id": run_id, "status": current}
        )

    config = load_run_config(paths)
    if max_minutes and int(max_minutes) > 0:
        config["max_minutes"] = int(max_minutes)
        save_run_config(paths, config)

    store.clear_cancel()
    store.update(
        status="resuming",
        phase="queued",
        pid=None,
        last_error=None,
        finished_at=None,
        partial=False,
    )
    try:
        pid = _spawn_runner(run_id, resume=True)
        store.update(pid=pid)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        store.finish("failed", error=message, partial=True)
        return _json({"ok": False, "error": message, "run_id": run_id})

    state = _wait_and_continue(
        paths,
        wait_seconds,
        auto_continue=auto_continue,
        auto_continue_max=auto_continue_max,
    )
    if not state:
        return _json({"ok": False, "error": "run_state_missing", "run_id": run_id})
    return _json(_payload(paths, state, max_chars=max(1000, min(int(max_chars), 40000))))


def _credential_exists() -> bool:
    own = default_bili_plus_home() / "bili_credential.json"
    if own.exists() and own.stat().st_size > 10:
        return True
    for candidate in (
        Path.home() / ".dsh" / "bili-duty" / "bili_credential.json",
        Path.home() / ".dsh" / "bili-scroll" / "bili_credential.json",
    ):
        if candidate.exists() and candidate.stat().st_size > 10:
            return True
    return False


@mcp.tool()
def health() -> str:
    """检查 Ollama、模型、bili-plus 文件/数据库/凭证和写入开关。"""
    config = default_config()
    bili_home = default_bili_plus_home()
    model = config.get("model")
    ollama_ok = False
    model_ok = False
    ollama_error = ""
    client = OllamaClient(
        config.get("ollama_url", "http://127.0.0.1:11434"),
        model,
        timeout=20,
    )
    try:
        tags = client.tags()
        ollama_ok = True
        names = [str(x.get("name") or x.get("model") or "") for x in tags.get("models") or []]
        model_ok = any(
            n == model or n.split(":")[0] == str(model).split(":")[0] for n in names
        )
    except OllamaError as exc:
        ollama_error = str(exc)
    finally:
        client.close()

    server_file = bili_home / "mcp_server.py"
    db_file = bili_home / "bili_plus.db"
    cred_ok = _credential_exists()
    python_ok = Path(python_exe()).exists()
    checks = {
        "python": python_ok,
        "ollama": ollama_ok,
        "model": model_ok,
        "bili_plus_server": server_file.exists(),
        "bili_plus_db": db_file.exists(),
        "credential": cred_ok,
    }
    return _json(
        {
            "ok": all(checks.values()),
            "checks": checks,
            "model": model,
            "ollama_url": config.get("ollama_url"),
            "ollama_error": ollama_error,
            "write_allowed_by_env": allow_write_env(),
            "python": python_exe(),
            "project_root": str(project_root()),
            "bili_plus_home": str(bili_home),
            "bili_plus_server": str(server_file),
            "bili_plus_db": str(db_file),
            "home": str(default_home()),
            "active_runs": _active_runs(),
            "daily": budget.summary(int(config.get("daily_video_max") or 6)),
            "quality_gate": {
                "min_up_fans": config.get("min_up_fans"),
                "min_play": config.get("min_play"),
            },
            "quota": {
                "daily_video_max": config.get("daily_video_max"),
                "ai_tech": config.get("ai_tech_quota"),
                "ai_fun": config.get("ai_fun_quota"),
                "other": sum(int(p.get("quota") or 0) for p in (config.get("other_prefs") or [])),
                "wanted": config.get("quota_wanted"),
                "overflow": config.get("quota_overflow"),
            },
        }
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
