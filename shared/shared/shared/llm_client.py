"""
Client for the local LLM gateway (llama-swap + llama.cpp) on caos.

Speaks the OpenAI-compatible `/v1/chat/completions` API exposed by llama-swap.
Everything runs on the home server; no external provider is involved.
"""

import asyncio
import os
from typing import Any, Dict, List, Optional

import aiohttp

from .logging import setup_logger

logger = setup_logger(__name__)

# Model aliases served by llama-swap (see portfolio/k8s/llama/configmap.yaml)
MODEL_CODER = "coder"          # Qwen3.6-35B-A3B, agents / tool calling
MODEL_VISION = "vision"        # Gemma 4 26B-A4B + mmproj
MODEL_REASONING = "reasoning"  # gpt-oss-20b
MODEL_EXTRACT = "extract"      # Qwen3.5-9B, fast JSON extraction

DEFAULT_BASE_URL = "http://home.server:30080/llm"
# Generous default: a request may wait for llama-swap to unload/load a model (10-60s)
DEFAULT_TIMEOUT_S = 180.0

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRYABLE_STATUSES = {502, 503, 504}

# Global semaphore to limit concurrent LLM requests across all client instances
_global_llm_semaphore: Optional[asyncio.Semaphore] = None


def _get_global_semaphore(max_concurrent: int = 2) -> asyncio.Semaphore:
    """Get or create the global LLM semaphore for the current event loop."""
    global _global_llm_semaphore
    # Create semaphore lazily to avoid event loop issues
    if _global_llm_semaphore is None:
        _global_llm_semaphore = asyncio.Semaphore(max_concurrent)
    return _global_llm_semaphore


class LLMError(Exception):
    """Raised when the LLM gateway returns an error or an unusable response."""


def _image_data_url(image_base64: str) -> str:
    """Wrap a raw base64 image as a data URL (pass-through if it already is one)."""
    if image_base64.startswith("data:"):
        return image_base64
    return f"data:image/png;base64,{image_base64}"


def _to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Ollama-style messages (with an `images` list) to OpenAI content parts."""
    converted: List[Dict[str, Any]] = []
    for message in messages:
        images = message.get("images")
        if not images:
            converted.append({k: v for k, v in message.items() if k != "images"})
            continue
        parts: List[Dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": _image_data_url(img)}} for img in images
        ]
        if message.get("content"):
            parts.append({"type": "text", "text": message["content"]})
        converted.append({"role": message.get("role", "user"), "content": parts})
    return converted


class LLMClient:
    """Async client for the local llama-swap gateway (text and vision)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        max_concurrent: int = 2,
        timeout_s: Optional[float] = None,
    ):
        """Initialize the LLM client.

        Args:
            base_url: Gateway base URL (without /v1). Defaults to LLM_BASE_URL env var.
            model: Default model alias. Defaults to LLM_MODEL env var or `extract`.
            max_concurrent: Maximum concurrent LLM requests (applies globally)
            timeout_s: Total request timeout. Defaults to LLM_TIMEOUT_S env var or 180s.
        """
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL", MODEL_EXTRACT)
        self.timeout_s = timeout_s or float(os.getenv("LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.max_concurrent = max_concurrent
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Enter async context."""
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context."""
        if self.session:
            await self.session.close()

    async def generate(
            self,
            prompt: str,
            model: Optional[str] = None,
            system: Optional[str] = None,
            temperature: float = 0.2,
            num_predict: int = 4096,
            format: Optional[str] = None,
        ) -> str:
        """Generate text from a single prompt.

        Args:
            prompt: The user prompt
            model: Model alias. Defaults to the one specified in constructor.
            system: Optional system prompt
            temperature: Sampling temperature (default: 0.2)
            num_predict: Maximum tokens to generate (default: 4096)
            format: "json" to constrain the output to valid JSON

        Returns:
            str: The generated text

        Raises:
            LLMError: If the request fails after all retries
        """
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        data = await self.chat(
            messages=messages,
            model=model,
            format=format,
            temperature=temperature,
            num_predict=num_predict,
        )
        return self._content(data)

    async def chat(
            self,
            messages: List[Dict[str, Any]],
            model: Optional[str] = None,
            format: Optional[str] = None,
            temperature: float = 0.2,
            num_predict: int = 4096,
        ) -> Dict[str, Any]:
        """Chat completion supporting images for vision models.

        Args:
            messages: List of message dicts. Each message can include an 'images' list with base64 strings.
            model: Optional override model alias.
            format: Optional output format hint ("json").
            temperature: Sampling temperature.
            num_predict: Max tokens to generate.

        Returns:
            Dict response from the /v1/chat/completions endpoint.
        """
        if self.session is None:
            raise LLMError("LLMClient must be used inside 'async with'")

        url = f"{self.base_url}/v1/chat/completions"
        model_name = model or self.model
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": _to_openai_messages(messages),
            "stream": False,
            "temperature": temperature,
            "max_tokens": num_predict,
        }
        if format == "json":
            payload["response_format"] = {"type": "json_object"}

        semaphore = _get_global_semaphore(self.max_concurrent)
        last_error: Optional[str] = None

        for attempt in range(MAX_RETRIES):
            retry = attempt < MAX_RETRIES - 1
            try:
                async with semaphore:
                    async with self.session.post(url, json=payload) as response:
                        if response.status == 200:
                            return await response.json()
                        error_text = (await response.text())[:500]
                        last_error = f"status={response.status} body={error_text}"
                        if response.status not in RETRYABLE_STATUSES or not retry:
                            logger.error(f"LLM request failed model={model_name} {last_error}")
                            raise LLMError(f"LLM request failed ({response.status})")
            except asyncio.TimeoutError:
                last_error = f"timeout after {self.timeout_s}s"
                if not retry:
                    logger.error(f"LLM request timed out model={model_name} after {self.timeout_s}s")
                    raise LLMError(f"LLM request timed out after {self.timeout_s}s")
            except aiohttp.ClientError as e:
                last_error = f"connection error: {e}"
                if not retry:
                    logger.error(f"LLM connection failed model={model_name}: {e}")
                    raise LLMError(f"LLM connection failed: {e}") from e

            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"LLM request failed (attempt {attempt + 1}/{MAX_RETRIES}) model={model_name}, "
                f"retrying in {delay}s: {last_error[:100]}"
            )
            await asyncio.sleep(delay)

        raise LLMError(f"LLM request failed after {MAX_RETRIES} retries: {last_error}")

    async def extract_from_image(
            self,
            image_base64: str,
            instruction: str,
            model: Optional[str] = None,
            format: str = "json",
            num_predict: int = 4096,
        ) -> str:
        """Convenience helper to perform vision extraction from a single image.

        Args:
            image_base64: Base64 encoded image (raw or data URL)
            instruction: Extraction instruction
            model: Vision model alias (default: `vision`)
            format: Output format (default: json)
            num_predict: Max tokens to generate

        Returns the assistant content (string). Use JSON-only instructions and format="json" to get structured output.
        """
        messages = [{
            "role": "user",
            "content": instruction,
            "images": [image_base64],
        }]
        data = await self.chat(
            messages=messages,
            model=model or MODEL_VISION,
            format=format,
            num_predict=num_predict,
        )
        return self._content(data)

    async def health_check(self) -> bool:
        """Check if the LLM gateway is healthy.

        Returns:
            bool: True if healthy, False otherwise
        """
        if self.session is None:
            return False
        try:
            async with self.session.get(f"{self.base_url}/health", timeout=aiohttp.ClientTimeout(total=20)) as response:
                return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"LLM health check failed: {e}")
            return False

    @staticmethod
    def _content(data: Dict[str, Any]) -> str:
        """Extract the assistant message content from a chat completion response."""
        try:
            return (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"Invalid LLM response shape: {str(data)[:200]}") from e
