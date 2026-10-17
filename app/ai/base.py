"""Pluggable AI provider interface.

A provider reads the user's unstructured input (chat text + images) and returns
field updates for the active template. To add a provider (OpenAI, Claude, a
local model), subclass AIProvider, implement extract(), and register it in
get_provider() below.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..registry import Template


@dataclass
class ImageInput:
    id: str
    mime: str
    data: bytes
    name: str = ""
    source: str = ""  # e.g. "page 2 of invitation.pdf" when rendered from an uploaded document


@dataclass
class ExtractionResult:
    updates: dict = field(default_factory=dict)  # field_id -> value (see registry.normalize_value)
    reply: str = ""                              # what the assistant says back in chat


class AIProvider(ABC):
    name = "base"

    @abstractmethod
    def extract(self, *, tpl: Template, values: dict, skipped: list[str], missing: list[dict],
                message: str, images: list[ImageInput], history: list[dict]) -> ExtractionResult:
        ...


class ProviderError(RuntimeError):
    pass


def get_provider(name: str | None = None) -> AIProvider:
    name = (name or os.environ.get("AI_PROVIDER") or "").lower()
    if not name:
        name = "gemini" if os.environ.get("GEMINI_API_KEY") else "offline"
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider()
    if name == "offline":
        from .offline import OfflineProvider
        return OfflineProvider()
    raise ValueError(f"Unknown AI_PROVIDER {name!r} (expected 'gemini' or 'offline')")
