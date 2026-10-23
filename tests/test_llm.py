import json

import httpx
import pytest

from review_agent.config import Settings
from review_agent.llm import DeepSeekClient, ModelError


def client_for(handler, **settings):
    return DeepSeekClient(
        Settings(api_key="test-secret-never-log", **settings), httpx.MockTransport(handler)
    )


def test_chat_contract_and_usage():
    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert body["tools"][0]["type"] == "function"
        assert body["max_tokens"] == 1600
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
        )

    client = client_for(handler)
    try:
        assert (
            client.complete([], tools=[{"type": "function"}], json_output=True, max_tokens=1600)[
                "content"
            ]
            == "{}"
        )
        assert client.metrics.prompt_tokens == 12
        assert client.metrics.http_attempts == 1
    finally:
        client.close()


def test_rate_limit_retry_and_success(monkeypatch):
    monkeypatch.setattr("review_agent.llm.time.sleep", lambda _: None)
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    client = client_for(handler)
    try:
        assert client.complete([])["content"] == "ok"
        assert client.metrics.http_attempts == 3
    finally:
        client.close()


def test_invalid_key_not_retried_or_leaked():
    client = client_for(lambda _: httpx.Response(401, text="test-secret-never-log"))
    try:
        with pytest.raises(ModelError) as error:
            client.complete([])
        assert "401" in str(error.value)
        assert "test-secret" not in str(error.value)
        assert client.metrics.http_attempts == 1
        assert "test-secret" not in repr(client.settings)
    finally:
        client.close()


def test_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr("review_agent.llm.time.sleep", lambda _: None)

    def handler(request):
        raise httpx.ReadTimeout("private network details", request=request)

    client = client_for(handler)
    try:
        with pytest.raises(ModelError, match="超时"):
            client.complete([])
        assert client.metrics.http_attempts == 3
    finally:
        client.close()


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        {
            "choices": [
                {"message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}
            ]
        },
    ],
)
def test_malformed_or_truncated_response(body):
    client = client_for(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(ModelError):
            client.complete([])
    finally:
        client.close()


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://user:pass@example.com", "https://example.com?key=secret"]
)
def test_unsafe_config_is_rejected(url):
    with pytest.raises(ValueError):
        Settings(api_key="secret", base_url=url).validate()
