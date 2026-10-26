from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class OllamaError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    done_reason: str = ""
    prompt_eval_count: int = 0
    eval_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments", fn.get("args", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        calls.append(
            {
                "id": str(raw.get("id") or f"call_{index + 1}"),
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    return calls


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 32768,
        num_predict: int = 1200,
        temperature: float = 0.35,
        top_p: float = 0.9,
        timeout: float = 600,
        logger: logging.Logger | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = int(num_ctx)
        self.num_predict = int(num_predict)
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.timeout = float(timeout)
        self.log = logger or logging.getLogger(__name__)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=15.0, read=self.timeout, write=60.0, pool=15.0),
            headers={"Content-Type": "application/json"},
            # Ollama 永远在本机：绝不套系统代理。
            # 宿主/env 里残留的 HTTP_PROXY 会让 127.0.0.1:11434 走代理，
            # 探测拿到 502，整条链路被判成「引擎未就绪」。
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def tags(self) -> dict[str, Any]:
        try:
            response = self._client.get("/api/tags", timeout=15)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"Ollama 不可用：{exc}") from exc

    def has_model(self, model: str | None = None) -> bool:
        want = (model or self.model).strip()
        try:
            data = self.tags()
        except OllamaError:
            return False
        for item in data.get("models") or []:
            name = str(item.get("name") or item.get("model") or "")
            if name == want or name.split(":")[0] == want.split(":")[0]:
                return True
        return False

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        think: bool = True,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "10m",
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "repeat_penalty": 1.05,
            },
        }
        if tools:
            payload["tools"] = tools
        # ⚠️ 必须**显式**给 think，不能只在 true 时才加：
        #    qwen3 这类模型默认开思维链，不写这个字段等于让它在 num_predict
        #    里把 token 全花在 thinking 上、content 空着回来（done_reason=length）。
        #    整理文本、只读巡检都不需要思维链。
        payload["think"] = bool(think)

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.post("/api/chat", json=payload)
                if response.status_code == 400 and "think" in payload:
                    # 老版本 Ollama / 不支持思维链的模型会拒这个字段，去掉重试一次。
                    self.log.warning("Ollama rejected think, retrying without it")
                    payload.pop("think", None)
                    response = self._client.post("/api/chat", json=payload)
                if response.status_code == 404:
                    raise OllamaError(
                        f"模型 {self.model} 不存在。先运行：ollama pull {self.model}"
                    )
                if response.status_code >= 500:
                    raise OllamaError(f"Ollama HTTP {response.status_code}: {response.text[:300]}")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise OllamaError("Ollama 返回了非 JSON 对象")
                if data.get("error"):
                    raise OllamaError(str(data["error"]))
                message = data.get("message") or {}
                if not isinstance(message, dict):
                    message = {}
                return ChatResult(
                    content=str(message.get("content") or ""),
                    thinking=str(message.get("thinking") or ""),
                    tool_calls=normalize_tool_calls(message),
                    done_reason=str(data.get("done_reason") or ""),
                    prompt_eval_count=int(data.get("prompt_eval_count") or 0),
                    eval_count=int(data.get("eval_count") or 0),
                    raw=data,
                )
            except OllamaError:
                raise
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                last_error = exc
                self.log.warning("Ollama request failed (attempt %s/3): %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(2 + attempt * 3)
                    continue
            except httpx.HTTPStatusError as exc:
                last_error = exc
                self.log.warning("Ollama HTTP error (attempt %s/3): %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(2 + attempt * 3)
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.log.exception("Unexpected Ollama error")
                break
        raise OllamaError(f"Ollama 调用失败：{last_error}")
