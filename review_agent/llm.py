"""DeepSeek Chat Completions adapter, with bounded retries and no secret logging."""

import time
from typing import Any, Protocol

import httpx

from review_agent.config import Settings
from review_agent.models import Metrics


class ModelError(RuntimeError):
    pass


class ChatModel(Protocol):
    metrics: Metrics

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_output: bool = False,
        max_tokens: int = 6000,
    ) -> dict: ...


class DeepSeekClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        settings.validate()
        self.settings = settings
        self.metrics = Metrics()
        self.client = httpx.Client(
            timeout=settings.timeout, transport=transport, follow_redirects=False
        )

    def close(self) -> None:
        self.client.close()

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_output: bool = False,
        max_tokens: int = 6000,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        retryable = {408, 429, 500, 502, 503, 504}
        self.metrics.model_calls += 1
        for attempt in range(self.settings.max_retries + 1):
            self.metrics.http_attempts += 1
            try:
                response = self.client.post(
                    self.settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                    json=payload,
                )
            except httpx.RequestError:
                if attempt < self.settings.max_retries:
                    time.sleep(0.5 * 2**attempt)
                    continue
                raise ModelError(
                    "无法连接 DeepSeek 或请求超时，请检查网络和 API 地址后重试。"
                ) from None
            if response.status_code in retryable and attempt < self.settings.max_retries:
                time.sleep(0.5 * 2**attempt)
                continue
            if response.status_code != 200:
                messages_by_status = {
                    400: "请求参数不被支持，请检查模型名及接口兼容性。",
                    401: "API Key 无效，请检查配置。",
                    402: "API 余额不足，请检查 DeepSeek 账户。",
                    403: "API 访问被拒绝，请检查账户权限。",
                    404: "API 地址或模型不存在，请检查配置。",
                    429: "请求过于频繁，请稍后重试。",
                }
                detail = messages_by_status.get(response.status_code, "服务暂不可用，请稍后重试。")
                raise ModelError(f"DeepSeek HTTP {response.status_code}：{detail}")
            try:
                body = response.json()
                usage = body.get("usage") or {}
                self.metrics.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.metrics.completion_tokens += int(usage.get("completion_tokens") or 0)
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ModelError("模型输出达到长度上限，请缩小审查范围后重试。")
                message = choice["message"]
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ValueError("invalid assistant message")
                if not message.get("content") and not message.get("tool_calls"):
                    raise ValueError("empty message")
                # Preserve protocol fields, including reasoning when returned by a compatible endpoint.
                return {
                    key: value
                    for key, value in message.items()
                    if key in {"role", "content", "tool_calls", "reasoning_content"}
                }
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise ModelError("模型服务返回了无法解析的响应，请重试。") from None
        raise ModelError("请求失败。")
