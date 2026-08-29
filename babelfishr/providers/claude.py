"""Optional cloud translation via the Claude API.

This is the only component that can send anything off the machine, and it sends
**transcript text only** - never audio.  It is opt-in: without an API key
configured the engine reports itself unavailable and BabelFishR runs entirely
locally.

Why it is worth offering: radio traffic is short, idiomatic, full of callsigns
and procedure words, and often code-switches mid-transmission.  A frontier
model handles that context far better than a general MT model, and it can be
given the operator's glossary as instructions rather than as blind string
substitution.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from .base import (EngineError, EngineUnavailable, PrivacyProfile,
                   TranslationEngine, TranslationResult)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "BABELFISHR_CLAUDE_MODEL"

SYSTEM_PROMPT = """\
You translate short radio transmissions for a monitoring operator.

Context you can rely on:
- The text is a transcript of one over-the-air voice transmission, produced by
  a speech recogniser. It may be clipped, mis-heard, or missing punctuation.
- It is typically brief, procedural, and may contain callsigns, unit
  designators, place names, phonetic-alphabet spellings, brevity codes and
  numbers spoken digit by digit.

Rules:
- Output ONLY the translation. No preamble, no notes, no quotation marks, and
  no commentary about the quality of the source.
- Preserve callsigns, unit designators, phonetic spellings and numbers exactly.
- Keep the register: terse operational speech stays terse.
- If part of the source is unintelligible, render what you can and mark the gap
  with [unintelligible]. Do not invent content to fill a gap.
- If the text is already in the target language, return it unchanged.
- Translate the content faithfully even when it is fragmentary; do not refuse
  or editorialise.\
"""


class ClaudeTranslationEngine(TranslationEngine):
    """Claude API translation. Sends transcript text only."""

    id = "claude"
    name = "Claude API (cloud)"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 max_tokens: int = 2000, effort: str = "low",
                 timeout: float = 30.0, max_retries: int = 2):
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self._explicit_key = api_key
        self.max_tokens = max_tokens
        # Translating a short transmission is not a reasoning problem; low
        # effort keeps latency and cost down without hurting quality here.
        self.effort = effort
        self.timeout = timeout
        self.max_retries = max_retries
        self.version = self.model
        self._client: Any = None
        self.privacy = PrivacyProfile(
            is_cloud=True, sends_audio=False, sends_text=True,
            destination="the Anthropic API")

    # -- availability ----------------------------------------------------
    def api_key(self) -> Optional[str]:
        if self._explicit_key:
            return self._explicit_key
        from .credentials import get_secret

        return get_secret(API_KEY_ENV)

    def sdk_installed(self) -> bool:
        try:
            import anthropic  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def available(self) -> bool:
        return self.sdk_installed() and bool(self.api_key())

    def unavailable_reason(self) -> str:
        if not self.sdk_installed():
            return ("The anthropic SDK is not installed. Install the cloud extra:\n"
                    "    pip install 'babelfishr[cloud]'")
        if not self.api_key():
            return (f"No {API_KEY_ENV} configured. Set it in the environment, in a "
                    f".env file, or in the macOS Keychain:\n"
                    f"    security add-generic-password -U -s BabelFishR "
                    f"-a {API_KEY_ENV} -w")
        return ""

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.sdk_installed():
            raise EngineUnavailable(self.unavailable_reason())
        key = self.api_key()
        if not key:
            raise EngineUnavailable(self.unavailable_reason())
        import anthropic

        self._client = anthropic.Anthropic(api_key=key, timeout=self.timeout,
                                           max_retries=self.max_retries)
        return self._client

    def close(self) -> None:
        self._client = None

    # -- translation -----------------------------------------------------
    def translate(self, text: str, target_language: str, *,
                  source_language: Optional[str] = None,
                  glossary: Optional[Dict[str, str]] = None,
                  do_not_translate: Optional[Sequence[str]] = None,
                  ) -> TranslationResult:
        if not text.strip():
            return TranslationResult(text="", source_language=source_language,
                                     target_language=target_language,
                                     engine=self.id, engine_version=self.version)
        if source_language and source_language == target_language:
            return TranslationResult(
                text=text, source_language=source_language,
                target_language=target_language, engine=self.id,
                engine_version=self.version, untranslated=True)

        client = self._get_client()
        system = self._build_system(glossary, do_not_translate)
        user = self._build_user(text, target_language, source_language)

        try:
            import anthropic
        except Exception as exc:  # noqa: BLE001 - checked above, belt and braces
            raise EngineUnavailable(self.unavailable_reason()) from exc

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as exc:
            raise EngineUnavailable(
                f"Anthropic rejected the API key: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise EngineUnavailable(
                f"API key lacks permission for {self.model}: {exc}") from exc
        except anthropic.NotFoundError as exc:
            raise EngineError(f"unknown model {self.model!r}: {exc}") from exc
        except anthropic.BadRequestError as exc:
            raise EngineError(f"request rejected: {exc}") from exc
        except anthropic.RateLimitError as exc:
            # Recoverable: the pipeline can retry this transmission later.
            raise EngineError(f"rate limited by the Anthropic API: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise EngineError(f"could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise EngineError(f"Anthropic API error {exc.status_code}: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise EngineError(
                "the model declined to translate this transmission"
                + (f" (category: {category})" if category else ""))

        translated = "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text").strip()
        if not translated:
            raise EngineError("the model returned an empty translation")

        return TranslationResult(
            text=translated, source_language=source_language,
            target_language=target_language, engine=self.id,
            engine_version=self.version)

    def _build_system(self, glossary: Optional[Dict[str, str]],
                      do_not_translate: Optional[Sequence[str]]) -> List[dict]:
        """System prompt as a cacheable block.

        The prompt and glossary are identical for every transmission in a
        session, so caching the prefix cuts the cost of a long monitoring run
        substantially.
        """
        text = SYSTEM_PROMPT
        lines: List[str] = []
        for term in do_not_translate or ():
            lines.append(f"- {term}: reproduce exactly, never translate")
        for term, preferred in (glossary or {}).items():
            if preferred:
                lines.append(f"- {term} -> {preferred}")
        if lines:
            text += ("\n\nOperator vocabulary (authoritative, overrides your own "
                     "judgement):\n" + "\n".join(lines))
        return [{"type": "text", "text": text,
                 "cache_control": {"type": "ephemeral"}}]

    def _build_user(self, text: str, target_language: str,
                    source_language: Optional[str]) -> str:
        source = (f"The source language is {source_language}."
                  if source_language else
                  "The source language is unknown; identify it yourself.")
        return (f"{source}\n"
                f"Translate into {target_language}.\n\n"
                f"Transmission transcript:\n{text}")
