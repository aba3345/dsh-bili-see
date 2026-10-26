from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .config import default_bili_plus_home
from .util import clip, json_dumps, python_exe


class BiliToolError(RuntimeError):
    pass


READ_TOOLS = [
    "status",
    "credential_check",
    "video_info",
    "subtitle",
    "keyframes",
    "ai_conclusion",
    "comments",
    "search",
    "popular",
    "user_videos",
    # 质量门槛要查 UP 粉丝数：少了这个，scout 的 gate 只能一路"粉丝未知"放行
    "user_info",
    "safety_check",
    "style_check",
    "persona_prompt",
]
WRITE_TOOLS = ["draft_create", "comment_post", "verify_posted", "draft_delete"]

# Only these are shown to the 4B model. Scout tools stay private.
MODEL_READ_TOOLS = ["video_info", "subtitle", "keyframes", "ai_conclusion", "comments"]
MODEL_WRITE_TOOLS = ["draft_create", "comment_post"]

TOOL_HINTS = {
    "video_info": "看视频标题、简介、UP、时长、标签、播放数据。",
    "subtitle": "读视频字幕原文；判断内容优先用它。max_chars 已自动限制。",
    "keyframes": "抽 2~4 张关键帧截图（本地模型能看图）。字幕之外最硬的依据。",
    "ai_conclusion": "读 B站官方 AI 总结；没有字幕时用。",
    "comments": "看热门评论，了解观众在聊什么。",
    "draft_create": "把评论写成待发草稿。只在 write=true 且已记录 comment 判断后调用。",
    "comment_post": "真发草稿（没有预演，调用即发出）。",
    "safety_check": "检查评论文本或视频是否安全。runner 会自动调用。",
    "style_check": "检查文本像不像人设。runner 会自动调用。",
    "persona_prompt": "读取人设提示块。",
    "status": "查看插件登录、暂停、额度和草稿状态。",
    "credential_check": "查看 B站 登录身份。",
    "search": "搜索 B站视频。",
    "popular": "B站热门视频。",
    "user_info": "查 UP 粉丝数（质量门槛用）。",
    "user_videos": "看 UP 主最新投稿。",
    "verify_posted": "核验草稿是否真的出现在评论区。",
    "draft_delete": "软删除一条草稿。",
}


def _sanitize_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out: dict[str, Any] = {"type": "object", "properties": {}}
    props = schema.get("properties")
    if isinstance(props, dict):
        clean_props: dict[str, Any] = {}
        for name, spec in props.items():
            if not isinstance(spec, dict):
                clean_props[name] = {"type": "string"}
                continue
            clean: dict[str, Any] = {}
            for key in ("type", "description", "enum", "default", "items", "minimum", "maximum"):
                if key in spec:
                    clean[key] = spec[key]
            if "anyOf" in spec:
                # FastMCP uses anyOf for Optional[...]; flatten to the first type.
                for option in spec.get("anyOf") or []:
                    if isinstance(option, dict) and option.get("type") not in (None, "null"):
                        clean.update(option)
                        break
            if "type" not in clean:
                clean["type"] = "string"
            if clean.get("type") == "array" and "items" not in clean:
                clean["items"] = {"type": "string"}
            clean_props[name] = clean
        out["properties"] = clean_props
    required = schema.get("required")
    if isinstance(required, list):
        out["required"] = [str(x) for x in required if str(x) in out["properties"]]
    return out


class BiliPlusBridge:
    """Async MCP client for dsh-bili-plus, running as a stdio subprocess."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.log = logger or logging.getLogger(__name__)
        self.home = default_bili_plus_home()
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: dict[str, Any] = {}

    def _env(self) -> dict[str, str]:
        allowed = list(READ_TOOLS)
        if bool(self.config.get("write")):
            allowed.extend(WRITE_TOOLS)
        env = os.environ.copy()
        env.update(
            {
                "BILI_PLUS_HOME": str(self.home),
                "BILI_PLUS_WRITE": "1" if self.config.get("write") else "0",
                "BILI_PLUS_TOOLS": ",".join(dict.fromkeys(allowed)),
                "BILI_PLUS_MAX_POSTS_PER_DAY": str(self.config.get("max_posts_per_day", 5)),
                "BILI_PLUS_MIN_INTERVAL_MIN": str(self.config.get("min_interval_min", 30)),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        # Never inherit a test-only gate unless the caller explicitly set it.
        for key in ("BILI_PLUS_TEST_MODE", "BILI_PLUS_TEST_BVID"):
            if key in env and not str(env[key]).strip():
                env.pop(key, None)
        return env

    async def __aenter__(self) -> "BiliPlusBridge":
        server = self.home / "mcp_server.py"
        if not server.exists():
            raise BiliToolError(f"找不到 dsh-bili-plus 服务端：{server}")
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=python_exe(),
            args=[str(server)],
            cwd=str(self.home),
            env=self._env(),
            encoding="utf-8",
            encoding_error_handler="replace",
        )
        read_stream, write_stream = await self._stack.enter_async_context(
            stdio_client(params, errlog=sys.stderr)
        )
        self.session = await self._stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=max(60, int(self.config.get("request_timeout", 600)))),
                client_info=None,
            )
        )
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = {tool.name: tool for tool in listed.tools}
        self.log.info("bili-plus tools: %s", ", ".join(sorted(self.tools)))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack:
            await self._stack.aclose()
        self._stack = None
        self.session = None

    async def call(self, name: str, arguments: dict[str, Any] | None = None, *, timeout: int | None = None) -> dict[str, Any]:
        if not self.session:
            raise BiliToolError("bili-plus 未连接")
        if name not in self.tools:
            raise BiliToolError(f"bili-plus 没有工具 {name}（可能被 BILI_PLUS_TOOLS 过滤）")
        args = arguments or {}
        try:
            result = await self.session.call_tool(
                name,
                args,
                read_timeout_seconds=timedelta(seconds=int(timeout or self.config.get("request_timeout", 600))),
            )
        except Exception as exc:  # noqa: BLE001
            raise BiliToolError(f"{name} 调用失败：{exc}") from exc

        is_error = bool(getattr(result, "isError", False))

        def _parse_text(text: str) -> dict[str, Any]:
            text = text.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"ok": not is_error, "text": text}
            if isinstance(parsed, dict):
                if is_error and parsed.get("ok") is not False:
                    parsed["ok"] = False
                return parsed
            return {"ok": not is_error, "data": parsed}

        # FastMCP usually mirrors a scalar string return into both text content
        # and {"result": "<json>"} structured content. Prefer the raw text,
        # then unwrap the one-key structured wrapper.
        text = ""
        for item in getattr(result, "content", []) or []:
            value = getattr(item, "text", None)
            if value:
                text += value
        parsed = _parse_text(text)
        if parsed:
            return parsed

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict) and structured:
            if set(structured.keys()) == {"result"}:
                inner = structured.get("result")
                if isinstance(inner, str):
                    parsed = _parse_text(inner)
                    if parsed:
                        return parsed
                return {"ok": not is_error, "data": inner}
            return structured
        return {"ok": not is_error, "text": ""}

    async def call_ok(self, name: str, arguments: dict[str, Any] | None = None, *, timeout: int | None = None) -> dict[str, Any]:
        result = await self.call(name, arguments, timeout=timeout)
        if result.get("ok") is False:
            raise BiliToolError(f"{name}: {result.get('error') or json_dumps(result)}")
        return result

    def model_tool_specs(self) -> list[dict[str, Any]]:
        names = list(MODEL_READ_TOOLS)
        if bool(self.config.get("write")):
            names.extend(MODEL_WRITE_TOOLS)
        specs: list[dict[str, Any]] = []
        for name in names:
            tool = self.tools.get(name)
            if tool is None:
                self.log.warning("model tool missing from bili-plus: %s", name)
                continue
            description = TOOL_HINTS.get(name) or clip(getattr(tool, "description", "") or name, 240)
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": _sanitize_schema(getattr(tool, "inputSchema", {})),
                    },
                }
            )
        return specs

    def has_tool(self, name: str) -> bool:
        return name in self.tools
