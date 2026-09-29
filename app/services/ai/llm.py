"""
LLM access with structured outputs.

`structured(system, user, schema)` returns a validated Pydantic object. The OpenAI provider uses
Structured Outputs (`chat.completions.parse`), so responses always match the schema — no JSON
repair and no placeholder fallbacks. The fake provider (tests, local dev without keys) builds
deterministic, schema-valid content so every flow can run end to end offline.
"""

import logging
from functools import lru_cache
from typing import Protocol, TypeVar

import openai
from pydantic import BaseModel

from app.core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not produce a usable answer (refusal, truncation, provider error)."""


class LLM(Protocol):
    def structured(self, system: str, user: str, schema: type[T], max_tokens: int = 8000) -> T: ...


class OpenAILLM:
    def __init__(self, api_key: str, model: str):
        self.client = openai.OpenAI(api_key=api_key, max_retries=2, timeout=180)
        self.model = model

    def structured(self, system, user, schema, max_tokens=8000):
        try:
            completion = self.client.chat.completions.parse(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
                max_completion_tokens=max_tokens,
            )
        except openai.LengthFinishReasonError as exc:
            raise LLMError("La respuesta del modelo quedó incompleta; intenta con menos contenido.") from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise LLMError("El filtro de contenido del modelo bloqueó la respuesta.") from exc
        except openai.APIError as exc:
            logger.error("llm_provider_error", extra={"error": str(exc)[:300], "model": self.model})
            raise LLMError("El servicio de IA no respondió. Intenta de nuevo en unos minutos.") from exc
        message = completion.choices[0].message
        if message.refusal or message.parsed is None:
            raise LLMError("El modelo no pudo generar este contenido.")
        usage = completion.usage
        logger.info(
            "llm_completion",
            extra={
                "model": self.model,
                "schema": schema.__name__,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            },
        )
        return message.parsed


@lru_cache
def get_llm() -> LLM:
    settings = get_settings()
    if settings.use_fake_providers:
        from app.services.ai.fake import FakeLLM  # here, not at the top: fake -> designer -> this module

        return FakeLLM()
    if not settings.openai_api_key:
        raise LLMError("OPENAI_API_KEY no está configurada.")
    return OpenAILLM(settings.openai_api_key, settings.openai_model)
