import asyncio
from typing import Any, Dict, List

import pytest
from aiohttp import web

from shared.llm_client import LLMClient, LLMError, MODEL_VISION, _to_openai_messages


def _completion(content: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.fixture
async def gateway(aiohttp_unused_port):
    """Tiny fake llama-swap gateway that records requests."""
    state: Dict[str, Any] = {"requests": [], "fail_times": 0, "fail_status": 503, "delay": 0.0}

    async def completions(request: web.Request) -> web.Response:
        body = await request.json()
        state["requests"].append(body)
        if state["fail_times"] > 0:
            state["fail_times"] -= 1
            return web.Response(status=state["fail_status"], text="upstream loading")
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        return web.json_response(_completion('  {"ok": true}  '))

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = aiohttp_unused_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    state["url"] = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()


@pytest.fixture
def aiohttp_unused_port():
    import socket

    def _port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    return _port


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr("shared.llm_client.RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr("shared.llm_client._global_llm_semaphore", None)


async def test_generate_builds_openai_payload(gateway):
    async with LLMClient(base_url=gateway["url"], model="extract") as llm:
        text = await llm.generate("hi", system="sys", format="json", num_predict=50, temperature=0.0)

    assert text == '{"ok": true}'
    body = gateway["requests"][0]
    assert body["model"] == "extract"
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 50
    assert body["temperature"] == 0.0


async def test_extract_from_image_uses_vision_content_parts(gateway):
    async with LLMClient(base_url=gateway["url"]) as llm:
        await llm.extract_from_image(image_base64="QUJD", instruction="price?")

    body = gateway["requests"][0]
    assert body["model"] == MODEL_VISION
    parts: List[Dict[str, Any]] = body["messages"][0]["content"]
    assert parts[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    assert parts[1] == {"type": "text", "text": "price?"}


async def test_retries_on_503_then_succeeds(gateway):
    gateway["fail_times"] = 2
    async with LLMClient(base_url=gateway["url"]) as llm:
        assert await llm.generate("hi") == '{"ok": true}'
    assert len(gateway["requests"]) == 3


async def test_non_retryable_status_raises(gateway):
    gateway["fail_times"] = 1
    gateway["fail_status"] = 400
    async with LLMClient(base_url=gateway["url"]) as llm:
        with pytest.raises(LLMError):
            await llm.generate("hi")
    assert len(gateway["requests"]) == 1


async def test_timeout_raises_llm_error(gateway):
    gateway["delay"] = 1.0
    async with LLMClient(base_url=gateway["url"], timeout_s=0.2) as llm:
        with pytest.raises(LLMError, match="timed out"):
            await llm.generate("hi")


async def test_health_check(gateway):
    async with LLMClient(base_url=gateway["url"]) as llm:
        assert await llm.health_check() is True


async def test_env_defaults(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://example.invalid/llm/")
    monkeypatch.setenv("LLM_MODEL", "reasoning")
    client = LLMClient()
    assert client.base_url == "http://example.invalid/llm"
    assert client.model == "reasoning"


def test_messages_without_images_pass_through():
    msgs = [{"role": "user", "content": "x"}]
    assert _to_openai_messages(msgs) == msgs
