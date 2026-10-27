from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import (
    atomic_write_json,
    json_dumps,
    normalize_mid,
    parse_bool,
    parse_int,
    project_root,
    python_exe,
    read_json,
    truthy,
)


def default_home() -> Path:
    return Path(os.environ.get("BILI_AUTOPILOT_HOME") or (Path.home() / ".bili-autopilot"))


def default_bili_plus_home() -> Path:
    return Path(os.environ.get("BILI_PLUS_HOME") or (Path.home() / ".dsh" / "bili-plus"))


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    state: Path
    trace: Path
    memory_json: Path
    memory_md: Path
    thinking_md: Path
    digest_md: Path
    report_md: Path
    runner_log: Path
    config_json: Path
    cancel_flag: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "run_dir": str(self.run_dir),
            "state": str(self.state),
            "trace": str(self.trace),
            "memory_json": str(self.memory_json),
            "memory_md": str(self.memory_md),
            "thinking_md": str(self.thinking_md),
            "digest_md": str(self.digest_md),
            "report_md": str(self.report_md),
            "runner_log": str(self.runner_log),
            "config_json": str(self.config_json),
        }


def safe_run_id(run_id: str) -> str | None:
    """run_id 必须是一个**纯目录名**；不合法返回 None。

    旧实现 `runs / run_id` 完全不校验，`../outside` 这种就能读到 runs 目录
    之外的 state.json/digest.md，cancel/continue 还会在外部目录建文件、
    按里面的 PID 去杀进程（实测复现）。
    """
    s = str(run_id or "").strip()
    if not s or len(s) > 64:
        return None
    if s in {".", ".."} or ".." in s or "/" in s or "\\" in s:
        return None
    if not all(c.isalnum() or c in "._-" for c in s):
        return None
    return s


# 非法 run_id 一律指到这个占位目录：读不到（run_not_found），也绝不会越界。
_INVALID_RUN_DIR = "__invalid__"


def paths_for(run_id: str) -> RunPaths:
    run_dir = default_home() / "runs" / (safe_run_id(run_id) or _INVALID_RUN_DIR)
    return RunPaths(
        run_dir=run_dir,
        state=run_dir / "state.json",
        trace=run_dir / "trace.jsonl",
        memory_json=run_dir / "memory.json",
        memory_md=run_dir / "memory.md",
        thinking_md=run_dir / "thinking.md",
        digest_md=run_dir / "digest.md",
        report_md=run_dir / "report.md",
        runner_log=run_dir / "runner.log",
        config_json=run_dir / "config.json",
        cancel_flag=run_dir / "cancel.requested",
    )


def _read_bili_plus_config() -> dict[str, Any]:
    path = default_bili_plus_home() / "config.json"
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


DEFAULT_AI_TECH_KEYWORDS = ["AI 大模型", "开源模型", "编程工具", "推理框架", "AI 实测"]
DEFAULT_AI_FUN_KEYWORDS = ["AI 绘画", "AI 配音", "AI 整活", "AI 生成", "AI 玩梗"]


def parse_other_prefs(value: Any) -> list[dict[str, Any]]:
    """「其他视频」偏好：一行一条，`名称 | 关键词1,关键词2 | 配额`。

    也接受已经解析好的 list[dict]，以及省略配额（默认 1）。
    写坏的行直接跳过 —— 设置页里手打的东西不能因为一个逗号让整轮跑不起来。
    """
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                words = [str(x).strip() for x in (item.get("keywords") or []) if str(x).strip()]
                quota = parse_int(item.get("quota"), 1)
                if name and words:
                    out.append({"name": name, "keywords": words[:4], "quota": max(0, min(quota, 10))})
            elif isinstance(item, str):
                out.extend(parse_other_prefs(item))
        return out

    out = []
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0] if parts else ""
        words = _split_csv(parts[1]) if len(parts) > 1 else []
        quota = parse_int(parts[2], 1) if len(parts) > 2 else 1
        if not name or not words:
            continue
        out.append({"name": clip_name(name), "keywords": words[:4], "quota": max(0, min(quota, 10))})
    return out[:6]


def clip_name(text: str, limit: int = 24) -> str:
    text = str(text or "").strip()
    return text[:limit]


def _env_config_file() -> dict[str, Any]:
    """An external JSON config that overrides defaults.

    The DSH plugin writes the settings page's local-model knobs (model, num_ctx,
    temperature, max_videos …) here, so changing them in the UI does not require
    a new MCP tool signature. Missing / broken file = no override.
    """
    path = os.environ.get("BILI_AUTOPILOT_CONFIG_FILE", "").strip()
    if not path:
        return {}
    data = read_json(Path(path), {})
    return data if isinstance(data, dict) else {}


def default_config() -> dict[str, Any]:
    """Defaults tuned for qwen3.5:4b-32k and dsh-bili-plus.

    The plugin's own config supplies watch MIDs / keywords, but safety limits
    always come from the plugin's MCP environment. We never raise them here.
    """
    plugin = _read_bili_plus_config()
    watch = [normalize_mid(x) for x in _split_csv(plugin.get("watchMids"))]
    watch = [x for x in watch if x]
    legacy_keywords = _split_csv(plugin.get("keywords"))
    # 0 是有效值 = 不限新旧；只有"没填"才回落到 2 天
    fresh_days = parse_int(plugin.get("freshDays"), 2)
    if fresh_days < 0:
        fresh_days = 0

    cfg = {
        "task": "刷一轮 B 站，挑值得看的视频，理解内容，并在合适时写一条安全评论。",
        "model": os.environ.get("BILI_AUTOPILOT_MODEL", "qwen3.5:4b-32k"),
        "ollama_url": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
        "num_ctx": 32768,
        "num_predict": 1200,
        "temperature": 0.35,
        "top_p": 0.9,
        "request_timeout": 600,
        "max_steps": 40,
        "max_continues": 8,
        "max_minutes": 20,
        "max_videos": 2,
        "max_comments": 1,
        "candidate_limit": 12,
        "subtitle_max_chars": 4500,
        "comments_num": 8,
        "fresh_days": fresh_days,
        # 关注的 UP 完全由使用者决定：留空就是真留空，不再回落到任何内置 UID
        "watch_mids": watch,
        # ── 视频质量门槛（关注 UP 不受限） ──
        "min_up_fans": 10000,
        "min_play": 10000,
        # ── 视频详细设置：一天最多浏览多少条 + 三块配额 ──
        "daily_video_max": 6,
        "ai_tech_quota": 3,
        "ai_tech_keywords": legacy_keywords or list(DEFAULT_AI_TECH_KEYWORDS),
        "ai_fun_quota": 2,
        "ai_fun_keywords": list(DEFAULT_AI_FUN_KEYWORDS),
        "other_prefs": [],
        # ── 视频总结（本地先整合，云端只读压缩结果） ──
        "summary_enabled": True,
        "include_seen": False,
        "include_old": False,
        "write": False,
        "wait_seconds": 900,
        "max_posts_per_day": 5,
        "min_interval_min": 30,
        "auto_continue": True,
        "auto_continue_max": 2,
    }
    # 兼容老配置里的 keywords：当作 AI技术 那一块
    overlay = _env_config_file()
    if overlay.get("keywords") and not overlay.get("ai_tech_keywords"):
        overlay = {**overlay, "ai_tech_keywords": overlay["keywords"]}
    if overlay:
        cfg.update({k: v for k, v in overlay.items() if v is not None})
    return cfg


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = default_config()
    if not raw:
        return cfg
    for key, value in raw.items():
        if value is None:
            continue
        if key in {
            "num_ctx",
            "num_predict",
            "max_steps",
            "max_continues",
            "max_minutes",
            "max_videos",
            "max_comments",
            "candidate_limit",
            "subtitle_max_chars",
            "comments_num",
            "fresh_days",
            "wait_seconds",
            "max_posts_per_day",
            "min_interval_min",
            "request_timeout",
            "auto_continue_max",
            "min_up_fans",
            "min_play",
            "daily_video_max",
            "ai_tech_quota",
            "ai_fun_quota",
        }:
            cfg[key] = parse_int(value, parse_int(cfg.get(key), 0))
        elif key in {"temperature", "top_p"}:
            try:
                cfg[key] = float(value)
            except (TypeError, ValueError):
                pass
        elif key in {"write", "include_seen", "include_old", "auto_continue", "summary_enabled"}:
            cfg[key] = parse_bool(value, bool(cfg.get(key)))
        elif key == "watch_mids":
            # 🔴 空列表是**有效值**：用户在设置页清空「关注的 UP」，就是要清空。
            #    这里以前写成 `... or cfg[key]`，于是清空之后又静默回落到默认关注列表，
            #    表现就是"删了又回来"。别再加那个 or。
            mids = [normalize_mid(x) for x in _split_csv(value)]
            cfg[key] = [x for x in mids if x]
        elif key in {"keywords", "ai_tech_keywords", "ai_fun_keywords"}:
            # `keywords` 是老键名，等同 AI技术 那一块
            target = "ai_tech_keywords" if key == "keywords" else key
            # 同样允许清空：空列表就是"这个板块不要了"，不再回落到示例词
            cfg[target] = _split_csv(value)
        elif key == "other_prefs":
            cfg[key] = parse_other_prefs(value)
        elif key == "model":
            text = str(value).strip()
            if text:
                cfg[key] = text
        elif key == "ollama_url":
            text = str(value).strip().rstrip("/")
            if text:
                cfg[key] = text
        elif key == "task":
            cfg[key] = str(value)
        else:
            cfg[key] = value

    # Hard sanity bounds. These are local-model guardrails, not Bilibili limits.
    cfg["num_ctx"] = max(4096, min(cfg["num_ctx"], 131072))
    cfg["num_predict"] = max(256, min(cfg["num_predict"], 4096))
    cfg["max_steps"] = max(4, min(cfg["max_steps"], 200))
    cfg["max_continues"] = max(0, min(cfg["max_continues"], 30))
    cfg["max_minutes"] = max(1, min(cfg["max_minutes"], 120))
    cfg["max_videos"] = max(1, min(cfg["max_videos"], 10))
    cfg["max_comments"] = max(0, min(cfg["max_comments"], 5))
    cfg["candidate_limit"] = max(3, min(cfg["candidate_limit"], 30))
    cfg["subtitle_max_chars"] = max(500, min(cfg["subtitle_max_chars"], 8000))
    cfg["comments_num"] = max(3, min(cfg["comments_num"], 20))
    # 0 保留：表示不限新旧（别在这里抬到 1，否则设置页的「0 表示不限」又变成谎话）
    cfg["fresh_days"] = max(0, min(cfg["fresh_days"], 14))
    cfg["wait_seconds"] = max(30, min(cfg["wait_seconds"], 1800))
    cfg["temperature"] = max(0.0, min(cfg["temperature"], 1.2))
    cfg["top_p"] = max(0.1, min(cfg["top_p"], 1.0))
    cfg["request_timeout"] = max(30, min(cfg["request_timeout"], 1800))
    cfg["max_posts_per_day"] = max(1, min(cfg["max_posts_per_day"], 20))
    cfg["min_interval_min"] = max(0, min(cfg["min_interval_min"], 1440))

    # 视频详细设置：一天最多浏览是所有板块的总闸。
    cfg["min_up_fans"] = max(0, min(cfg["min_up_fans"], 100_000_000))
    cfg["min_play"] = max(0, min(cfg["min_play"], 100_000_000))
    cfg["daily_video_max"] = max(1, min(cfg["daily_video_max"], 20))
    cfg["ai_tech_quota"] = max(0, min(cfg["ai_tech_quota"], cfg["daily_video_max"]))
    cfg["ai_fun_quota"] = max(0, min(cfg["ai_fun_quota"], cfg["daily_video_max"]))
    cfg["ai_tech_keywords"] = [str(x)[:24] for x in cfg["ai_tech_keywords"]][:6]
    cfg["ai_fun_keywords"] = [str(x)[:24] for x in cfg["ai_fun_keywords"]][:6]
    others = cfg.get("other_prefs") or []
    cfg["other_prefs"] = [p for p in others if p.get("quota", 0) > 0][:6]

    # 三块配额之和超过总闸时：**不静默改用户的值**，而是记下来交给
    # 前端提示 + 运行时按优先级收敛。elda 配了多少就是多少，用户看得见。
    wanted = cfg["ai_tech_quota"] + cfg["ai_fun_quota"] + sum(p["quota"] for p in cfg["other_prefs"])
    cfg["quota_wanted"] = wanted
    cfg["quota_overflow"] = wanted > cfg["daily_video_max"]
    cfg["max_videos"] = min(cfg["max_videos"], cfg["daily_video_max"])
    return cfg


def make_run_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return normalize_config(overrides or {})


def load_run_config(paths: RunPaths) -> dict[str, Any]:
    raw = read_json(paths.config_json, {})
    return normalize_config(raw if isinstance(raw, dict) else {})


def save_run_config(paths: RunPaths, config: dict[str, Any]) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.config_json, config)


def allow_write_env() -> bool:
    return truthy(os.environ.get("BILI_AUTOPILOT_ALLOW_WRITE", ""))


def write_permission_error() -> str:
    return (
        "写入未启用：去设置页「B站视频评论设置 → 发评论」把「允许发评论」打开，"
        "然后用 write=true 调用。"
    )


def ensure_home() -> Path:
    home = default_home()
    (home / "runs").mkdir(parents=True, exist_ok=True)
    return home


def describe_paths() -> dict[str, str]:
    return {
        "project_root": str(project_root()),
        "home": str(default_home()),
        "bili_plus_home": str(default_bili_plus_home()),
        "python": python_exe(),
    }


def config_preview(config: dict[str, Any]) -> str:
    safe = {k: v for k, v in config.items() if k not in {"task"}}
    return json_dumps(safe)
