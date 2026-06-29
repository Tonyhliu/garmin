# Garmin → AI sync

Pulls your own Garmin Connect data (activities + sleep, HRV, resting HR, body
battery, stress, training readiness) into a `garmin/` folder of plain-English
Markdown notes your AI coach can read. Built on the open-source
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) library.

This setup uses **Path A: GitHub Actions** (runs in the cloud every morning) with
the **Markdown files** sink. The workflow commits the refreshed `garmin/` folder
back into this repo so the data persists.

## Files

- `sync_garmin.py` — the pull script (read-only; never writes to Garmin).
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

### 3. Push this repo to GitHub, then add the secret

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret             | Value                                  |
| ------------------ | -------------------------------------- |
| `GARMIN_TOKEN_B64` | the base64 bundle printed by `--login` |

### 4. Run the workflow once

**Actions** tab → **Garmin sync** → **Run workflow**. Confirm a green run and that
a `garmin/` folder appears in the repo. After that it runs every morning on its own
(06:17 UTC — edit the `cron` in the workflow to change the time).

## Point your AI coach at the data

Have it read the `garmin/` folder:

```
garmin/
  daily/2026-06-28.md           # one wellness note per day
  activities/2026-06-28-...md    # one note per workout
  data.json                     # full machine-readable store
```

## Run it manually anytime

```bash
.venv/bin/python sync_garmin.py --days 7 --sink files --out ./garmin
```

## Maintenance

- If the yearly token expires or your password changes: re-run `--login` and
  update the `GARMIN_TOKEN_B64` secret.
- If Garmin changes their login and it breaks: `pip install -U garminconnect`,
  then re-run `--login`.
