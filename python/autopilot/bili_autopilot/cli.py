from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import server


def _print(value: Any, *, digest_only: bool = False) -> None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            print(value)
            return
    if digest_only and isinstance(value, dict) and value.get("digest"):
        print(value["digest"])
        return
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", default="")
    parser.add_argument("--write", action="store_true", help="允许本地模型真正发评论")
    parser.add_argument("--max-videos", type=int, default=1)
    parser.add_argument("--max-comments", type=int, default=1)
    parser.add_argument("--max-minutes", type=int, default=20)
    parser.add_argument("--wait", type=int, default=0, dest="wait_seconds",
                        help="0=按 max-minutes 自动等满")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model", default="")
    parser.add_argument("--include-seen", action="store_true")
    parser.add_argument("--no-auto-continue", dest="auto_continue",
                        action="store_false", default=True)
    parser.add_argument("--auto-continue-max", type=int, default=2, dest="auto_continue_max")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bili-autopilot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")

    run_parser = sub.add_parser("run", help="启动一轮并等待")
    _add_run_args(run_parser)

    wait_parser = sub.add_parser("wait", help="等待已有 run")
    wait_parser.add_argument("run_id")
    wait_parser.add_argument("--wait", type=int, default=0, dest="wait_seconds")
    wait_parser.add_argument("--max-chars", type=int, default=14000)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("run_id", nargs="?", default="")

    digest_parser = sub.add_parser("digest")
    digest_parser.add_argument("run_id")
    digest_parser.add_argument("--max-chars", type=int, default=14000)

    trace_parser = sub.add_parser("trace")
    trace_parser.add_argument("run_id")
    trace_parser.add_argument("--count", type=int, default=40)
    trace_parser.add_argument("--max-chars", type=int, default=12000)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=10)

    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("run_id")
    cancel_parser.add_argument("--force", action="store_true")

    continue_parser = sub.add_parser("continue")
    continue_parser.add_argument("run_id")
    continue_parser.add_argument("--wait", type=int, default=0, dest="wait_seconds")
    continue_parser.add_argument("--max-minutes", type=int, default=0)
    continue_parser.add_argument("--max-chars", type=int, default=14000)

    args = parser.parse_args(argv)

    if args.command == "health":
        _print(server.health())
    elif args.command == "run":
        _print(
            server.run(
                task=args.task,
                write=args.write,
                max_videos=args.max_videos,
                max_comments=args.max_comments,
                max_minutes=args.max_minutes,
                wait_seconds=args.wait_seconds,
                force=args.force,
                model=args.model,
                include_seen=args.include_seen,
                auto_continue=args.auto_continue,
                auto_continue_max=args.auto_continue_max,
            )
        )
    elif args.command == "wait":
        _print(
            server.wait(
                args.run_id,
                wait_seconds=args.wait_seconds,
                max_chars=args.max_chars,
            )
        )
    elif args.command == "status":
        _print(server.status(args.run_id))
    elif args.command == "digest":
        _print(
            server.digest(args.run_id, max_chars=args.max_chars),
            digest_only=True,
        )
    elif args.command == "trace":
        _print(server.trace(args.run_id, count=args.count, max_chars=args.max_chars))
    elif args.command == "list":
        _print(server.list_runs(limit=args.limit))
    elif args.command == "cancel":
        _print(server.cancel(args.run_id, force=args.force))
    elif args.command == "continue":
        _print(
            server.continue_run(
                args.run_id,
                wait_seconds=args.wait_seconds,
                max_minutes=args.max_minutes,
                max_chars=args.max_chars,
            )
        )
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
