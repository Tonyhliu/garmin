#!/usr/bin/env python3
"""Adaptive taper/training plan — a day-by-day schedule to the goal race, cached on disk.

`ensure_plan()` returns a plan covering today → race date, regenerating it (via one Claude
call with structured output) only when needed: missing, race changed, exhausted, a week
old, or forced with --replan. The daily coach reads `tomorrow_workout()` from it and adapts
the prescribed session to that day's recovery.

Stored at <out>/plan.json (committed by the workflow so it persists between runs).
Fail-soft: returns None / the stale plan if no API key / SDK / the call fails.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

MODEL = "claude-opus-4-8"
REPLAN_AFTER_DAYS = 7  # weekly re-plan so it adapts to how training actually went

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal_marathon": {"type": "string"},
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "phase": {"type": "string"},
                    "type": {"type": "string"},
                    "detail": {"type": "string"},
                    "distance_km": {"type": "number"},
                },
                "required": ["date", "phase", "type", "detail", "distance_km"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal_marathon", "days"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are an expert marathon coach building a concrete day-by-day training plan for one
athlete, ending on race day. Output ONLY the structured plan.

Rules:
- One entry per calendar day from the given start date through race day (inclusive).
- Each day: date (YYYY-MM-DD), phase (e.g. "Peak", "Taper", "Race week", "Race day"),
  type (e.g. "Easy run", "Long run", "Intervals", "Tempo", "Recovery run", "Rest",
  "Cross-train", "Race"), detail (one concise line with effort/pace cues), distance_km
  (0 for rest/cross-train).
- Structure a proper marathon taper: peak volume early, the taper typically begins ~2-3
  weeks out, with the last long run ~2-3 weeks before race day, reduced volume but some
  intensity retained in race week, and easy/rest in the final 2-3 days.
- Respect the athlete's recent weekly volume — do not jump it dramatically.
- The race-day entry is the marathon itself with a goal-pace note.
- Set goal_marathon to the goal finish time you are planning around.
"""


def _race_summary(race, fitness, series, today):
    """Lazy import to avoid a circular dependency (report.py imports nothing from here,
    but coach/report share helpers we reuse)."""
    from report import dig, fnum

    days_to_race = (date.fromisoformat(race["date"]) - today).days
    lines = [
        f"Today: {today.isoformat()}.",
        f"Goal race: {race['name']} on {race['date']} ({days_to_race} days away).",
        f"Plan should start {(today + timedelta(days=1)).isoformat()} (tomorrow) "
        f"and run through {race['date']}.",
    ]

    goal = os.getenv("GARMIN_GOAL_TIME")
    if goal:
        lines.append(f"Athlete's goal finish time: {goal}.")

    fitness = fitness or {}
    vo2 = dig(fitness, "max_metrics.0.generic.vo2MaxValue", "max_metrics.generic.vo2MaxValue")
    if vo2 is not None:
        lines.append(f"VO2 max: {fnum(vo2, 1)}.")
    marathon = dig(fitness, "race_predictions.timeMarathon", "race_predictions.0.timeMarathon")
    if marathon:
        h, rem = divmod(int(marathon), 3600)
        m, _ = divmod(rem, 60)
        lines.append(f"Garmin-predicted marathon: ~{h}h{m:02d}m.")

    # Recent weekly volume from the trend window's workouts.
    workouts = (series or {}).get("workouts") or []
    total_km = sum((a.get("distance") or 0) for a in workouts) / 1000.0
    window_days = len((series or {}).get("dates") or []) or 14
    weekly_km = total_km / max(window_days / 7.0, 1.0)
    lines.append(f"Recent running volume: ~{weekly_km:.0f} km/week "
                 f"({len(workouts)} runs in the last {window_days} days).")
    return "\n".join(lines)


def generate_plan(race, fitness, series, today=None):
    """One Claude call → validated day-by-day plan dict, or None (logged) on failure."""
    today = today or date.today()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Plan skipped: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        return None
    try:
        import anthropic
    except ImportError:
        print("Plan skipped: anthropic SDK not installed.", file=sys.stderr)
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _race_summary(race, fitness, series, today)}],
            output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        data = json.loads(text)
        return {
            "generated": today.isoformat(),
            "race": {"name": race["name"], "date": race["date"]},
            "goal_marathon": data.get("goal_marathon", ""),
            "days": data.get("days", []),
        }
    except Exception as exc:  # noqa: BLE001 - never break the digest over planning
        print(f"Plan skipped: {exc}", file=sys.stderr)
        return None


def _needs_regen(plan, race, today) -> bool:
    if not plan or not plan.get("days"):
        return True
    if (plan.get("race") or {}).get("date") != race["date"]:
        return True
    try:
        if (today - date.fromisoformat(plan["generated"])).days >= REPLAN_AFTER_DAYS:
            return True
    except (ValueError, KeyError, TypeError):
        return True
    last = plan["days"][-1].get("date", "")
    if last and last < today.isoformat():  # plan exhausted
        return True
    return False


def ensure_plan(out, race, fitness, series, replan=False, today=None):
    """Load the cached plan, regenerating only when stale/missing. Writes <out>/plan.json."""
    today = today or date.today()
    path = Path(out) / "plan.json"

    existing = None
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None

    if not replan and not _needs_regen(existing, race, today):
        return existing

    fresh = generate_plan(race, fitness, series, today)
    if fresh:
        path.write_text(json.dumps(fresh, indent=2, ensure_ascii=False))
        print(f"Wrote {path} ({len(fresh['days'])} days).", file=sys.stderr)
        return fresh
    return existing  # fail-soft: keep the stale plan rather than nothing


def tomorrow_workout(plan, today=None):
    """The plan entry for tomorrow, or None."""
    if not plan or not plan.get("days"):
        return None
    today = today or date.today()
    target = (today + timedelta(days=1)).isoformat()
    return next((d for d in plan["days"] if d.get("date") == target), None)


def upcoming(plan, today=None, n=7):
    """The next `n` plan entries from today onward (for the dashboard/email week view)."""
    if not plan or not plan.get("days"):
        return []
    today = today or date.today()
    iso = today.isoformat()
    return [d for d in plan["days"] if d.get("date", "") >= iso][:n]
