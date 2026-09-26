"""Translation module — translates text using the LLM."""

from __future__ import annotations

import json
import logging

from bot.services.llm import client, LLM_MODEL

logger = logging.getLogger(__name__)

TRANSLATE_PROMPT = (
    "You are a professional translator. Translate the following text to {target_lang}. "
    "Return ONLY the translated text, nothing else. Preserve the original formatting."
)


async def translate_text(text: str, target_lang: str, source_lang: str = "") -> str:
    """Translate text to the target language using the LLM."""
    prompt = TRANSLATE_PROMPT.format(target_lang=target_lang)
    if source_lang:
        prompt = (
            f"You are a professional translator. Translate the following text "
            f"from {source_lang} to {target_lang}. "
            f"Return ONLY the translated text, nothing else. Preserve the original formatting."
        )

    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        translated = resp.choices[0].message.content or ""
        return json.dumps({
            "status": "ok",
            "translated_text": translated.strip(),
            "source_lang": source_lang or "auto",
            "target_lang": target_lang,
        })
    except Exception as e:
        logger.error("Translation failed: %s", e)
        return json.dumps({"status": "error", "error": str(e)})