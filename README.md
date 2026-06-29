# Garmin → AI sync

Pulls your own Garmin Connect data (activities + sleep, HRV, resting HR, body
battery, stress, training readiness) into a `garmin/` folder of plain-English
Markdown notes your AI coach can read. Built on the open-source
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) library.

This setup uses **Path A: GitHub Actions** (runs in the cloud every morning) with
the **Markdown files** sink. Each morning the workflow:

1. Pulls the latest Garmin data and commits the refreshed `garmin/` folder back
   into this repo (so the data persists).
2. Emails you a **daily digest** (yesterday's snapshot + multi-day trends +
   workouts) via [Resend](https://resend.com).
3. Publishes a **password-protected dashboard** (charts + history) to GitHub Pages.
4. Adds a short **AI coaching note** from Claude — recovery read, **tomorrow's
   workout** from an adaptive marathon plan, fitness (VO2max / race predictor), and
   wellness — at the top of the email and dashboard.

## ▶ Next steps (do these now)

Login already works and the code is pushed. To turn on the daily email + dashboard,
do these on GitHub (no terminal needed):

- [ ] **Sign up at [resend.com](https://resend.com)** (free) and create an API key.
      ⚠️ With the default sender `onboarding@resend.dev`, Resend will only deliver to
      the address that **owns the Resend account** — so sign up with the same Gmail
      you want the digest sent to.
- [ ] **Add the secrets** at **Settings → Secrets and variables → Actions** (full
      table in [step 3](#3-push-this-repo-to-github-then-add-the-secrets)):
      `RESEND_API_KEY`, `GARMIN_MAIL_TO`, `DASHBOARD_PASSWORD` (make it **long**),
      and a *variable* `GARMIN_MAIL_FROM` = `onboarding@resend.dev`.
      (`GARMIN_TOKEN_B64` is already set.)
- [ ] **(AI coach)** Add secret `ANTHROPIC_API_KEY` from
      [console.anthropic.com](https://console.anthropic.com). Optional: variables
      `GARMIN_RACE_NAME` / `GARMIN_RACE_DATE` (defaults: San Francisco Marathon,
      2026-07-26). Skip this and everything else still works — the coach just no-ops.
- [ ] **Enable Pages**: **Settings → Pages → Source = GitHub Actions**.
- [ ] **Run it**: **Actions → Garmin sync → Run workflow**. Confirm a green run, the
      email arrives, and `https://tonyhliu.github.io/garmin/` asks for your password.
- [ ] *(optional)* Backfill trend history once:
      `.venv/bin/python sync_garmin.py --days 30 --out ./garmin` then re-run the
      workflow (or `git add garmin && git commit && git push`).

After that it runs every morning at 06:17 UTC on its own.

## Files

- `sync_garmin.py` — the pull script (read-only; never writes to Garmin).
- `report.py` — builds the email digest + `dashboard.html` from `garmin/data.json`
  (makes no Garmin calls).
- `coach.py` — asks Claude for a short daily marathon/wellness note from the trend
  summary + fitness + tomorrow's planned workout (no Garmin calls; fail-soft).
- `planner.py` — generates/caches the day-by-day taper plan (`plan.json`) via Claude;
  regenerates weekly or on `--replan` (fail-soft).
- `requirements.txt` — Python dependencies.
- `.github/workflows/garmin-sync.yml` — daily cloud automation.

## One-time setup

### 1. Install + log in locally (only place you enter your password / 2FA)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GARMIN_EMAIL="you@example.com"
export GARMIN_PASSWORD="your-password"
.venv/bin/python sync_garmin.py --login
```

This saves a token to `~/.garminconnect` (good ~1 year) and prints a
**`GARMIN_TOKEN_B64`** bundle. Copy it.

### 2. Test it

```bash
.venv/bin/python sync_garmin.py --days 3 --dry-run
```

You should see your last 3 days of activities + wellness print out.

### 3. Push this repo to GitHub, then add the secrets

In the repo: **Settings → Secrets and variables → Actions**. Add these under
**Secrets** (and the one **Variable** as noted):

| Name                 | Kind     | Value                                                        |
| -------------------- | -------- | ----------------------------------------------------------- |
| `GARMIN_TOKEN_B64`   | secret   | the base64 bundle printed by `--login`                      |
| `RESEND_API_KEY`     | secret   | API key from [resend.com](https://resend.com) (free tier)   |
| `GARMIN_MAIL_TO`     | secret   | the email address to send the digest to                     |
| `DASHBOARD_PASSWORD` | secret   | a **strong** passphrase that unlocks the dashboard (see below) |
| `GARMIN_MAIL_FROM`   | variable | sender; use `onboarding@resend.dev` until you verify a domain |
| `ANTHROPIC_API_KEY`  | secret   | Claude API key for the AI coach (optional — omit and coaching is skipped) |
| `GARMIN_RACE_NAME`   | variable | optional; goal race name (default: San Francisco Marathon)   |
| `GARMIN_RACE_DATE`   | variable | optional; goal race date `YYYY-MM-DD` (default: 2026-07-26)   |
| `GARMIN_GOAL_TIME`   | variable | optional; target finish like `3:15:00` (else uses Garmin's predictor) |

**Resend note:** with the no-domain sender `onboarding@resend.dev`, Resend only
delivers to the email address that owns the Resend account. So either sign up for
Resend with the same address you put in `GARMIN_MAIL_TO`, or
[verify a domain](https://resend.com/domains) to send to any address.

### 4. Enable GitHub Pages

**Settings → Pages → Build and deployment → Source = GitHub Actions.** The dashboard
will be published at `https://<you>.github.io/garmin/`.

> ⚠️ **Why the password matters.** GitHub Pages serves a *public* URL even from a
> private repo (private Pages needs Enterprise). To keep your health data private,
> the workflow encrypts the dashboard client-side with
> [StatiCrypt](https://github.com/robinmoisson/staticrypt) before publishing — the
> public URL serves only AES-256 ciphertext, and `DASHBOARD_PASSWORD` decrypts it in
> your browser. Because the encrypted file is public, **use a long, unique
> passphrase** (a weak one could be brute-forced offline).

### 5. Run the workflow once

**Actions** tab → **Garmin sync** → **Run workflow**. Confirm a green run, then:
- a `garmin/` folder appears in the repo,
- the digest lands in your inbox,
- visiting the Pages URL prompts for the password and then shows the dashboard.

After that it runs every morning on its own (06:17 UTC — edit the `cron` in the
workflow to change the time).

## The AI coach

`coach.py` sends your recent trend summary — plus current fitness (VO2max, race
predictor, training status) and **tomorrow's planned workout** — to Claude
(`claude-opus-4-8`) and gets back a short daily note: a push/hold/easy/rest call,
a specific **"Tomorrow: …"** session adapted to your recovery, race-prep bullets, and
a wellness tip. It's race-aware: it counts down to `GARMIN_RACE_DATE` and shifts advice
as the taper approaches.

**The plan.** `planner.py` generates a day-by-day schedule from today to race day and
caches it in `garmin/plan.json` (committed, so it persists). It **auto-regenerates
weekly** (adapting to how training actually went) — or whenever the race changes, the
plan runs out, or you pass `--replan`. The coach reads tomorrow's entry and adapts it;
the email/dashboard show the upcoming days. Set `GARMIN_GOAL_TIME` (e.g. `3:15:00`) to
plan around a target pace instead of Garmin's predicted time.

- **Cost:** one Claude call per day on a small prompt — negligible.
- **Privacy:** only the *aggregated* metric summary (numbers + a workout list) is sent
  to the Claude API — never raw GPS or the full `data.json`.
- **Fail-soft:** no `ANTHROPIC_API_KEY` (or any API error) → the digest still sends,
  just without the coaching note.
- **Not medical advice** — the note says so; treat it as informational.

Test it locally:

```bash
# Preview the coaching note + metrics without sending an email
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python report.py --out ./garmin --dry-run

# Force-regenerate the training plan (plan.json)
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python report.py --out ./garmin --replan --dry-run

# Build everything but skip the Claude call entirely (no coach, no plan)
.venv/bin/python report.py --out ./garmin --no-coach --dry-run
```

## Point your AI coach at the data

Have it read the `garmin/` folder:

```
garmin/
  daily/2026-06-28.md           # one wellness note per day
  activities/2026-06-28-...md    # one note per workout
  data.json                     # full machine-readable store (incl. fitness snapshot)
  plan.json                     # day-by-day taper plan to race day
  dashboard.html                # self-contained charts UI (also published to Pages)
```

## Run it manually anytime

```bash
# Pull fresh data
.venv/bin/python sync_garmin.py --days 7 --sink files --out ./garmin

# Rebuild the dashboard only (no email)
.venv/bin/python report.py --out ./garmin --no-email

# Preview the digest without sending, then open garmin/dashboard.html
.venv/bin/python report.py --out ./garmin --dry-run
```

> On the PayPal/Zscaler network, the sync needs the cert-bundle env vars
> (`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`) — see the login notes.
> `report.py` makes no network calls except sending email, so it needs none of that.

## Maintenance

- If the yearly token expires or your password changes: re-run `--login` and
  update the `GARMIN_TOKEN_B64` secret.
- If Garmin changes their login and it breaks: `pip install -U garminconnect`,
  then re-run `--login`.
