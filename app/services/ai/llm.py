"""
LLM access with structured outputs.

`structured(system, user, schema)` returns a validated Pydantic object. The OpenAI provider uses
Structured Outputs (`chat.completions.parse`), so responses always match the schema — no JSON
repair and no placeholder fallbacks. The fake provider (tests, local dev without keys) builds
deterministic, schema-valid content so every flow can run end to end offline. A request can carry
pictures too (`images`: JPEG files), e.g. the frames of a video to review.
"""

import base64
import logging
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol, TypeVar

import openai
from pydantic import BaseModel

from app.core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
TRANSIENT = (408, 409, 429)  # 4xx the SDK itself retries (with 5xx and connection errors): a later try may pass


class LLMError(RuntimeError):
    """The model could not produce a usable answer (refusal, truncation, provider error).

    `permanent`: sending the same request again can't help (refusal, truncation, rejected request or key);
    transient errors (timeouts, rate limits, 5xx) were already retried by the SDK and may pass later.
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class LLM(Protocol):
    def structured(
        self, system: str, user: str, schema: type[T], max_tokens: int = 8000, usage: dict | None = None,
        images: Sequence[bytes] | None = None,
    ) -> T:
        """`usage`, when given, gets the tokens the call used (`prompt_tokens`, `completion_tokens`). `images`: JPEG
        pictures the model looks at after reading `user`, each introduced as "Imagen N" (N from 0, in order)."""


def _user_content(user: str, images: Sequence[bytes] | None) -> str | list[dict]:
    """The user's message: the text, then each picture (sent whole: `detail` high) after its number."""
    if not images:
        return user
    content: list[dict] = [{"type": "text", "text": user}]
    for number, image in enumerate(images):
        url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        content += [{"type": "text", "text": f"Imagen {number}:"},
                    {"type": "image_url", "image_url": {"url": url, "detail": "high"}}]
    return content


class OpenAILLM:
    def __init__(self, api_key: str, model: str):
        self.client = openai.OpenAI(api_key=api_key, max_retries=2, timeout=180)
        self.model = model

    def structured(self, system, user, schema, max_tokens=8000, usage=None, images=None):
        try:
            completion = self.client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system}, {"role": "user", "content": _user_content(user, images)},
                ],
                response_format=schema,
                max_completion_tokens=max_tokens,
            )
        except openai.LengthFinishReasonError as exc:
            raise LLMError("La respuesta del modelo quedó incompleta; intenta con menos contenido.", permanent=True) from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise LLMError("El filtro de contenido del modelo bloqueó la respuesta.", permanent=True) from exc
        except openai.APIError as exc:
            status_code = getattr(exc, "status_code", None)
            logger.error(
                "llm_provider_error",
                extra={"error": str(exc)[:300], "model": self.model, "status": status_code,
                       "request_id": getattr(exc, "request_id", None)},
            )
            if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)):
                raise LLMError("La IA no está bien configurada en el servidor (llave o modelo).", permanent=True) from exc
            if getattr(exc, "code", None) == "context_length_exceeded":
                raise LLMError("Los materiales son demasiado largos para la IA; quita alguno.", permanent=True) from exc
            if isinstance(exc, openai.APIStatusError) and 400 <= exc.status_code < 500 and exc.status_code not in TRANSIENT:
                # e.g. a model that doesn't take pictures: say so, the admin can't tell it from the generic message
                rejected = "las imágenes" if images else "la solicitud"
                raise LLMError(f"El servicio de IA rechazó {rejected}.", permanent=True) from exc
            raise LLMError("El servicio de IA no respondió. Intenta de nuevo en unos minutos.") from exc
        message = completion.choices[0].message
        if message.refusal or message.parsed is None:
            raise LLMError("El modelo no pudo generar este contenido.", permanent=True)
        tokens = {
            "prompt_tokens": getattr(completion.usage, "prompt_tokens", None),
            "completion_tokens": getattr(completion.usage, "completion_tokens", None),
        }
        logger.info("llm_completion", extra={"model": self.model, "schema": schema.__name__, **tokens})
        if usage is not None:
            usage.update(tokens)
        return message.parsed


def available() -> bool:
    """Whether this server can ask an LLM at all (a key, or the offline fake)."""
    settings = get_settings()
    return settings.use_fake_providers or bool(settings.openai_api_key)


@lru_cache
def get_llm() -> LLM:
    settings = get_settings()
    if settings.use_fake_providers:
        from app.services.ai.fake import FakeLLM  # here, not at the top: fake -> designer -> this module

        return FakeLLM()
    if not settings.openai_api_key:
        raise LLMError("OPENAI_API_KEY no está configurada.")
    return OpenAILLM(settings.openai_api_key, settings.openai_model)
