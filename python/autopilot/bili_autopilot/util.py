from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_ts() -> int:
    return int(time.time())


def new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def clip(value: Any, limit: int, suffix: str = "…") -> str:
    text = "" if value is None else str(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def compact_text(value: Any, limit: int = 2000) -> str:
    """Collapse whitespace and clip. Used for tool results shown to a 4B model."""
    text = "" if value is None else str(value)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return clip(text, limit)


def json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        fh.flush()


def tail_text(path: Path, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def tail_jsonl(path: Path, count: int = 40) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, count) :]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            value = {"type": "raw", "text": clip(line, 800)}
        if isinstance(value, dict):
            out.append(value)
        else:
            out.append({"type": "value", "value": value})
    return out


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return truthy(value)


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def ts_text(value: Any) -> str:
    ts = parse_timestamp(value)
    if not ts:
        return "未知"
    try:
        return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知"


def normalize_bvid(value: Any) -> str:
    match = re.search(r"BV[0-9A-Za-z]{10}", str(value or ""))
    return match.group(0) if match else ""


def normalize_mid(value: Any) -> int:
    match = re.search(r"\d{2,}", str(value or ""))
    return int(match.group(0)) if match else 0


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def terminate_pid(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=15,
            )
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 15)
        return True
    except OSError:
        return False


def python_exe() -> str:
    """The interpreter used for the MCP server and the detached runner."""
    override = os.environ.get("BILI_AUTOPILOT_PYTHON", "").strip()
    if override and Path(override).exists():
        return override
    return sys.executable


def runner_exe() -> str:
    """跑 runner 用的解释器：优先 pythonw.exe，永远不弹命令框。

    为什么要单独一个：python.exe 是**控制台子系统**程序，只要父进程没有控制台
    又没带对 creationflags，Windows 就会给它开一个新窗口 —— 用户看到的就是
    「跑本地模型时闪过一个黑框」。pythonw.exe 是 GUI 子系统，永远不会分配控制台。
    它的 stdout/stderr 我们本来就重定向到 runner.log，所以不依赖控制台。
    """
    exe = Path(python_exe())
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def env_flag(name: str) -> bool:
    return truthy(os.environ.get(name, ""))
