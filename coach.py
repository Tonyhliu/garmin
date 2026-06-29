#!/usr/bin/env python3
"""AI marathon + wellness coach — turns the Garmin trend series into a short daily note.

Reads only the in-memory ``series`` dict that report.py already builds (no Garmin calls,
no raw data.json), summarizes it, and asks Claude for concise coaching toward a goal race.

Env:
    ANTHROPIC_API_KEY   Claude API key. If unset, coaching is skipped (digest still sends).
    GARMIN_RACE_NAME    Goal race name (default: "San Francisco Marathon").
    GARMIN_RACE_DATE    Goal race date YYYY-MM-DD (default: "2026-07-26").

Fail-soft by design: generate_coaching() returns None (and logs to stderr) if the API
key or SDK is missing, or the call fails — so the daily digest never breaks over coaching.
"""

from __future__ import annotations

import os
import sys
from datetime import date

MODEL = "claude-opus-4-8"
DEFAULT_RACE_NAME = "San Francisco Marathon"
DEFAULT_RACE_DATE = "2026-07-26"

SYSTEM_PROMPT = """\
You are an experienced marathon coach and wellness advisor writing a short daily check-in
for one athlete training for a specific goal race. You are given their recent Garmin
wellness and training metrics and how many days remain until the race.

Write a brief, plain-text note (no markdown headers, no preamble) with exactly these parts:
1. A first line starting with "Today's call:" then one of: push / hold / easy / rest —
   chosen from the recovery signals (sleep, HRV, resting HR, body battery, training
   readiness, stress).
2. 2-4 short bullets (each starting with "- ") connecting the trends to marathon
   preparation, accounting for where they are in the cycle: with this many days to go,
   weigh peak vs taper (the taper typically begins ~2-3 weeks out, and easy/rest is
   correct in the final days).
3. One short wellness bullet (sleep, stress, or recovery).

Be specific to the actual numbers — cite them. Be encouraging and concrete, never generic.
Keep the whole note under ~150 words. End with one final line exactly: "Not medical advice."
"""


def race_config() -> dict:
    """Goal race from env, with sensible defaults."""
    return {
        "name": os.getenv("GARMIN_RACE_NAME") or DEFAULT_RACE_NAME,
        "date": os.getenv("GARMIN_RACE_DATE") or DEFAULT_RACE_DATE,
    }


def _days_to_race(race: dict, today: date):
    try:
        return (date.fromisoformat(race["date"]) - today).days
    except (ValueError, KeyError, TypeError):
        return None


def summarize_for_prompt(series: dict, race: dict, today: date | None = None) -> str:
    """Compact numeric summary of the trends + race context — this is all Claude sees."""
    # Imported lazily to avoid a circular import (report.py imports this module).
    from report import METRICS, fnum, _last_two, _fmt_workout

    today = today or date.today()
    lines = []

    dtr = _days_to_race(race, today)
    if dtr is not None:
        lines.append(f"Goal race: {race['name']} on {race['date']} ({dtr} days away).")
    else:
        lines.append(f"Goal race: {race['name']}.")
    lines.append(
        f"Today: {today.isoformat()}. Trend window: {len(series['dates'])} days "
        f"({series['dates'][0]} to {series['dates'][-1]})."
    )

    lines.append("\nMetrics (latest value, with min/max over the window):")
    for m in METRICS:
        vals = [v for v in series["metrics"][m["key"]] if v is not None]
        if not vals:
            continue
        latest, prev = _last_two(series["metrics"][m["key"]])
        unit = (" " + m["unit"]) if m["unit"] else ""
        trend = f", prev {fnum(prev, m['nd'])}" if (prev is not None and prev != latest) else ""
        lines.append(
            f"- {m['label']}: {fnum(latest, m['nd'])}{unit} "
            f"(min {fnum(min(vals), m['nd'])}, max {fnum(max(vals), m['nd'])}{trend})"
        )

    workouts = series.get("workouts") or []
    if workouts:
        lines.append("\nRecent workouts:")
        for a in workouts[:8]:
            w = _fmt_workout(a)
            lines.append(f"- {w['day']}: {w['name']} — {w['dist']}, {w['dur']}, avg HR {w['hr']}")
    else:
        lines.append("\nRecent workouts: none in this window.")

    return "\n".join(lines)


def generate_coaching(series: dict, race: dict | None = None):
    """Return a short coaching note from Claude, or None (logged) if unavailable."""
    race = race or race_config()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Coaching skipped: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        return None

    try:
        import anthropic  # lazy: report.py must run without this dependency installed
    except ImportError:
        print(
            "Coaching skipped: anthropic SDK not installed (pip install anthropic).",
            file=sys.stderr,
        )
        return None

    summary = summarize_for_prompt(series, race)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": summary}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - never break the digest over coaching
        print(f"Coaching skipped: Claude API error: {exc}", file=sys.stderr)
        return None
