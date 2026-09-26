"""Summarizer module — summarizes URLs, text, and PDFs using the LLM."""

from __future__ import annotations

import json
import logging
import tempfile
import os

import httpx
from bs4 import BeautifulSoup

from bot.services.llm import client, LLM_MODEL

logger = logging.getLogger(__name__)

SUMMARIZE_SYSTEM = (
    "You are a concise summarizer. Summarize the following content in the same language as the source. "
    "Use bullet points for key takeaways. Keep it under 500 words."
)

MAX_TEXT_LENGTH = 15000


async def summarize_text(text: str, language: str = "") -> str:
    """Summarize plain text using the LLM."""
    prompt = SUMMARIZE_SYSTEM
    if language:
        prompt = f"Summarize the following content in {language}. Use bullet points for key takeaways. Keep it under 500 words."

    truncated = text[:MAX_TEXT_LENGTH]
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": truncated},
            ],
            temperature=0.3,
        )
        summary = resp.choices[0].message.content or ""
        return json.dumps({"status": "ok", "summary": summary.strip()})
    except Exception as e:
        logger.error("Text summarization failed: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


async def summarize_url(url: str, language: str = "") -> str:
    """Fetch a URL and summarize its content."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        if not text:
            return json.dumps({"status": "error", "error": "페이지에서 텍스트를 추출할 수 없었다냥."})

        title = soup.title.string.strip() if soup.title and soup.title.string else url

        result = json.loads(await summarize_text(text, language))
        result["title"] = title
        result["url"] = url
        return json.dumps(result)
    except httpx.HTTPError as e:
        logger.error("URL fetch failed: %s", e)
        return json.dumps({"status": "error", "error": f"URL을 가져올 수 없었다냥: {e}"})
    except Exception as e:
        logger.error("URL summarization failed: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


async def summarize_pdf_file(file_path: str, language: str = "") -> str:
    """Extract text from a PDF file and summarize it."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return json.dumps({"status": "error", "error": "PyMuPDF 라이브러리가 설치되지 않았다냥."})

    try:
        doc = fitz.open(file_path)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()

        full_text = "\n".join(text_parts).strip()
        if not full_text:
            return json.dumps({"status": "error", "error": "PDF에서 텍스트를 추출할 수 없었다냥. (이미지 기반 PDF일 수 있어 nya)"})

        result = json.loads(await summarize_text(full_text, language))
        result["pages"] = len(text_parts)
        return json.dumps(result)
    except Exception as e:
        logger.error("PDF summarization failed: %s", e)
        return json.dumps({"status": "error", "error": f"PDF 처리 실패: {e}"})