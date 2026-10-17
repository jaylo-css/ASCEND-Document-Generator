"""Google Gemini provider (REST API, JSON mode, accepts images)."""
from __future__ import annotations

import base64
import os

import httpx

from .base import AIProvider, ExtractionResult, ImageInput, ProviderError
from .prompt import parse_json, system_prompt

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        if not self.api_key:
            raise ProviderError("GEMINI_API_KEY is not set")

    def build_request(self, *, tpl, values, skipped, missing, message, images, history) -> dict:
        contents = []
        for turn in history[-10:]:
            role = "model" if turn.get("role") == "assistant" else "user"
            if turn.get("text"):
                contents.append({"role": role, "parts": [{"text": turn["text"]}]})
        parts = [{"text": message or "(no text, see attached images)"}]
        for img in images:
            src = f", {img.source}" if img.source else ""
            parts.append({"text": f"[image id: {img.id}, file name: {img.name}{src}]"})
            parts.append({"inline_data": {"mime_type": img.mime, "data": base64.b64encode(img.data).decode()}})
        contents.append({"role": "user", "parts": parts})
        return {
            "systemInstruction": {"parts": [{"text": system_prompt(tpl, values, skipped, missing, [i.id for i in images])}]},
            "contents": contents,
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
        }

    def extract(self, *, tpl, values, skipped, missing, message, images: list[ImageInput], history) -> ExtractionResult:
        body = self.build_request(tpl=tpl, values=values, skipped=skipped, missing=missing,
                                  message=message, images=images, history=history)
        try:
            r = httpx.post(API.format(model=self.model), json=body, timeout=90,
                           headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Gemini: {e}") from e
        if r.status_code != 200:
            if "API_KEY_INVALID" in r.text or r.status_code in (401, 403):
                raise ProviderError("Gemini rejected the API key. Check it under the key button at the top right.")
            if r.status_code == 429:
                raise ProviderError("This Gemini API key has hit its usage limit. Wait a minute, or use another key.")
            raise ProviderError(f"Gemini returned {r.status_code}: {r.text[:300]}")
        try:
            text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
            data = parse_json(text)
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"Unexpected Gemini response: {e}") from e
        return ExtractionResult(updates=data.get("updates") or {}, reply=str(data.get("reply") or ""))
