#!/usr/bin/env python3
"""Tiny Gemini client over the stable v1beta REST endpoint (no extra SDK dependency).

The coach and planner call complete() instead of binding to a fast-moving SDK. Uses the
free-tier Gemini API: get a key at https://aistudio.google.com/apikey.

Env:
    GEMINI_API_KEY (or GOOGLE_API_KEY)   API key. Absent -> complete() returns None.
    GEMINI_MODEL                          Model id (default: gemini-2.5-flash). Override
                                          if your account exposes a different free model
                                          (e.g. gemini-3.5-flash).

Fail-soft: complete() returns None (and logs to stderr) on missing key / HTTP / parse
error, so the daily digest never breaks over the LLM call.
"""

from __future__ import annotations

import os
import sys

DEFAULT_MODEL = "gemini-2.5-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def complete(system: str, user: str, max_tokens: int = 2048,
             as_json: bool = False, timeout: int = 60):
    """Return the model's text (str), or None (logged) if unavailable."""
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        print("LLM skipped: GEMINI_API_KEY not set.", file=sys.stderr)
        return None

    import requests  # already a project dependency

    model = os.getenv("GEMINI_MODEL") or DEFAULT_MODEL
    gen_config = {"maxOutputTokens": max_tokens}
    if as_json:
        gen_config["responseMimeType"] = "application/json"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": gen_config,
    }

    try:
        resp = requests.post(
            ENDPOINT.format(model=model),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            print(f"LLM skipped: Gemini {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            return None
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            print(f"LLM skipped: Gemini returned no candidates ({str(data)[:200]}).",
                  file=sys.stderr)
            return None
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - never break the digest over the LLM
        print(f"LLM skipped: Gemini error: {exc}", file=sys.stderr)
        return None
