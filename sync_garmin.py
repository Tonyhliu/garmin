#!/usr/bin/env python3
"""Pull your own Garmin Connect data into Markdown notes (or your own database).

A thin wrapper around the open-source python-garminconnect library:
    https://github.com/cyberjunky/python-garminconnect

Modes:
    --login        One-time interactive login (email/password + optional 2FA).
                   Saves a reusable token locally and prints a base64 token
                   bundle for cloud automation (GitHub Actions, etc.).
    (default)      Fetch the last --days of activities + wellness and write
                   them to the chosen --sink.

Auth resolution order for normal runs:
    1. GARMIN_TOKEN_B64 env var (the base64 bundle from --login) — used by CI.
    2. Local token store directory (default ~/.garminconnect, or $GARMINTOKENS).

This script is read-only: it never writes anything back to your Garmin account.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import garminconnect
except ImportError:
    sys.exit(
        "Missing dependency. Install it first:\n"
        "    pip install -r requirements.txt\n"
        "(or: pip install garminconnect)"
    )

DEFAULT_TOKENSTORE = os.getenv("GARMINTOKENS") or os.path.expanduser("~/.garminconnect")


# --------------------------------------------------------------------------- #
# Small helpers for digging values out of Garmin's (deeply nested) responses.
# --------------------------------------------------------------------------- #
def dig(obj, *paths, default=None):
    """Return the first present, non-None value among several key paths.

    Each path is a dot-separated string of dict keys, e.g. "dailySleepDTO.sleepTimeSeconds".
    """
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, list):
                try:
                    cur = cur[int(key)]
                except (ValueError, IndexError):
                    ok = False
                    break
            elif isinstance(cur, dict) and key in cur and cur[key] is not None:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def fnum(value, ndigits=0):
    """Format a number, dropping a trailing .0 when ndigits==0; None -> None."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if ndigits == 0:
        return str(int(round(num)))
    return f"{num:.{ndigits}f}"


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", (text or "").strip().lower())
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "activity"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _token_client(garmin):
    """The underlying garth/token client. Renamed across library versions."""
    return getattr(garmin, "garth", None) or garmin.client


def do_login(tokenstore: str) -> int:
    """Interactive one-time login. Saves token + prints a base64 bundle."""
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit(
            "Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables first:\n"
            '    export GARMIN_EMAIL="you@example.com"\n'
            '    export GARMIN_PASSWORD="your-password"'
        )

    def prompt_mfa() -> str:
        return input("Enter the Garmin 2FA / MFA code sent to you: ").strip()

    print(f"Logging in as {email} ...", file=sys.stderr)
    garmin = garminconnect.Garmin(
        email=email, password=password, prompt_mfa=prompt_mfa
    )
    garmin.login()

    # Persist the token directory so future local runs need no password.
    Path(tokenstore).expanduser().mkdir(parents=True, exist_ok=True)
    _token_client(garmin).dump(str(Path(tokenstore).expanduser()))
    print(f"\nToken saved to {tokenstore} (good for ~1 year).", file=sys.stderr)

    # Print the portable base64 bundle for cloud automation (Path A).
    bundle = _token_client(garmin).dumps()
    print(
        "\n=== GARMIN_TOKEN_B64 (copy everything between the lines) ===\n"
        f"{bundle}\n"
        "=== end GARMIN_TOKEN_B64 ===",
    )
    print(
        "\nStore that as the GARMIN_TOKEN_B64 secret if you use GitHub Actions.",
        file=sys.stderr,
    )
    return 0


def connect(tokenstore: str):
    """Authenticate for a normal run using a saved token (no password)."""
    garmin = garminconnect.Garmin()

    token_b64 = os.getenv("GARMIN_TOKEN_B64")
    if token_b64:
        # login() accepts the base64 string directly (it detects len > 512).
        garmin.login(token_b64.strip())
        return garmin

    store = str(Path(tokenstore).expanduser())
    if not os.path.isdir(store):
        sys.exit(
            f"No saved token found at {store} and GARMIN_TOKEN_B64 is not set.\n"
            "Run a one-time login first:\n"
            "    python sync_garmin.py --login"
        )
    garmin.login(store)
    return garmin


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_wellness_day(garmin, day: str) -> dict:
    """Best-effort pull of all wellness metrics for one YYYY-MM-DD date.

    Each sub-call is wrapped so a single missing/unsupported metric (varies by
    device and by whether the watch was worn) never aborts the whole day.
    """

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - metric availability varies
            print(f"  (skip {fn.__name__} for {day}: {exc})", file=sys.stderr)
            return None

    return {
        "date": day,
        "summary": safe(garmin.get_user_summary, day),
        "sleep": safe(garmin.get_sleep_data, day),
        "hrv": safe(garmin.get_hrv_data, day),
        "stress": safe(garmin.get_stress_data, day),
        "training_readiness": safe(garmin.get_training_readiness, day),
    }


def fetch_fitness(garmin, day: str) -> dict:
    """Best-effort current fitness snapshot (VO2max, training status, race predictor, …).

    These are 'where am I now' metrics rather than per-day history, so we grab one
    snapshot for the latest date. Availability varies by device and recent activity.
    """

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - metric availability varies
            print(f"  (skip {fn.__name__}: {exc})", file=sys.stderr)
            return None

    return {
        "date": day,
        "max_metrics": safe(garmin.get_max_metrics, day),          # VO2 max
        "fitness_age": safe(garmin.get_fitnessage_data, day),
        "training_status": safe(garmin.get_training_status, day),  # status + acute load
        "race_predictions": safe(garmin.get_race_predictions),     # 5K/10K/half/marathon
        "endurance_score": safe(garmin.get_endurance_score, day),
        "hill_score": safe(garmin.get_hill_score, day),
    }


def fetch(garmin, days: int) -> dict:
    today = date.today()
    start = today - timedelta(days=days - 1)
    start_s, end_s = start.isoformat(), today.isoformat()

    print(f"Fetching activities {start_s} .. {end_s} ...", file=sys.stderr)
    activities = garmin.get_activities_by_date(start_s, end_s) or []

    wellness = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        print(f"Fetching wellness {day} ...", file=sys.stderr)
        wellness.append(fetch_wellness_day(garmin, day))

    print(f"Fetching fitness snapshot {end_s} ...", file=sys.stderr)
    fitness = fetch_fitness(garmin, end_s)

    return {"activities": activities, "wellness": wellness, "fitness": fitness}


# --------------------------------------------------------------------------- #
# Rendering: plain-English Markdown
# --------------------------------------------------------------------------- #
def render_daily_note(w: dict) -> str:
    day = w["date"]
    summary = w.get("summary") or {}
    sleep = w.get("sleep") or {}
    hrv = w.get("hrv") or {}
    stress = w.get("stress") or {}
    readiness = w.get("training_readiness") or []
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}

    lines = [f"# Garmin wellness {day}"]

    rhr = dig(summary, "restingHeartRate")
    if rhr is not None:
        lines.append(f"- Resting HR: {fnum(rhr)} bpm")

    hrv_avg = dig(hrv, "hrvSummary.lastNightAvg", "lastNightAvg")
    if hrv_avg is not None:
        lines.append(f"- HRV (overnight): {fnum(hrv_avg)} ms")

    sleep_secs = dig(sleep, "dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds")
    sleep_score = dig(
        sleep,
        "dailySleepDTO.sleepScores.overall.value",
        "sleepScores.overall.value",
    )
    if sleep_secs:
        hours = fnum(sleep_secs / 3600.0, 1)
        if sleep_score is not None:
            lines.append(f"- Sleep: {hours} h (score {fnum(sleep_score)})")
        else:
            lines.append(f"- Sleep: {hours} h")

    bb_low = dig(summary, "bodyBatteryLowestValue")
    bb_high = dig(summary, "bodyBatteryHighestValue")
    if bb_low is not None and bb_high is not None:
        lines.append(f"- Body battery: {fnum(bb_low)} -> {fnum(bb_high)}")

    stress_avg = dig(summary, "averageStressLevel", default=dig(stress, "avgStressLevel"))
    if stress_avg is not None and stress_avg >= 0:
        lines.append(f"- Stress (avg): {fnum(stress_avg)}")

    steps = dig(summary, "totalSteps")
    if steps is not None:
        lines.append(f"- Steps: {fnum(steps)}")

    tr_score = dig(readiness, "score")
    if tr_score is not None:
        lines.append(f"- Training readiness: {fnum(tr_score)}")

    return "\n".join(lines) + "\n"


def render_activity_note(a: dict) -> str:
    name = a.get("activityName") or "Activity"
    atype = dig(a, "activityType.typeKey", default="activity")
    start = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
    day = start[:10] if start else ""

    lines = [f"# {name} — {day}".rstrip(" —"), f"- Type: {atype}"]
    if start:
        lines.append(f"- Start: {start}")

    dist = a.get("distance")
    if dist:
        lines.append(f"- Distance: {fnum(dist / 1000.0, 2)} km")

    dur = a.get("duration")
    if dur:
        total = int(dur)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        pretty = (f"{h}h " if h else "") + f"{m}m {s}s"
        lines.append(f"- Duration: {pretty}")

    avg_hr, max_hr = a.get("averageHR"), a.get("maxHR")
    if avg_hr:
        suffix = f" (max {fnum(max_hr)})" if max_hr else ""
        lines.append(f"- Avg HR: {fnum(avg_hr)} bpm{suffix}")

    cal = a.get("calories")
    if cal:
        lines.append(f"- Calories: {fnum(cal)}")

    elev = a.get("elevationGain")
    if elev:
        lines.append(f"- Elevation gain: {fnum(elev)} m")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #
def sink_files(payload: dict, out: str) -> None:
    out_dir = Path(out)
    (out_dir / "daily").mkdir(parents=True, exist_ok=True)
    (out_dir / "activities").mkdir(parents=True, exist_ok=True)

    # 1) Human/AI-readable Markdown notes.
    for w in payload["wellness"]:
        note = render_daily_note(w)
        # Skip empty days (header only) so we don't litter the folder.
        if note.count("\n") > 1:
            (out_dir / "daily" / f"{w['date']}.md").write_text(note)

    for a in payload["activities"]:
        start = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
        day = start[:10] if start else "undated"
        name = slugify(a.get("activityName") or dig(a, "activityType.typeKey", default="activity"))
        aid = a.get("activityId", "")
        fname = f"{day}-{name}-{aid}.md".replace("--", "-")
        (out_dir / "activities" / fname).write_text(render_activity_note(a))

    # 2) The full machine-readable store, merged with any prior runs.
    store_path = out_dir / "data.json"
    store = {"updated": None, "wellness": {}, "activities": {}}
    if store_path.exists():
        try:
            store = json.loads(store_path.read_text())
            store.setdefault("wellness", {})
            store.setdefault("activities", {})
        except (json.JSONDecodeError, OSError):
            pass

    for w in payload["wellness"]:
        store["wellness"][w["date"]] = w
    for a in payload["activities"]:
        store["activities"][str(a.get("activityId", a.get("startTimeLocal", "")))] = a
    if payload.get("fitness"):
        store["fitness"] = payload["fitness"]  # latest snapshot, overwritten each run
    store["updated"] = datetime.now().isoformat(timespec="seconds")

    store_path.write_text(json.dumps(store, indent=2, ensure_ascii=False))
    print(
        f"Wrote {len(payload['wellness'])} daily + {len(payload['activities'])} "
        f"activity notes to {out_dir}/",
        file=sys.stderr,
    )


def sink_supabase(payload: dict) -> None:
    import requests  # local import: only needed for this sink

    url = os.getenv("GARMIN_INGEST_URL")
    secret = os.getenv("GARMIN_INGEST_SECRET") or os.getenv("SESSION_LOG_SECRET")
    if not url:
        sys.exit("Set GARMIN_INGEST_URL to use --sink supabase.")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    print(f"POSTed payload to {url} (HTTP {resp.status_code}).", file=sys.stderr)


def sink_stdout(payload: dict) -> None:
    """Dry-run preview."""
    for w in payload["wellness"]:
        print(render_daily_note(w))
    for a in payload["activities"]:
        print(render_activity_note(a))

    fit = payload.get("fitness") or {}
    if fit:
        vo2 = dig(fit, "max_metrics.0.generic.vo2MaxValue", "max_metrics.generic.vo2MaxValue")
        marathon = dig(fit, "race_predictions.timeMarathon", "race_predictions.0.timeMarathon")
        status = dig(fit, "training_status.latestTrainingStatus",
                     "training_status.mostRecentTrainingStatus.latestTrainingStatusData")
        print(f"# Fitness snapshot {fit.get('date','')}")
        if vo2 is not None:
            print(f"- VO2 max: {fnum(vo2, 1)}")
        if marathon:
            h, rem = divmod(int(marathon), 3600)
            m, s = divmod(rem, 60)
            print(f"- Predicted marathon: {h}h{m:02d}m{s:02d}s")
        if status:
            print(f"- Training status: {status}")
        print()
    print(
        f"\n[dry-run] {len(payload['wellness'])} daily + "
        f"{len(payload['activities'])} activities. Nothing written.",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull Garmin Connect data into Markdown notes or a database."
    )
    parser.add_argument(
        "--login", action="store_true",
        help="One-time interactive login; saves a token and prints a base64 bundle.",
    )
    parser.add_argument(
        "--days", type=int, default=3,
        help="How many days back to fetch (default: 3).",
    )
    parser.add_argument(
        "--sink", choices=["files", "supabase"], default="files",
        help="Where to write the data (default: files).",
    )
    parser.add_argument(
        "--out", default="./garmin",
        help="Output folder for --sink files (default: ./garmin).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the data instead of writing it anywhere.",
    )
    parser.add_argument(
        "--tokenstore", default=DEFAULT_TOKENSTORE,
        help=f"Local token directory (default: {DEFAULT_TOKENSTORE}).",
    )
    args = parser.parse_args()

    if args.login:
        return do_login(args.tokenstore)

    if args.days < 1:
        sys.exit("--days must be >= 1")

    garmin = connect(args.tokenstore)
    payload = fetch(garmin, args.days)

    if args.dry_run:
        sink_stdout(payload)
    elif args.sink == "files":
        sink_files(payload, args.out)
    elif args.sink == "supabase":
        sink_supabase(payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
