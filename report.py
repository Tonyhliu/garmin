#!/usr/bin/env python3
"""Turn the synced Garmin data into a daily email digest + a browseable dashboard.

Reads the merged store written by sync_garmin.py (``<out>/data.json``) — it makes
NO Garmin API calls and never authenticates, so it can't touch your account.

Outputs:
    1. An HTML email digest (yesterday's snapshot + multi-day trends + workouts)
       sent via Resend (https://resend.com).
    2. A self-contained ``<out>/dashboard.html`` (Chart.js, data embedded) that the
       workflow encrypts with StatiCrypt and publishes to GitHub Pages.

Env vars:
    RESEND_API_KEY    Resend API key (required to actually send mail).
    GARMIN_MAIL_TO    Recipient address.
    GARMIN_MAIL_FROM  Sender (default: onboarding@resend.dev — Resend's no-domain
                      sender, which only delivers to your own Resend account email
                      until you verify a domain).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "onboarding@resend.dev"


# --------------------------------------------------------------------------- #
# Small helpers for digging values out of Garmin's (deeply nested) responses.
# Mirrors sync_garmin.py so the digest matches the daily notes; kept local so
# this script has no dependency on garminconnect.
# --------------------------------------------------------------------------- #
def dig(obj, *paths, default=None):
    """Return the first present, non-None value among several dot-separated paths."""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur and cur[key] is not None:
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


# --------------------------------------------------------------------------- #
# Metric extraction — one entry per tracked metric, mirroring render_daily_note.
# `get` takes a single wellness-day dict and returns a float (or None).
# `higher_better` only drives the trend arrow colour.
# --------------------------------------------------------------------------- #
def _sleep_hours(w):
    secs = dig(w.get("sleep") or {}, "dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds")
    return (secs / 3600.0) if secs else None


def _stress_avg(w):
    val = dig(
        w.get("summary") or {},
        "averageStressLevel",
        default=dig(w.get("stress") or {}, "avgStressLevel"),
    )
    return val if (val is not None and val >= 0) else None


def _readiness(w):
    tr = w.get("training_readiness") or []
    if isinstance(tr, list):
        tr = tr[0] if tr else {}
    return dig(tr, "score")


METRICS = [
    {"key": "resting_hr", "label": "Resting HR", "unit": "bpm", "nd": 0,
     "higher_better": False, "color": "#e1567c",
     "get": lambda w: dig(w.get("summary") or {}, "restingHeartRate")},
    {"key": "hrv", "label": "HRV (overnight)", "unit": "ms", "nd": 0,
     "higher_better": True, "color": "#3a86ff",
     "get": lambda w: dig(w.get("hrv") or {}, "hrvSummary.lastNightAvg", "lastNightAvg")},
    {"key": "sleep_hours", "label": "Sleep", "unit": "h", "nd": 1,
     "higher_better": True, "color": "#7048e8", "get": _sleep_hours},
    {"key": "sleep_score", "label": "Sleep score", "unit": "", "nd": 0,
     "higher_better": True, "color": "#9775fa",
     "get": lambda w: dig(w.get("sleep") or {},
                          "dailySleepDTO.sleepScores.overall.value",
                          "sleepScores.overall.value")},
    {"key": "readiness", "label": "Training readiness", "unit": "", "nd": 0,
     "higher_better": True, "color": "#2f9e44", "get": _readiness},
    {"key": "body_battery_high", "label": "Body battery (peak)", "unit": "", "nd": 0,
     "higher_better": True, "color": "#f59f00",
     "get": lambda w: dig(w.get("summary") or {}, "bodyBatteryHighestValue")},
    {"key": "stress", "label": "Stress (avg)", "unit": "", "nd": 0,
     "higher_better": False, "color": "#fa5252", "get": _stress_avg},
    {"key": "steps", "label": "Steps", "unit": "", "nd": 0,
     "higher_better": True, "color": "#1098ad",
     "get": lambda w: dig(w.get("summary") or {}, "totalSteps")},
]


# --------------------------------------------------------------------------- #
# Load + shape the data
# --------------------------------------------------------------------------- #
def load_store(out: str) -> dict:
    path = Path(out) / "data.json"
    if not path.exists():
        sys.exit(
            f"No data found at {path}. Run a sync first:\n"
            "    python sync_garmin.py --days 7 --out " + out
        )
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"Could not read {path}: {exc}")


def build_series(store: dict, days: int) -> dict:
    """Return {dates: [...], metrics: {key: [values...]}, workouts: [...]}.

    Dates are the last `days` calendar days up to today, oldest first, so a gap
    (an unworn day) shows as a None rather than silently collapsing the trend.
    """
    today = date.today()
    dates = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    wellness = store.get("wellness") or {}

    metrics = {}
    for m in METRICS:
        metrics[m["key"]] = [
            (lambda v: float(v) if v is not None else None)(m["get"](wellness.get(d) or {}))
            for d in dates
        ]

    # Workouts within the window, newest first.
    start = dates[0]
    workouts = []
    for a in (store.get("activities") or {}).values():
        start_local = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
        day = start_local[:10]
        if day and day >= start:
            workouts.append(a)
    workouts.sort(key=lambda a: a.get("startTimeLocal") or a.get("startTimeGMT") or "",
                  reverse=True)

    return {"dates": dates, "metrics": metrics, "workouts": workouts}


def _last_two(values):
    """Most recent non-None value and the previous non-None value (latest, prev)."""
    present = [v for v in values if v is not None]
    if not present:
        return None, None
    latest = present[-1]
    prev = present[-2] if len(present) > 1 else None
    return latest, prev


def _fmt_workout(a):
    name = a.get("activityName") or dig(a, "activityType.typeKey", default="Activity")
    day = (a.get("startTimeLocal") or a.get("startTimeGMT") or "")[:10]
    dist = a.get("distance")
    dist_s = f"{dist / 1000.0:.2f} km" if dist else "—"
    dur = a.get("duration")
    if dur:
        total = int(dur)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        dur_s = (f"{h}h " if h else "") + f"{m}m"
    else:
        dur_s = "—"
    hr = a.get("averageHR")
    hr_s = f"{fnum(hr)} bpm" if hr else "—"
    return {"name": name, "day": day, "dist": dist_s, "dur": dur_s, "hr": hr_s}


# --------------------------------------------------------------------------- #
# Email rendering — inline-CSS only (Gmail strips <style> and blocks images).
# --------------------------------------------------------------------------- #
def _sparkline(values, color) -> str:
    """A tiny bar chart as an HTML table; renders reliably in Gmail."""
    nums = [v for v in values if v is not None]
    if not nums:
        return '<span style="color:#aaa;font-size:12px">no data</span>'
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    cells = []
    for v in values:
        if v is None:
            inner = '<div style="height:1px;background:#eee"></div>'
        else:
            h = 4 + int(round(28 * (v - lo) / span))  # 4..32 px
            inner = f'<div style="height:{h}px;background:{color};border-radius:2px"></div>'
        cells.append(
            '<td style="vertical-align:bottom;padding:0 1px;height:34px;width:10px">'
            f"{inner}</td>"
        )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse"><tr>' + "".join(cells) + "</tr></table>"
    )


def _coaching_html(coaching: str | None) -> str:
    """Render the coach's plain-text note as an inline-styled email block."""
    if not coaching:
        return ""
    import html as _html

    body = []
    for raw in coaching.splitlines():
        line = raw.strip()
        if not line:
            continue
        safe = _html.escape(line)
        if line.startswith("- "):
            body.append(
                f'<div style="font:14px -apple-system,Segoe UI,Arial;margin:2px 0 2px 12px">'
                f'• {_html.escape(line[2:])}</div>'
            )
        else:
            body.append(f'<div style="font:14px -apple-system,Segoe UI,Arial;margin:4px 0">{safe}</div>')
    return (
        '<div style="background:#f3f0ff;border:1px solid #e5dbff;border-radius:10px;'
        'padding:14px 16px;margin:0 0 20px">'
        '<div style="font:700 14px -apple-system,Segoe UI,Arial;color:#7048e8;margin:0 0 8px">'
        '🏃 Coach</div>'
        + "".join(body) + "</div>"
    )


def render_email_html(series: dict, dashboard_url: str | None, coaching: str | None = None) -> str:
    # Most recent date that has any metric present (falls back to the last date).
    latest_day = series["dates"][-1]
    for i in range(len(series["dates"]) - 1, -1, -1):
        if any(series["metrics"][m["key"]][i] is not None for m in METRICS):
            latest_day = series["dates"][i]
            break

    rows = []
    for m in METRICS:
        values = series["metrics"][m["key"]]
        latest, prev = _last_two(values)
        if latest is None:
            continue
        val_s = fnum(latest, m["nd"])
        unit = (" " + m["unit"]) if m["unit"] else ""
        # Delta arrow vs previous reading.
        delta_html = ""
        if prev is not None and prev != latest:
            up = latest > prev
            good = up == m["higher_better"]
            arrow = "▲" if up else "▼"
            dcol = "#2f9e44" if good else "#e03131"
            delta_html = (
                f'<span style="color:{dcol};font-size:12px;margin-left:6px">'
                f"{arrow} {fnum(abs(latest - prev), m['nd'])}</span>"
            )
        rows.append(
            '<tr>'
            '<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;'
            f'font:14px -apple-system,Segoe UI,Arial">{m["label"]}</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;'
            'font:600 15px -apple-system,Segoe UI,Arial;white-space:nowrap">'
            f'{val_s}{unit}{delta_html}</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">'
            f'{_sparkline(values, m["color"])}</td>'
            "</tr>"
        )

    # Workouts.
    workouts = series["workouts"]
    if workouts:
        wrows = []
        for a in workouts[:8]:
            w = _fmt_workout(a)
            wrows.append(
                '<tr>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font:13px Arial">{w["day"]}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font:13px Arial">{w["name"]}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font:13px Arial">{w["dist"]}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font:13px Arial">{w["dur"]}</td>'
                f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font:13px Arial">{w["hr"]}</td>'
                "</tr>"
            )
        workouts_html = (
            '<h3 style="font:600 16px -apple-system,Segoe UI,Arial;margin:24px 0 8px">Workouts</h3>'
            '<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%">'
            '<tr style="text-align:left;color:#888;font:12px Arial">'
            '<th style="padding:4px 12px">Date</th><th style="padding:4px 12px">Activity</th>'
            '<th style="padding:4px 12px">Distance</th><th style="padding:4px 12px">Time</th>'
            '<th style="padding:4px 12px">Avg HR</th></tr>'
            + "".join(wrows) + "</table>"
        )
    else:
        workouts_html = (
            '<p style="font:13px Arial;color:#888;margin-top:24px">No workouts in this window.</p>'
        )

    link_html = ""
    if dashboard_url:
        link_html = (
            f'<p style="margin:24px 0 0"><a href="{dashboard_url}" '
            'style="font:14px -apple-system,Segoe UI,Arial;color:#3a86ff">'
            "Open the full dashboard →</a></p>"
        )

    return f"""\
<div style="max-width:560px;margin:0 auto;padding:8px">
  <h2 style="font:700 20px -apple-system,Segoe UI,Arial;margin:0 0 2px">Garmin daily digest</h2>
  <p style="font:13px Arial;color:#888;margin:0 0 16px">Latest data: {latest_day} · trend over {len(series['dates'])} days</p>
  {_coaching_html(coaching)}
  <table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%">
    {''.join(rows)}
  </table>
  {workouts_html}
  {link_html}
  <p style="font:11px Arial;color:#bbb;margin-top:28px">Generated from your Garmin Connect data · read-only sync</p>
</div>"""


# --------------------------------------------------------------------------- #
# Dashboard rendering — self-contained HTML (Chart.js via CDN, data embedded).
# --------------------------------------------------------------------------- #
def render_dashboard_html(series: dict, coaching: str | None = None) -> str:
    chart_meta = [
        {"key": m["key"], "label": m["label"], "unit": m["unit"], "color": m["color"]}
        for m in METRICS
    ]
    workouts = [_fmt_workout(a) for a in series["workouts"]]
    payload = {
        "dates": series["dates"],
        "metrics": series["metrics"],
        "meta": chart_meta,
        "workouts": workouts,
        "coaching": coaching or "",
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    data_json = json.dumps(payload)
    # Keep </script> in embedded JSON from prematurely closing the tag.
    data_json = data_json.replace("</", "<\\/")

    return """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Garmin dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body{font:15px -apple-system,Segoe UI,Arial,sans-serif;margin:0;background:#fafafa;color:#222}
  header{padding:20px 24px;background:#fff;border-bottom:1px solid #eee}
  h1{margin:0;font-size:20px} .sub{color:#888;font-size:13px;margin-top:4px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;padding:24px;max-width:1200px;margin:0 auto}
  .card{background:#fff;border:1px solid #eee;border-radius:10px;padding:14px}
  .card h3{margin:0 0 4px;font-size:14px} .card .now{font-size:22px;font-weight:700}
  .coach{max-width:1200px;margin:20px auto 0;padding:0 24px}
  .coach .box{background:#f3f0ff;border:1px solid #e5dbff;border-radius:12px;padding:16px 20px}
  .coach h2{margin:0 0 8px;font-size:15px;color:#7048e8}
  .coach .line{margin:3px 0} .coach .bullet{margin:3px 0 3px 14px}
  .wrap{max-width:1200px;margin:0 auto;padding:0 24px 40px}
  table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #eee;border-radius:10px;overflow:hidden}
  th,td{padding:8px 12px;text-align:left;font-size:13px;border-bottom:1px solid #f0f0f0}
  th{color:#888;font-weight:600}
</style>
</head>
<body>
<header>
  <h1>Garmin dashboard</h1>
  <div class="sub" id="sub"></div>
</header>
<div class="coach" id="coach" style="display:none"><div class="box"><h2>🏃 Coach</h2><div id="coachbody"></div></div></div>
<div class="grid" id="grid"></div>
<div class="wrap">
  <h2 style="font-size:16px">Workouts</h2>
  <table id="workouts"><thead><tr><th>Date</th><th>Activity</th><th>Distance</th><th>Time</th><th>Avg HR</th></tr></thead><tbody></tbody></table>
</div>
<script id="data" type="application/json">""" + data_json + """</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
document.getElementById('sub').textContent =
  `${D.dates[0]} – ${D.dates[D.dates.length-1]} · generated ${D.generated}`;
if (D.coaching) {
  const cb = document.getElementById('coachbody');
  for (const raw of D.coaching.split('\\n')) {
    const line = raw.trim();
    if (!line) continue;
    const el = document.createElement('div');
    if (line.startsWith('- ')) { el.className = 'bullet'; el.textContent = '• ' + line.slice(2); }
    else { el.className = 'line'; el.textContent = line; }
    cb.appendChild(el);
  }
  document.getElementById('coach').style.display = 'block';
}
const grid = document.getElementById('grid');
for (const m of D.meta) {
  const vals = D.metrics[m.key];
  const present = vals.filter(v => v !== null);
  const now = present.length ? present[present.length-1] : null;
  const card = document.createElement('div');
  card.className = 'card';
  const unit = m.unit ? ' ' + m.unit : '';
  card.innerHTML = `<h3>${m.label}</h3><div class="now">${now===null?'—':(Math.round(now*10)/10)+unit}</div><canvas></canvas>`;
  grid.appendChild(card);
  new Chart(card.querySelector('canvas'), {
    type: 'line',
    data: { labels: D.dates.map(d => d.slice(5)),
            datasets: [{ data: vals, borderColor: m.color, backgroundColor: m.color+'22',
                         tension: 0.3, spanGaps: true, fill: true, pointRadius: 2 }] },
    options: { plugins:{legend:{display:false}}, scales:{x:{ticks:{maxTicksLimit:7}}},
               maintainAspectRatio: true, aspectRatio: 2 }
  });
}
const tb = document.querySelector('#workouts tbody');
if (!D.workouts.length) tb.innerHTML = '<tr><td colspan="5" style="color:#888">No workouts in this window.</td></tr>';
for (const w of D.workouts) {
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${w.day}</td><td>${w.name}</td><td>${w.dist}</td><td>${w.dur}</td><td>${w.hr}</td>`;
  tb.appendChild(tr);
}
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Email sending (Resend)
# --------------------------------------------------------------------------- #
def send_email(html: str, subject: str) -> int:
    import requests  # already a project dependency

    api_key = os.getenv("RESEND_API_KEY")
    to = os.getenv("GARMIN_MAIL_TO")
    sender = os.getenv("GARMIN_MAIL_FROM") or DEFAULT_FROM
    if not api_key:
        sys.exit("RESEND_API_KEY is not set — cannot send email (use --no-email to skip).")
    if not to:
        sys.exit("GARMIN_MAIL_TO is not set — cannot send email (use --no-email to skip).")

    resp = requests.post(
        RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": sender, "to": [to], "subject": subject, "html": html},
        timeout=30,
    )
    if resp.status_code >= 400:
        sys.exit(f"Resend API error {resp.status_code}: {resp.text}")
    msg_id = ""
    try:
        msg_id = resp.json().get("id", "")
    except ValueError:
        pass
    print(f"Email sent to {to} via {sender} (id {msg_id}).", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Garmin email digest + dashboard from synced data.json."
    )
    parser.add_argument("--days", type=int, default=14,
                        help="Trend window in days (default: 14).")
    parser.add_argument("--out", default="./garmin",
                        help="Folder holding data.json; dashboard.html is written here.")
    parser.add_argument("--no-email", action="store_true",
                        help="Build the dashboard only; do not send any email.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print a text preview instead of sending the email.")
    parser.add_argument("--dashboard-url", default=os.getenv("DASHBOARD_URL"),
                        help="Public dashboard URL to link from the email (optional).")
    parser.add_argument("--no-coach", action="store_true",
                        help="Skip the AI coaching note (no Claude API call).")
    args = parser.parse_args()

    if args.days < 2:
        sys.exit("--days must be >= 2 (need at least two points for a trend).")

    store = load_store(args.out)
    series = build_series(store, args.days)

    # AI coaching note (fail-soft: None if disabled / no key / SDK / API error).
    coaching = None
    if not args.no_coach:
        import coach
        coaching = coach.generate_coaching(series)

    # Always (re)write the dashboard — it's the browseable UI.
    dash_path = Path(args.out) / "dashboard.html"
    dash_path.write_text(render_dashboard_html(series, coaching))
    print(f"Wrote {dash_path}", file=sys.stderr)

    email_html = render_email_html(series, args.dashboard_url, coaching)

    if args.no_email:
        print("Skipping email (--no-email).", file=sys.stderr)
        return 0

    if args.dry_run:
        print("=== email preview (text) ===")
        if coaching:
            print("--- coach ---")
            print(coaching)
            print("--- metrics ---")
        for m in METRICS:
            latest, prev = _last_two(series["metrics"][m["key"]])
            if latest is None:
                continue
            unit = (" " + m["unit"]) if m["unit"] else ""
            print(f"  {m['label']}: {fnum(latest, m['nd'])}{unit}")
        print(f"  workouts in window: {len(series['workouts'])}")
        print("\n[dry-run] Nothing sent. Dashboard written.", file=sys.stderr)
        return 0

    subject = f"Garmin digest — {series['dates'][-1]}"
    return send_email(email_html, subject)


if __name__ == "__main__":
    raise SystemExit(main())
