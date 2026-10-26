from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import traceback
from typing import Any

from . import summary
from .agent import Agent, AgentOutcome
from .bili_mcp import BiliPlusBridge
from .config import load_run_config, paths_for
from .digest import write_outputs
from .memory import MemoryStore
from .ollama_client import OllamaClient
from .scout import load_commented_bvids, load_seen_bvids, scout
from .state import StateStore
from .util import now_iso


def _setup_logger(paths) -> logging.Logger:
    logger = logging.getLogger("bili_autopilot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    logging.getLogger("mcp").setLevel(logging.WARNING)
    return logger


def _status_summary(status: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "ok",
        "logged_in",
        "uname",
        "mid",
        "paused",
        "pause_reason",
        "posts_24h",
        "quota_left",
        "drafts_pending",
        "drafts_posted",
        "write_enabled",
        "limits",
        "videos",
    }
    return {k: v for k, v in status.items() if k in keep}


def _result(
    state: dict[str, Any],
    paths,
    outcome: AgentOutcome | None = None,
    *,
    error: str = "",
) -> dict[str, Any]:
    memory = MemoryStore(paths, state.get("config") or {})
    memory.load()
    data = memory.data
    return {
        "summary": (outcome.summary if outcome else "") or (data.get("finish") or {}).get("summary", ""),
        "reason": (outcome.reason if outcome else "") or error,
        "partial": bool(outcome.partial if outcome else state.get("partial")),
        "cancelled": bool(outcome.cancelled if outcome else False),
        "videos_seen": len(data.get("videos") or []),
        "comments_posted": sum(
            1 for v in (data.get("videos") or []) if v.get("comment_status") == "posted"
        ),
        "usage": data.get("usage") or {},
        "digest": str(paths.digest_md),
        "report": str(paths.report_md),
        "memory": str(paths.memory_md),
        "thinking": str(paths.thinking_md),
        "trace": str(paths.trace),
        "runner_log": str(paths.runner_log),
        "error": error,
    }


async def _execute(run_id: str, resume: bool, logger: logging.Logger) -> int:
    paths = paths_for(run_id)
    state_store = StateStore(paths)
    state = state_store.load()
    if not state:
        logger.error("run %s has no state.json", run_id)
        return 2

    config = load_run_config(paths)
    if not config.get("model"):
        config = state.get("config") or config

    memory = MemoryStore(paths, config)
    if resume:
        memory.load()
        if not memory.data.get("run_id"):
            memory.reset()
    else:
        memory.reset()

    state_store.clear_cancel()
    state_store.update(
        status="running",
        phase="starting",
        pid=os.getpid(),
        last_error=None,
        finished_at=None,
        started_at=state.get("started_at") or now_iso(),
        partial=False,
    )
    memory.set_status("running")
    logger.info("runner start run_id=%s resume=%s write=%s", run_id, resume, config.get("write"))

    ollama = OllamaClient(
        config.get("ollama_url", "http://127.0.0.1:11434"),
        config.get("model", "qwen3.5:4b-32k"),
        num_ctx=int(config.get("num_ctx", 32768)),
        num_predict=int(config.get("num_predict", 1200)),
        temperature=float(config.get("temperature", 0.35)),
        top_p=float(config.get("top_p", 0.9)),
        timeout=float(config.get("request_timeout", 600)),
        logger=logger,
    )
    outcome: AgentOutcome | None = None
    try:
        if not ollama.has_model():
            raise RuntimeError(
                f"Ollama 里没有模型 {config.get('model')}。先运行：ollama pull {config.get('model')}"
            )

        async with BiliPlusBridge(config, logger) as bridge:
            if resume:
                status = await bridge.call("status", {})
                if status.get("ok") is not False:
                    memory.set_bili_status(_status_summary(status))
                candidates = memory.data.get("candidates") or []
                if not candidates:
                    logger.info("resume has no candidates; scouting again")
                    candidates, _ = await scout(bridge, config, memory, logger)
            else:
                candidates, _ = await scout(bridge, config, memory, logger)

            if not candidates:
                logger.warning("scout returned no candidates; the model will be asked to finish")

            seen = load_seen_bvids()
            commented = load_commented_bvids()
            agent = Agent(
                bridge=bridge,
                ollama=ollama,
                memory=memory,
                config=config,
                state=state_store,
                paths=paths,
                candidates=candidates,
                bili_status=memory.data.get("bili_status") or {},
                seen_bvids=seen,
                commented_bvids=commented,
                logger=logger,
            )
            outcome = await agent.run()
            logger.info(
                "agent finished partial=%s cancelled=%s reason=%s",
                outcome.partial,
                outcome.cancelled,
                outcome.reason,
            )

        if outcome.cancelled:
            status = "cancelled"
        elif outcome.partial:
            status = "needs_continue"
        elif outcome.finished:
            status = "completed"
        else:
            status = "failed"

        result = _result(state_store.load(), paths, outcome)

        # 视频总结：本地模型多跑一次，把这一轮压成一段结论。
        # 云端读这一段，不读原始记忆 —— 省的是云端 token。
        #
        # ⚠️ 必须在 state_store.finish() **之前**做：
        #    finish 会把状态写成 completed，外层的 _wait 一看到终态就返回，
        #    run() 立刻去读 memory.json —— 那时总结还没写进去，返回里就是空的。
        #    这个是实测抓到的竞态，不是理论问题。
        if bool(config.get("summary_enabled")) and status != "cancelled":
            try:
                built = summary.build(
                    memory, ollama, config, logger,
                    previous=summary.recent_summaries(run_id, limit=2),
                )
                result["summary"] = {
                    "ok": True,
                    "degraded": built.get("degraded"),
                    "repaired": built.get("repaired"),
                    "chars": built.get("summary_chars"),
                    "raw_chars": built.get("raw_chars"),
                    "saved_ratio": built.get("saved_ratio"),
                    "usage": built.get("usage"),
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("summary failed: %s", exc)
                memory.add_error("summary", f"{type(exc).__name__}: {exc}")

        state_store.finish(
            status,
            result=result,
            partial=outcome.partial,
            error=outcome.reason if status in {"failed", "needs_continue"} else None,
        )
        write_outputs(paths, state_store.load(), memory)
        logger.info("runner done status=%s", status)
        return 0

    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("runner failed")
        memory.add_error("runner", message)
        memory.mark_finished(
            f"运行失败：{message}",
            issues=[message],
            partial=True,
        )
        # A hard runner failure is `failed`, not a normal continuation pause.
        memory.set_status("failed")
        state = state_store.load()
        state["status"] = "failed"
        state["phase"] = "done"
        state["finished_at"] = now_iso()
        state["partial"] = True
        state["last_error"] = message
        state_store.save(state)
        try:
            write_outputs(paths, state, memory)
        except Exception:  # noqa: BLE001
            logger.exception("could not write digest after failure")
        result = _result(state, paths, error=message)
        state_store.finish("failed", result=result, error=message, partial=True)
        logger.error("runner failed: %s", traceback.format_exc())
        return 1
    finally:
        ollama.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bili-autopilot detached runner")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    paths = paths_for(args.run_id)
    logger = _setup_logger(paths)
    try:
        return asyncio.run(_execute(args.run_id, args.resume, logger))
    except KeyboardInterrupt:
        logger.warning("runner interrupted")
        return 130
    except Exception:  # noqa: BLE001
        logger.exception("runner crashed before execution")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
