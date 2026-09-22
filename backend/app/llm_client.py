import json
import logging
from typing import Any

import httpx2

from .logging_utils import log_event

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class LLMError(Exception):
    """Base exception safe to expose at the API boundary."""


class LLMConfigurationError(LLMError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMInvalidResponseError(LLMError):
    pass


class OpenAICompatibleClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._api_key = api_key.strip() if api_key else None
        self._base_url = base_url.strip().rstrip("/") if base_url else None
        self._model = model.strip() if model else None
        self._timeout_seconds = timeout_seconds

    def ensure_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self._api_key),
                ("LLM_BASE_URL", self._base_url),
                ("LLM_MODEL", self._model),
            )
            if not value
        ]
        if missing:
            log_event(logger, logging.ERROR, "llm_error", category="configuration")
            raise LLMConfigurationError(
                "LLM не настроена. Задайте переменные окружения: " + ", ".join(missing) + "."
            )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.ensure_configured()
        request = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        try:
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(self._timeout_seconds),
                trust_env=True,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request,
                )
                response.raise_for_status()
        except httpx2.TimeoutException as exc:
            log_event(logger, logging.ERROR, "llm_error", category="timeout")
            raise LLMUnavailableError("LLM не ответила за отведённое время.") from exc
        except httpx2.HTTPStatusError as exc:
            log_event(
                logger,
                logging.ERROR,
                "llm_error",
                category="http_error",
                status_code=exc.response.status_code,
            )
            raise LLMUnavailableError(
                f"LLM недоступна: API вернул HTTP {exc.response.status_code}."
            ) from exc
        except httpx2.RequestError as exc:
            log_event(logger, logging.ERROR, "llm_error", category="connection")
            raise LLMUnavailableError("Не удалось подключиться к LLM API.") from exc

        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message.get("content")
            tool_calls = message.get("tool_calls") or []
            if content is not None and not isinstance(content, str):
                raise TypeError
            if not isinstance(tool_calls, list):
                raise TypeError
            if not tool_calls and not content:
                raise ValueError
            return {"role": "assistant", "content": content, "tool_calls": tool_calls}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log_event(logger, logging.ERROR, "llm_error", category="invalid_response")
            raise LLMInvalidResponseError("LLM вернула некорректный ответ.") from exc
