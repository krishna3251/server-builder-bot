"""AI service — OpenRouter chat completions with per-user memory and personas."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import wraps
from typing import Any

import aiofiles
import aiohttp

import config
from utils.storage import write_json_atomic

log = logging.getLogger("services.ai")

# ── Persona system prompts ────────────────────────────────────────────────────
PERSONAS: dict[str, str] = {
    "default": (
        "You are ServerBot, an intelligent and versatile Discord assistant. "
        "You have deep knowledge of Discord, servers, bots, coding, gaming, and general topics. "
        "Answer accurately, concisely, and helpfully. Use markdown formatting when it improves readability. "
        "If you don't know something, say so honestly. Be conversational but informative. "
        "Remember context from the conversation and build on previous messages."
    ),
    "mentor": (
        "You are a brilliant, patient teacher who adapts to the learner's level. "
        "Explain concepts step-by-step with real examples and analogies. "
        "Ask clarifying questions when needed. Check for understanding. "
        "Use code blocks for technical explanations. Encourage and motivate."
    ),
    "sarcastic": (
        "You are a witty, sarcastic genius who can't help but roast people — lightly. "
        "You're actually incredibly helpful despite the attitude. Use dry humour, "
        "clever wordplay, and the occasional burn, but ALWAYS answer the question properly. "
        "Think of yourself as a comedic expert — funny first, helpful always."
    ),
    "professional": (
        "You are a senior enterprise consultant. Communicate formally with precision. "
        "No jokes, slang, emojis, or casual language. Structure responses with headers, "
        "bullet points, and clear sections. Cite reasoning. Be thorough and authoritative."
    ),
    "coder": (
        "You are an expert software engineer proficient in Python, JavaScript, C++, "
        "and all major frameworks. Provide clean, well-commented code with explanations. "
        "Use best practices, proper error handling, and modern patterns. "
        "When debugging, think step-by-step through the problem."
    ),
}

# ── In-memory cache (loaded from disk on init) ───────────────────────────────
_memory: dict[str, Any] = {}
_loaded: bool = False
_memory_lock = asyncio.Lock()
_user_locks: dict[int, asyncio.Lock] = {}


def user_session(user_arg: int):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            user_id = kwargs.get("user_id", args[user_arg] if len(args) > user_arg else None)
            lock = _user_locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                return await function(*args, **kwargs)
        return wrapped
    return decorate



async def _ensure_loaded() -> None:
    async with _memory_lock:
        await _load_memory_unlocked()


async def _load_memory_unlocked() -> None:
    """Load memory from disk once."""
    global _memory, _loaded
    if _loaded:
        return
    if os.path.exists(config.MEMORY_FILE):
        try:
            async with aiofiles.open(config.MEMORY_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _memory = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            log.warning("Could not load memory file: %s", exc)
            raise RuntimeError("AI memory could not be read; refusing to overwrite it.") from exc
    _loaded = True


async def _save_memory() -> None:
    """Persist memory to disk."""
    async with _memory_lock:
        await write_json_atomic(config.MEMORY_FILE, _memory)


def _user_key(user_id: int) -> str:
    return str(user_id)


@user_session(1)
async def get_ai_response(prompt: str, user_id: int, persona: str = "default", *, use_history: bool = True) -> str:
    """Send *prompt* to OpenRouter and return the assistant reply.

    Maintains per-user conversation history (max ``config.AI_MAX_HISTORY`` exchanges).
    """
    if not config.OPENROUTER_API_KEY:
        return "⚠️ AI is unavailable. Ask an administrator to configure OPENROUTER_API_KEY."
    if use_history:
        await _ensure_loaded()
    key = _user_key(user_id)

    # Initialise user entry if missing
    if use_history and key not in _memory:
        _memory[key] = {"persona": persona, "history": []}

    user_data = _memory[key] if use_history else {"persona": persona, "history": []}
    active_persona = user_data.get("persona", persona)
    system_prompt = PERSONAS.get(active_persona, PERSONAS["default"])

    # Build message list
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(user_data["history"])
    messages.append({"role": "user", "content": prompt})

    # Call OpenRouter with retry + fallback models
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://discord.com",
        "X-Title": config.BOT_NAME,
        "Content-Type": "application/json",
    }

    # Build unique ordered model list
    seen: set[str] = set()
    models_to_try: list[str] = []
    for m in [config.AI_MODEL] + config.AI_FALLBACK_MODELS:
        if m not in seen:
            seen.add(m)
            models_to_try.append(m)

    reply: str = ""
    last_error: str = "Unknown error"

    async with aiohttp.ClientSession() as session:
        for model in models_to_try:
            payload = {"model": model, "messages": messages}
            for attempt in range(config.AI_MAX_RETRIES):
                try:
                    async with session.post(
                        config.OPENROUTER_BASE_URL, headers=headers, json=payload,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        data = await resp.json()
                        if resp.status == 200:
                            choice = data.get("choices", [{}])[0]
                            reply = choice.get("message", {}).get("content", "")
                            if reply:
                                log.info("AI response from %s (%d tokens)", model, len(reply.split()))
                                break
                            last_error = "Empty response"
                            break
                        elif resp.status == 429:
                            wait = min(2 ** attempt + 1, 8)
                            log.warning("Rate limited on %s, retry %d in %ds", model, attempt + 1, wait)
                            await asyncio.sleep(wait)
                            continue
                        elif resp.status in (404, 503):
                            last_error = data.get("error", {}).get("message", f"HTTP {resp.status}")
                            log.warning("Model %s unavailable (%d), skipping", model, resp.status)
                            break  # skip to next model immediately
                        else:
                            last_error = data.get("error", {}).get("message", f"HTTP {resp.status}")
                            log.error("OpenRouter error %d on %s: %s", resp.status, model, last_error)
                            break
                except asyncio.TimeoutError:
                    last_error = "Request timed out"
                    log.warning("Timeout on %s attempt %d", model, attempt + 1)
                    continue
                except Exception as exc:
                    last_error = str(exc)
                    log.exception("AI request failed on %s: %s", model, exc)
                    break
            if reply:
                break

    if not reply:
        log.warning("AI models exhausted: %s", last_error)
        return "⚠️ AI could not respond. Try again shortly; an administrator can check the bot logs."

    # Update history (FIFO, max AI_MAX_HISTORY messages)
    user_data["history"].append({"role": "user", "content": prompt})
    user_data["history"].append({"role": "assistant", "content": reply})
    if len(user_data["history"]) > config.AI_MAX_HISTORY * 2:
        user_data["history"] = user_data["history"][-(config.AI_MAX_HISTORY * 2) :]

    if use_history:
        await _save_memory()
    return reply


@user_session(0)
async def reset_user_memory(user_id: int) -> None:
    """Clear conversation history for a user."""
    await _ensure_loaded()
    key = _user_key(user_id)
    if key in _memory:
        _memory[key]["history"] = []
        await _save_memory()


@user_session(0)
async def set_user_persona(user_id: int, persona: str) -> None:
    """Switch the active persona for a user."""
    await _ensure_loaded()
    key = _user_key(user_id)
    if key not in _memory:
        _memory[key] = {"persona": persona, "history": []}
    else:
        _memory[key]["persona"] = persona
    await _save_memory()
