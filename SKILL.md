---
name: whoop-obsidian
description: Build a Whoop-to-Obsidian health data pipeline: Python script that pulls sleep, recovery and strain data from the Whoop v2 API daily and writes a DataviewJS health card into an Obsidian note with 7-day rolling trend arrows. Use this skill whenever the user wants to sync Whoop data to Obsidian, automate health tracking in Obsidian, visualise wearable data in a vault, or connect any fitness API to Obsidian notes. Also use when the user mentions Whoop + Obsidian together, health dashboards in Obsidian, or automating daily note health data.
---

# Whoop → Obsidian Health Pipeline

Automates daily sync of Whoop health data into a self-contained Obsidian note with a dark-themed DataviewJS card and 7-day rolling trend arrows.

## Architecture overview

```
Windows Task Scheduler
  └── whoop_sync.bat
        ├── ping delay (wait for network)
        ├── python whoop_to_obsidian.py
        │     ├── Refresh OAuth token (requires offline scope)
        │     ├── GET /v2/recovery      → HRV, RHR, SpO2, recovery score
        │     ├── GET /v2/cycle         → strain, calories
        │     └── GET /v2/cycle/{id}/sleep → sleep stages, efficiency
        │           ├── Compute 7-day trends from history/ YAML snapshots
        │           ├── Write frontmatter + DataviewJS to Active Health.md
        │           └── Save daily snapshot to 06 Health/history/YYYY-MM-DD.yaml
        └── obsidian://open?vault=Rp   (triggers Obsidian Sync push)
```

## Critical Whoop API gotchas

These are non-obvious and not well documented. Every one caused a real bug during development.

### 1. OAuth state parameter is required
Whoop requires a `state` parameter of at least 8 characters in the auth request or it returns `invalid_state`. Generate with `secrets.token_urlsafe(16)`.

```python
import secrets
state = secrets.token_urlsafe(16)
params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": SCOPES,
    "state": state,   # required — min 8 chars
}
```

### 2. `offline` scope is required for refresh tokens
Without `offline` in the scope list, Whoop issues access tokens only (expire in 1 hour) with no refresh token. The script will work once then fail every subsequent run.

```python
SCOPES = "offline read:recovery read:sleep read:cycles"
#         ^^^^^^^ critical — must be first
```

Verify the token file contains `refresh_token` after auth. If it only has `access_token` and `expires_in`, the offline scope was missing.

### 3. `/v2/sleep` collection endpoint returns 404
The standalone sleep collection (`GET /v2/sleep`) is unreliable. Fetch sleep via the cycle endpoint instead:

```python
# DO NOT USE — returns 404
sleep = whoop_get("/v2/sleep")

# CORRECT — fetch sleep via its parent cycle
sleep = whoop_get(f"/v2/cycle/{cycle_id}/sleep")
```

### 4. Never use date-matching on Whoop timestamps
Whoop timestamps are UTC. For users in UTC+11 (AEST), the cycle end time can fall on a different calendar date than local time — causing the wrong cycle to be selected.

**Do not do this:**
```python
yesterday = date.today() - timedelta(days=1)
cycle = next(c for c in cycles if c["end"].date() == yesterday)  # WRONG
```

**Do this instead — take the most recent scored record:**
```python
recovery = next(
    (r for r in recoveries if r.get("score_state") == "SCORED"),
    None,
)
cycle = next(
    (c for c in cycles if c.get("score_state") == "SCORED"),
    None,
)
```

Results are returned in descending order so the first scored record is always last night's.

### 5. Refresh token handling must be defensive
Whoop sometimes omits the refresh token from the response. Handle this gracefully:

```python
def refresh_access_token(token_data: dict):
    if not token_data.get("refresh_token"):
        return None  # caller falls back to existing access token
    try:
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
            ...
        })
        resp.raise_for_status()
        new_token = resp.json()
        new_token["expires_at"] = datetime.now(timezone.utc).timestamp() + new_token.get("expires_in", 3600)
        save_token(new_token)
        return new_token
    except Exception:
        return None
```

## Obsidian note structure

The script writes a single fixed note (e.g. `06 Health/Active Health.md`) — not daily notes. It overwrites it each run.

```
06 Health/
├── Active Health.md        ← overwritten daily, frontmatter + DataviewJS card
└── history/
    ├── 2026-03-29.yaml     ← daily snapshots for trend computation
    ├── 2026-03-30.yaml
    └── ...
```

The `history/` folder contains `.yaml` files which Obsidian won't show in the file explorer (markdown only) — this is expected behaviour.

## Trend computation

Trends compare today's value against the 7-day rolling average of snapshots in `history/`. Minimum 3 data points required before arrows activate. RHR is inverted (lower = better).

```python
def trend(current, history, key, invert=False):
    vals = [h[key] for h in history if key in h and h[key] is not None]
    if len(vals) < 3:
        return "flat"
    avg = sum(vals) / len(vals)
    delta = (current - avg) / avg
    if delta >  0.03: return "down" if invert else "up"
    if delta < -0.03: return "up"   if invert else "down"
    return "flat"
```

## Windows Task Scheduler setup

### The batch file (critical — do not skip)

Task Scheduler must call a `.bat` file, not Python directly. The batch file handles:
1. Network wait — the script fires before DNS is ready on wake from sleep
2. stdout/stderr logging for debugging
3. Opening Obsidian after writing — required for Obsidian Sync to detect and push the file change

```bat
@echo off
cd /d C:\Users\robpi\Scripts
ping -n 30 8.8.8.8 > nul
C:\Python314\python.exe C:\Users\robpi\Scripts\whoop_to_obsidian.py >> C:\Users\robpi\whoop_sync.log 2>&1
start "" "obsidian://open?vault=Rp"
```

**Why the ping delay:** `ping -n 30` takes ~30 seconds, giving the network stack time to come up after sleep/wake. Without this, DNS resolution fails (`getaddrinfo failed`) and the script errors silently.

**Why open Obsidian:** The script writes directly to disk, bypassing Obsidian. Obsidian Sync only detects and pushes changes when Obsidian is open. Without this line, the desktop note updates but mobile never receives it.

### Task Scheduler settings
- **Program:** `C:\Python314\python.exe` (use full path from `where python`)
- **Arguments:** path to `.bat` file
- **Start in:** script directory
- **Trigger:** Daily at chosen time
- **Settings tab:** tick "Run task as soon as possible after a scheduled start is missed"
- **Triggers tab → Advanced:** add 1-minute delay as belt-and-braces network buffer

### Python path
Always use the full Python path from `where python` — Task Scheduler doesn't inherit PATH. On Windows there are often multiple Python installs; the first result from `where python` is the right one.

## DataviewJS card requirements

- Obsidian **Dataview** community plugin, with **"Enable JavaScript Queries"** toggled on
- CSS snippet `whoop-health.css` installed in `.obsidian/snippets/` and enabled
- Note: CSS snippets don't apply on Obsidian iOS — card renders unstyled but data is correct

## One-time OAuth flow

The auth flow uses a manual URL paste approach (no localhost server) since Windows firewall often blocks the callback:

1. Script opens browser to Whoop auth URL
2. User approves
3. Browser redirects to `http://localhost:8765/callback?code=XXX&state=XXX` — page won't load, that's expected
4. User copies full URL from address bar and pastes into terminal
5. Script exchanges code for token and saves to `~/.whoop_token.json`

Token file location: `C:\Users\{username}\.whoop_token.json`

## Environment variables / config

For personal use, credentials can be hardcoded directly in the script. For sharing/GitHub, use `.env`:

```python
VAULT_PATH    = os.getenv("OBSIDIAN_VAULT", r"C:\Users\robpi\Rp")
HEALTH_NOTE   = os.getenv("HEALTH_NOTE", r"06 Health\Active Health.md")
CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID", "your-id-here")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "your-secret-here")
SCOPES        = "offline read:recovery read:sleep read:cycles"
```

Never commit credentials. Add `.env` to `.gitignore`.

## Debugging checklist

| Symptom | Likely cause | Fix |
|---|---|---|
| `invalid_state` on auth | state param missing or < 8 chars | Add `secrets.token_urlsafe(16)` as state |
| No `refresh_token` in token file | `offline` scope missing | Add `offline` to SCOPES, delete token, re-auth |
| `KeyError: refresh_token` | Old token file from before offline scope fix | Delete `~/.whoop_token.json`, re-auth |
| 404 on `/v2/sleep` | Wrong endpoint | Use `/v2/cycle/{id}/sleep` instead |
| Wrong day's data pulled | UTC date matching | Remove date matching, use first scored record |
| Task runs but note doesn't update | Network not ready on wake | Add `ping -n 30 8.8.8.8 > nul` to bat file |
| Note updates on desktop but not iOS | Obsidian not open to trigger sync | Add `start "" "obsidian://open?vault=Rp"` to bat file |
| `getaddrinfo failed` in log | DNS not ready at task fire time | ping delay in bat file |
| Task Scheduler runs but nothing happens | Wrong Python path | Use full path from `where python` |
