"""
LLM provider factory. The system is fully functional with NO LLM (rule-based
intent parsing + template explanations); an LLM adds free-form understanding,
the ReAct assistant and natural explanations.

MODEL_PROVIDER = auto | google | anthropic | openai | ollama | none
  auto picks the first provider whose key is set: GOOGLE_API_KEY, ANTHROPIC_API_KEY,
  OPENAI_API_KEY, OLLAMA_MODEL; otherwise runs offline.
MODEL_NAME overrides the default model of the chosen provider.
"""
from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

from ttagent.config import ROOT, settings

load_dotenv(ROOT / ".env")

DEFAULTS = {"google": "gemini-3.6-flash", "anthropic": "claude-sonnet-4-6", "openai": "gpt-4.1-mini", "ollama": "llama3.1"}


def resolve_provider() -> str:
    p = os.getenv("MODEL_PROVIDER") or settings()["agent"].get("model_provider", "auto")
    p = p.lower()
    if p != "auto":
        return p
    if os.getenv("GOOGLE_API_KEY"):
        return "google"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("OLLAMA_MODEL"):
        return "ollama"
    return "none"


def build_llm() -> Any | None:
    provider = resolve_provider()
    name = os.getenv("MODEL_NAME") or (os.getenv("OLLAMA_MODEL") if provider == "ollama" else None) or DEFAULTS.get(provider)
    try:
        if provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(model=name, temperature=0)
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model=name, temperature=0)
        if provider == "openai":
            from langchain_openai import ChatOpenAI  # optional dependency
            return ChatOpenAI(model=name, temperature=0)
        if provider == "ollama":
            from langchain_ollama import ChatOllama  # optional dependency
            return ChatOllama(model=name, temperature=0)
    except ImportError as e:
        print(f"[llm] provider {provider!r} not installed ({e}); running offline")
    return None


def describe() -> str:
    p = resolve_provider()
    return "offline (rule-based)" if p == "none" else f"{p}:{os.getenv('MODEL_NAME') or DEFAULTS.get(p)}"


def text_of(message: Any) -> str:
    """Plain text from an AIMessage whose content may be a str or a list of blocks (Gemini/Anthropic)."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)
