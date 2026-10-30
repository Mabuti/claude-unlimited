"""The per-request capacity guard's numbers: GPT context windows and the
input-size estimate they are compared against. See docs/adr/0009.

Pure — no I/O, no clock, no catalogue. The windows are a HARDCODED table by
design: a codex Profile's backend window is a fact about the provider
that must not be learned from overflow errors, and LiteLLM's numbers are the
public API's, wrong for the ChatGPT backend by ~3.4x (ADR 0009). The table
is kept current by `scripts/check_gpt_windows.py`, which diffs it against the
Codex CLI's own cache of the backend listing before a push, and which stays
out of pytest because the suite must never read a user file.
"""

from __future__ import annotations

import math
import urllib.parse
from dataclasses import dataclass
from typing import Optional

# --- ChatGPT/Codex subscription backend (chatgpt.com/backend-api/codex) -----
#
# Tokens. Source: the ChatGPT backend's /models listing as cached by Codex CLI
# (~/.codex/models_cache.json, fetched 2026-10-20 by client 0.163.0) and the
# Codex CLI 0.149.0 bundled fallback catalogue. Value = (context_window,
# max_context_window). `context_window` is the backend's default budget. For
# every row except gpt-6.1-sol and gpt-6.1-terra equals the 272K long-context
# threshold in
# OpenAI's published API pricing (API prompts over 272K input tokens are priced
# at 2× input and 1.5× output for the full request, developers.openai.com model
# pages, 2026-09). This does not claim that a subscription quota meter applies
# that API-pricing multiplier.
# gpt-6.1-sol and gpt-6.1-terra list 500K windows above that threshold:
# budgeting both is an explicit user choice, and each local -272k variant is an
# explicit 272,000 cap on every profile kind, including API-key; it is not an
# assumed window. The byte estimate cannot see
# replayed reasoning, so that target is not a guarantee that every upstream
# request stays below the pricing threshold.
# `max_context_window` is the server ceiling a user can raise Codex CLI's own
# budget to; it is recorded for the freshness check only, never budgeted
# against. Changing a row is a spending decision.
CODEX_BACKEND_WINDOWS: dict[str, tuple[int, int]] = {
    "gpt-6.1-sol": (500_000, 872_000),    # cache 2026-10-20, visibility list
    "gpt-6.1-terra": (500_000, 872_000),  # cache 2026-10-30, visibility list
    "gpt-6-astra": (272_000, 872_000),      # cache 2026-09-21, visibility list
    "gpt-6-sol": (272_000, 872_000),        # cache 2026-10-14, visibility list
    "gpt-6-luna": (272_000, 872_000),       # cache 2026-10-14, visibility list
    "gpt-5.6-sol": (272_000, 872_000),      # cache 2026-09-21, visibility list
    "gpt-5.6-terra": (272_000, 872_000),    # cache 2026-09-21, visibility list
    "gpt-5.6-luna": (272_000, 872_000),     # cache 2026-09-21, visibility list
    "gpt-5.5": (272_000, 272_000),          # cache 2026-09-21, visibility list
    "gpt-5.4": (272_000, 1_000_000),        # 0.149.0 bundled catalogue
    "gpt-5.4-mini": (272_000, 272_000),     # 0.149.0 bundled catalogue
    "gpt-5.2": (272_000, 272_000),          # 0.149.0 bundled catalogue
}

# User-selectable local aliases. The alias is capped here for quota safety and
# is translated back to the real backend slug before any request, substitution
# memo, ladder lookup, or Codex conversation key leaves this process.
CODEX_MODEL_VARIANTS: dict[str, tuple[str, int]] = {
    "gpt-6.1-sol-272k": ("gpt-6.1-sol", 272_000),
    "gpt-6.1-terra-272k": ("gpt-6.1-terra", 272_000),
}

# effective_context_window_percent from the same listing: the headroom the
# Codex CLI itself keeps below the window.
CODEX_BACKEND_EFFECTIVE = 0.95

# 272K is the smallest context_window any listed slug has had, so it stays the
# honest, conservative floor for an id the table has not met yet, or for a
# listed slug whose resolved backend has no published window. The account is
# not idled on a guess, the guess is
# just made visible ("assumed") wherever the window is shown.
UNKNOWN_WINDOW = 272_000

# --- OpenAI public API (api.openai.com) — `api_key` codex Profiles -----------
#
# Max input tokens per the public model pages (developers.openai.com,
# 2026-09): the 1,050,000 context window minus the 128,000 max output.
OPENAI_API_MAX_INPUT: dict[str, int] = {
    "gpt-6-astra": 922_000,
    "gpt-5.6": 922_000,
    "gpt-5.6-sol": 922_000,
    "gpt-5.6-terra": 922_000,
    "gpt-5.6-luna": 922_000,
    "gpt-5.2": 272_000,
}

# openai_translate sends no max_output_tokens, so the reply plus reasoning is
# bounded only by the model, and reasoning at high/xhigh routinely runs tens
# of thousands of tokens against the same window. The single largest lever on
# how early a conversation leaves a codex account (226K vs 258K); chosen deliberately.
OUTPUT_RESERVE = 32_000

BACKEND_SUBSCRIPTION = "chatgpt_subscription"
BACKEND_OPENAI_API = "openai_api"
BACKEND_UNKNOWN = "unknown"   # an api_key Profile pointed at a custom base_url


@dataclass(frozen=True)
class WindowInfo:
    """One GPT id's window on one backend, and the per-request input budget
    derived from it. `assumed` is True when the resolved model/backend has no
    published window in the table and UNKNOWN_WINDOW was used instead.
    `assumption_reason` explains why the floor was used for the Settings
    summary."""
    model: str
    backend: str
    window: int
    budget: int
    assumed: bool
    assumption_reason: Optional[str] = None


def budget_for(window: int) -> int:
    """Estimated input tokens a request may carry against `window`: the CLI's
    own headroom factor, minus the reserve for the reply and its reasoning."""
    return max(0, math.floor(window * CODEX_BACKEND_EFFECTIVE) - OUTPUT_RESERVE)


# The smallest budget any codex backend can have (226.4K estimated input
# tokens): what the cheap stage-1 byte check is sized against.
MIN_BUDGET = budget_for(UNKNOWN_WINDOW)


def upstream_slug(model: Optional[str]) -> str:
    """Return the backend slug for a model id, resolving local aliases.

    Alias comparisons ignore surrounding whitespace and case. Non-alias ids
    keep their original casing after surrounding whitespace is stripped: a
    profile override is an opaque upstream id unless it is one of our aliases.
    """
    value = (model or "").strip()
    variant = CODEX_MODEL_VARIANTS.get(value.lower())
    return variant[0] if variant is not None else value


def backend_for(auth_mode: Optional[str], base_url: Optional[str]) -> str:
    """Which window table a codex Profile is served from. Subscription
    profiles (the ones add-codex-account creates) talk to the ChatGPT backend;
    an api_key profile talks to api.openai.com unless its base_url points
    somewhere else, in which case nothing can be vouched for."""
    if auth_mode != "api_key":
        return BACKEND_SUBSCRIPTION
    if not base_url or not base_url.strip():
        return BACKEND_OPENAI_API
    try:
        host = urllib.parse.urlparse(base_url).hostname
    except ValueError:
        return BACKEND_UNKNOWN
    return BACKEND_OPENAI_API if host == "api.openai.com" else BACKEND_UNKNOWN


def window_for(model: Optional[str], auth_mode: Optional[str] = None,
               base_url: Optional[str] = None) -> WindowInfo:
    """The window and budget for `model` on the backend a codex Profile with
    this auth_mode/base_url is served from. A known local variant is an explicit
    cap on every profile kind. An unknown id, unknown backend, or known slug
    without a published window for that backend is `assumed` at UNKNOWN_WINDOW;
    `assumption_reason` identifies which case applied."""
    key = (model or "").strip().lower()
    variant = CODEX_MODEL_VARIANTS.get(key)
    real_key = upstream_slug(key)
    backend = backend_for(auth_mode, base_url)
    if variant is not None:
        window = variant[1]
        return WindowInfo(key, backend, window, budget_for(window), assumed=False)
    if backend == BACKEND_SUBSCRIPTION and real_key in CODEX_BACKEND_WINDOWS:
        window = CODEX_BACKEND_WINDOWS[real_key][0]
        return WindowInfo(key, backend, window, budget_for(window), assumed=False)
    if backend == BACKEND_OPENAI_API and key in OPENAI_API_MAX_INPUT:
        window = OPENAI_API_MAX_INPUT[key]
        return WindowInfo(key, backend, window, budget_for(window), assumed=False)
    if backend == BACKEND_OPENAI_API and real_key in CODEX_BACKEND_WINDOWS:
        reason = "no_published_api_window"
    elif backend == BACKEND_SUBSCRIPTION and real_key in OPENAI_API_MAX_INPUT:
        reason = "no_published_subscription_window"
    elif backend == BACKEND_UNKNOWN:
        reason = "unknown_backend"
    else:
        reason = "unknown_model"
    return WindowInfo(key or "?", backend, UNKNOWN_WINDOW, budget_for(UNKNOWN_WINDOW),
                      assumed=True, assumption_reason=reason)


# --- the estimate -----------------------------------------------------------
#
# No tokenizer here (stdlib only). o200k runs ~3.5-4.2 bytes/token on prose
# and ~2.5-3.5 on code/JSON, and the JSON-escaped body inflates bytes further,
# which pushes a bytes-based estimate UP — the safe direction. /3.0 is the
# "err toward excluding codex" margin: ~33% over a prose-typical ratio and
# >= 0% on dense code. No further multiplier.
BYTES_PER_TOKEN = 3.0
# The Responses API charges an image by tiles: a full-detail screenshot is
# ~1-2K tokens, never its base64 length (a 1 MB PNG is ~1.4 MB of base64,
# 460K "tokens" at /3 — which would wrongly bar codex from any conversation
# holding one screenshot). So the base64 is subtracted and a flat amount
# added per image.
IMAGE_TOKENS = 1_600
# parallel_tool_calls / reasoning / include envelope and the translation's
# per-item overhead.
FIXED_OVERHEAD = 2_000

# Stage 1, the cheap short-circuit: a body under this many bytes cannot
# exceed the smallest budget even at BYTES_PER_TOKEN, so nothing is parsed.
# Exact because estimate_input_tokens() never exceeds
# ceil(len(body) / BYTES_PER_TOKEN) + FIXED_OVERHEAD (see the per-image cap).
CERTAINLY_FITS_BYTES = (MIN_BUDGET - FIXED_OVERHEAD) * int(BYTES_PER_TOKEN)


def _image_base64_lengths(parsed: Optional[dict]) -> list[int]:
    """Byte lengths of the base64 payload of every image block in an
    Anthropic messages body. Tolerant of any shape: anything that is not a
    list of messages with list content is simply not an image."""
    out: list[int] = []
    messages = (parsed or {}).get("messages")
    if not isinstance(messages, list):
        return out
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            source = block.get("source")
            data = source.get("data") if isinstance(source, dict) else None
            if isinstance(source, dict) and source.get("type") == "base64" and isinstance(data, str):
                out.append(len(data))
            # A URL image (Claude Code never sends one) has nothing to
            # subtract; its few bytes are counted as text like anything else.
    return out


def estimate_input_tokens(body: bytes, parsed: Optional[dict]) -> int:
    """Conservative (high) estimate of the input tokens `body` becomes on an
    OpenAI backend. `parsed` is the shared parse of the same body, or None
    when it could not be parsed — then nothing is subtracted, so an
    unparsable body only ever over-estimates, which excludes codex, never
    an Anthropic account (those are never sized here at all)."""
    total = len(body)
    images = _image_base64_lengths(parsed)
    text_bytes = max(0, total - sum(images))
    # Per image: the flat tile charge, capped at what its own bytes would
    # have counted for — keeps the estimate monotone in body size and makes
    # CERTAINLY_FITS_BYTES exact.
    image_tokens = sum(min(IMAGE_TOKENS, math.ceil(n / BYTES_PER_TOKEN)) for n in images)
    return math.ceil(text_bytes / BYTES_PER_TOKEN) + image_tokens + FIXED_OVERHEAD


def prompt_too_long_message(estimated_tokens: int, maximum: int) -> str:
    """The exact Anthropic wording Claude Code's reactive compaction keys
    on (2.1.278: `/prompt is too long[^0-9]*(\\d+)\\s*tokens?\\s*>\\s*(\\d+)/i`
    parses actual/limit and compacts by the gap). Byte-exact, no prefix."""
    return f"prompt is too long: {int(estimated_tokens)} tokens > {int(maximum)} maximum"
