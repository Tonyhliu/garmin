#!/usr/bin/env python3
"""AI marathon + wellness coach — turns the Garmin trend series into a short daily note.

Reads only the in-memory ``series`` dict that report.py already builds (no Garmin calls,
no raw data.json), summarizes it, and asks the LLM for concise coaching toward a goal race.

Env (see gemini.py for the LLM key):
    GEMINI_API_KEY      Gemini key. If unset, coaching is skipped (digest still sends).
    GARMIN_RACE_NAME    Goal race name (default: "San Francisco Marathon").
    GARMIN_RACE_DATE    Goal race date YYYY-MM-DD (default: "2026-07-26").
    GARMIN_GOAL_TIME    Optional target finish time (e.g. "3:15:00").

Fail-soft by design: generate_coaching() returns None if the LLM is unavailable or the
call fails — so the daily digest never breaks over coaching.
"""

from __future__ import annotations

import os
from datetime import date

DEFAULT_RACE_NAME = "San Francisco Marathon"
DEFAULT_RACE_DATE = "2026-07-26"

SYSTEM_PROMPT = """\
You are an experienced marathon coach and wellness advisor writing a short daily check-in
for one athlete training for a specific goal race. You are given their recent Garmin
wellness and training metrics, current fitness (VO2max, race predictor, training status),
how many days remain until the race, and — when available — the workout their training
plan prescribes for tomorrow.

Write a brief, plain-text note (no markdown headers, no preamble) with exactly these parts:
1. A first line starting with "Today's call:" then one of: push / hold / easy / rest —
   chosen from the recovery signals (sleep, HRV, resting HR, body battery, training
   readiness, stress).
2. A line starting with "Tomorrow:" giving the specific workout. Start from the plan's
   prescribed session if one is provided, but ADAPT it to recovery — e.g. downgrade
   intervals/long runs to easy or rest when HRV is down, sleep was poor, or readiness is
   low; note the swap and why. If no plan is provided, prescribe a sensible session for
   where they are in the cycle.
3. 2-3 short bullets (each starting with "- ") connecting the trends and fitness to
   marathon prep and the taper (taper typically begins ~2-3 weeks out; easy/rest in the
   final days). Reference goal pace from the predictor/goal time when relevant.
4. One short wellness bullet (sleep, stress, or recovery).

Be specific to the actual numbers — cite them. Be encouraging and concrete, never generic.
Keep the whole note under ~170 words. End with one final line exactly: "Not medical advice."
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


def _fitness_lines(fitness: dict) -> list[str]:
    """Best-effort fitness extraction (paths vary by device; missing -> omitted)."""
    from report import dig, fnum

    out = []
    vo2 = dig(fitness, "max_metrics.0.generic.vo2MaxValue", "max_metrics.generic.vo2MaxValue")
    if vo2 is not None:
        out.append(f"- VO2 max: {fnum(vo2, 1)}")
    for label, key in [("5K", "time5K"), ("10K", "time10K"),
                       ("half", "timeHalfMarathon"), ("marathon", "timeMarathon")]:
        secs = dig(fitness, f"race_predictions.{key}", f"race_predictions.0.{key}")
        if secs:
            h, rem = divmod(int(secs), 3600)
            m, s = divmod(rem, 60)
            pretty = (f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s")
            out.append(f"- Predicted {label}: {pretty}")
    status = dig(fitness, "training_status.latestTrainingStatus")
    if status:
        out.append(f"- Training status: {status}")
    endurance = dig(fitness, "endurance_score.overallScore", "endurance_score.0.overallScore")
    if endurance is not None:
        out.append(f"- Endurance score: {fnum(endurance)}")
    return out


def summarize_for_prompt(series: dict, race: dict, fitness: dict | None = None,
                         planned: dict | None = None, today: date | None = None) -> str:
    """Compact numeric summary of trends + fitness + race + tomorrow's plan — all the LLM sees."""
    # Imported lazily to avoid a circular import (report.py imports this module).
    from report import METRICS, fnum, _last_two, _fmt_workout

    today = today or date.today()
    lines = []

    dtr = _days_to_race(race, today)
    if dtr is not None:
        lines.append(f"Goal race: {race['name']} on {race['date']} ({dtr} days away).")
    else:
        lines.append(f"Goal race: {race['name']}.")
    goal = os.getenv("GARMIN_GOAL_TIME")
    if goal:
        lines.append(f"Athlete's goal finish time: {goal}.")
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

    fit_lines = _fitness_lines(fitness or {})
    if fit_lines:
        lines.append("\nCurrent fitness:")
        lines.extend(fit_lines)

    workouts = series.get("workouts") or []
    if workouts:
        lines.append("\nRecent workouts:")
        for a in workouts[:8]:
            w = _fmt_workout(a)
            lines.append(f"- {w['day']}: {w['name']} — {w['dist']}, {w['dur']}, avg HR {w['hr']}")
    else:
        lines.append("\nRecent workouts: none in this window.")

    if planned:
        dist = planned.get("distance_km")
        dist_s = f", {dist} km" if dist else ""
        lines.append(
            f"\nTraining plan prescribes for tomorrow ({planned.get('date','')}): "
            f"{planned.get('type','')} — {planned.get('detail','')}{dist_s} "
            f"[phase: {planned.get('phase','')}]"
        )
    else:
        lines.append("\nTraining plan for tomorrow: none provided — prescribe one.")

    return "\n".join(lines)


def generate_coaching(series: dict, race: dict | None = None,
                      fitness: dict | None = None, planned: dict | None = None):
    """Return a short coaching note from the LLM, or None (logged) if unavailable."""
    race = race or race_config()
    summary = summarize_for_prompt(series, race, fitness=fitness, planned=planned)

    import gemini  # lazy: report.py must run even without an LLM configured
    return gemini.complete(SYSTEM_PROMPT, summary, max_tokens=2048)
