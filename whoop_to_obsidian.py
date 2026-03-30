#!/usr/bin/env python3
"""
whoop_to_obsidian.py
--------------------
Pulls yesterday's Whoop data (sleep, recovery, cycle/strain),
computes 7-day rolling trends, and overwrites a single Obsidian
health note with fresh frontmatter + an embedded DataviewJS card.

Setup:
  1. Copy .env.example to .env and fill in your credentials
  2. Register a personal app at https://developer-dashboard.whoop.com
     Scopes needed: read:recovery, read:sleep, read:cycles
  3. Complete the one-time OAuth flow: python whoop_to_obsidian.py --auth
  4. Schedule to run daily (see README for platform instructions)

Dependencies:
  pip install requests python-dateutil pyyaml python-dotenv
"""

import argparse
import json
import os
import re
import secrets
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import yaml
from dateutil import parser as dateparser

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional — env vars can be set directly

# ─── CONFIG ────────────────────────────────────────────────────────────────────

VAULT_PATH    = os.getenv("OBSIDIAN_VAULT")
HEALTH_NOTE   = os.getenv("HEALTH_NOTE", "06 Health/Active Health.md")

CLIENT_ID     = os.getenv("WHOOP_CLIENT_ID")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET")
REDIRECT_URI  = "http://localhost:8765/callback"

TOKEN_FILE    = Path.home() / ".whoop_token.json"
TREND_WINDOW  = 7   # days of history for rolling trend computation

API_BASE  = "https://api.prod.whoop.com/developer"
AUTH_URL  = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
SCOPES    = "offline read:recovery read:sleep read:cycles"

# ─── PERSONAL BASELINES ────────────────────────────────────────────────────────
# Bar widths are calculated as (value / max) * 100%.
# Tune these to your own typical ceiling values after a few weeks of data.
BASELINES = {
    "hrv_max":       150,   # ms  — your strong HRV ceiling
    "sleep_hrs_max": 9.0,   # hrs
    "deep_mins_max": 120,   # mins
    "rem_mins_max":  120,   # mins
    "calories_max":  3500,
    "strain_max":    21,
    "resp_max":      20,
}

# ─── DATAVIEWJS CARD ───────────────────────────────────────────────────────────
# Written below frontmatter on every run.
# Requires: Dataview plugin (community) + whoop-health.css snippet enabled.

NOTE_BODY = """
```dataviewjs
const p = dv.current();
const pct = (val, max) => Math.min(100, Math.round((val / max) * 100)) + '%';
const arrow = (t) => {
  if (t === 'up')   return ['↑', 'up'];
  if (t === 'down') return ['↓', 'down'];
  return ['→', 'flat'];
};
const row = (label, value, width, dotted, trend) => {
  const [sym, cls] = arrow(trend);
  const bc = dotted ? 'wc-bar-dot' : 'wc-bar-solid';
  return `<div class="wc-row">
    <span>${label}</span>
    <span class="wc-val">${value ?? '–'}</span>
    <div class="wc-bar-bg"><div class="${bc}" style="width:${width}"></div></div>
    <span class="${cls}">${sym}</span>
  </div>`;
};
const html = `
<div class="whoop-card">
  <div class="wc-title">Active Health
    <span class="whoop-badge wc-recovery">Recovery ${p.whoop_recovery ?? '–'}%</span>
    <span class="whoop-badge wc-strain">Strain ${p.whoop_strain ?? '–'}</span>
  </div>
  <div class="wc-section">Sleep</div>
  ${row('Sleep',      p.sleep_total ?? '–',                     pct(p.sleep_hrs ?? 0, 9),    false, p.sleep_trend)}
  ${row('Deep',       p.sleep_deep  ?? '–',                     pct(p.deep_mins ?? 0, 120),  true,  p.deep_trend)}
  ${row('REM',        p.sleep_rem   ?? '–',                     pct(p.rem_mins  ?? 0, 120),  true,  p.rem_trend)}
  ${row('Efficiency', (p.sleep_efficiency ?? '–') + '%',        String(p.sleep_efficiency ?? 0) + '%', false, p.efficiency_trend)}
  <hr class="wc-divider">
  <div class="wc-section">Recovery</div>
  ${row('HRV',        (p.hrv  ?? '–') + 'ms',                  pct(p.hrv  ?? 0, 150),         false, p.hrv_trend)}
  ${row('RHR',        (p.rhr  ?? '–') + 'bpm',                 pct(110 - (p.rhr ?? 110), 60), false, p.rhr_trend)}
  ${row('SpO2',       (p.spo2 ?? '–') + '%',                   pct(p.spo2 ?? 0, 100),         true,  p.spo2_trend)}
  ${row('Resp',       (p.respiratory_rate ?? '–') + '/min',    pct(p.respiratory_rate ?? 0, 20), false, 'flat')}
  <hr class="wc-divider">
  <div class="wc-section">Activity</div>
  ${row('Calories',   (p.calories ?? 0).toLocaleString() + ' kcal', pct(p.calories ?? 0, 3500), false, p.calories_trend)}
  ${row('Strain',     p.whoop_strain ?? '–',                   pct(p.whoop_strain ?? 0, 21),  false, 'flat')}
  <hr class="wc-divider">
  <div class="wc-footer">
    Sleep consistency: <span>${p.sleep_consistency ?? '–'}%</span>
    &nbsp;|&nbsp; Skin temp: <span>${p.skin_temp ?? '–'}°C</span>
    &nbsp;|&nbsp; Updated: <span>${p.updated}</span>
  </div>
</div>`;
dv.el('div', html, {cls: ''});
```
"""

# ─── VALIDATION ────────────────────────────────────────────────────────────────

def validate_config():
    missing = []
    if not VAULT_PATH:
        missing.append("OBSIDIAN_VAULT")
    if not CLIENT_ID:
        missing.append("WHOOP_CLIENT_ID")
    if not CLIENT_SECRET:
        missing.append("WHOOP_CLIENT_SECRET")
    if missing:
        sys.exit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your values."
        )

# ─── OAUTH ─────────────────────────────────────────────────────────────────────

def save_token(token_data: dict):
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))

def load_token() -> dict | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None

def refresh_access_token(token_data: dict) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": token_data["refresh_token"],
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    resp.raise_for_status()
    new_token = resp.json()
    new_token.setdefault("refresh_token", token_data["refresh_token"])
    save_token(new_token)
    return new_token

def get_valid_token() -> str:
    token = load_token()
    if not token:
        sys.exit("No token found. Run: python whoop_to_obsidian.py --auth")
    if datetime.now(timezone.utc).timestamp() + 300 > token.get("expires_at", 0):
        token = refresh_access_token(token)
    return token["access_token"]

def run_auth_flow():
    """
    One-time OAuth — opens browser for Whoop authorisation.
    After approving, your browser will land on a localhost URL that
    won't load (that's fine). Copy the full URL from the address bar
    and paste it back here when prompted.
    """
    state = secrets.token_urlsafe(16)

    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPES,
        "state":         state,
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print("\nStep 1: Opening Whoop authorisation page in your browser...")
    print(f"        (If it doesn't open, visit this URL manually:\n         {auth_url})\n")
    webbrowser.open(auth_url)

    print("Step 2: Log in and click Authorise in the browser.")
    print("        Your browser will show a page that can't be reached — that's expected.")
    print("        The URL will look like:")
    print("        http://localhost:8765/callback?code=XXXXXX&state=XXXXXX\n")

    callback_url = input("Step 3: Copy that full URL from the address bar and paste it here:\n> ").strip()

    qs = parse_qs(urlparse(callback_url).query)

    if "error" in qs:
        sys.exit(
            f"\nWhoop returned an error: {qs['error'][0]}\n"
            f"{qs.get('error_description', ['No description'])[0]}"
        )

    returned_state = qs.get("state", [None])[0]
    if returned_state != state:
        sys.exit("\nState mismatch — possible CSRF. Please try again.")

    auth_code = qs.get("code", [None])[0]
    if not auth_code:
        sys.exit(
            "\nCouldn't find an auth code in that URL.\n"
            "Make sure you copied the full URL from the address bar after approving."
        )

    print("\nExchanging code for token...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "code":          auth_code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    resp.raise_for_status()
    token = resp.json()
    token["expires_at"] = (
        datetime.now(timezone.utc).timestamp() + token.get("expires_in", 3600)
    )
    save_token(token)
    print(f"Token saved to {TOKEN_FILE}")
    print("Auth complete — you won't need to do this again.")

# ─── API HELPERS ───────────────────────────────────────────────────────────────

def whoop_get(path: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {get_valid_token()}"},
        params=params or {},
    )
    resp.raise_for_status()
    return resp.json()

def fetch_recent(endpoint: str, days: int = 3) -> list:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return whoop_get(endpoint, {"limit": 25, "start": start}).get("records", [])

# ─── DATA EXTRACTION ───────────────────────────────────────────────────────────

def ms_to_hm(ms: int) -> str:
    m = ms // 60000
    return f"{m // 60}h{m % 60:02d}m"

def ms_to_hrs(ms: int) -> float:
    return round(ms / 3600000, 2)

def ms_to_mins(ms: int) -> int:
    return ms // 60000

def get_yesterdays_data() -> dict:
    yesterday = date.today() - timedelta(days=1)

    def on_yesterday(iso_str: str) -> bool:
        return bool(iso_str) and dateparser.parse(iso_str).date() == yesterday

    recoveries = fetch_recent("/v2/recovery")
    cycles     = fetch_recent("/v2/cycle")

    recovery = next(
        (r for r in recoveries
         if r.get("score_state") == "SCORED" and on_yesterday(r.get("created_at", ""))),
        None,
    )
    cycle = next(
        (c for c in cycles
         if c.get("score_state") == "SCORED" and on_yesterday(c.get("end", ""))),
        None,
    )

    if not recovery:
        sys.exit(
            f"No scored recovery data found for {yesterday}. "
            "Device may still be syncing — try again in 30 minutes."
        )

    # Fetch sleep via cycle endpoint (read:cycles scope)
    sleep = None
    if cycle:
        try:
            sleep = whoop_get(f"/v2/cycle/{cycle['id']}/sleep")
        except Exception:
            pass

    # Fall back to sleep collection endpoint if available (read:sleep scope)
    if not sleep:
        try:
            sleeps = fetch_recent("/v2/sleep")
            sleep = next(
                (s for s in sleeps
                 if s.get("score_state") == "SCORED"
                 and not s.get("nap", False)
                 and on_yesterday(s.get("end", ""))),
                None,
            )
        except Exception:
            pass

    if not sleep:
        sys.exit(
            f"No scored sleep data found for {yesterday}. "
            "Make sure read:sleep or read:cycles scope is enabled in your Whoop app."
        )

    rs  = recovery["score"]
    ss  = sleep["score"]
    stg = ss["stage_summary"]
    cs  = cycle["score"] if cycle else {}

    asleep_ms = stg["total_in_bed_time_milli"] - stg["total_awake_time_milli"]

    return {
        "whoop_recovery":    rs.get("recovery_score"),
        "hrv":               round(rs.get("hrv_rmssd_milli", 0), 1),
        "rhr":               rs.get("resting_heart_rate"),
        "spo2":              round(rs.get("spo2_percentage", 0), 1),
        "skin_temp":         round(rs.get("skin_temp_celsius", 0), 1),
        "sleep_total":       ms_to_hm(asleep_ms),
        "sleep_hrs":         ms_to_hrs(asleep_ms),
        "sleep_deep":        ms_to_hm(stg["total_slow_wave_sleep_time_milli"]),
        "deep_mins":         ms_to_mins(stg["total_slow_wave_sleep_time_milli"]),
        "sleep_rem":         ms_to_hm(stg["total_rem_sleep_time_milli"]),
        "rem_mins":          ms_to_mins(stg["total_rem_sleep_time_milli"]),
        "sleep_efficiency":  round(ss.get("sleep_efficiency_percentage", 0), 1),
        "sleep_consistency": round(ss.get("sleep_consistency_percentage", 0), 1),
        "respiratory_rate":  round(ss.get("respiratory_rate", 0), 1),
        "whoop_strain":      round(cs.get("strain", 0), 1) if cs else None,
        "calories":          round(cs.get("kilojoule", 0) / 4.184) if cs else None,
        "updated":           date.today().strftime("%d %b %Y"),
    }

# ─── TREND COMPUTATION ─────────────────────────────────────────────────────────

def gather_history(vault: Path, days: int, keys: list) -> list:
    """
    Read daily YAML snapshots from <vault>/06 Health/history/YYYY-MM-DD.yaml.
    Snapshots are written automatically on each successful run — trends
    build up over time without needing a daily notes structure.
    """
    history = []
    history_dir = vault / "06 Health" / "history"
    for i in range(1, days + 1):
        snap = history_dir / f"{(date.today() - timedelta(days=i)).strftime('%Y-%m-%d')}.yaml"
        if snap.exists():
            try:
                fm = yaml.safe_load(snap.read_text(encoding="utf-8")) or {}
                row = {k: fm[k] for k in keys if k in fm}
                if row:
                    history.append(row)
            except yaml.YAMLError:
                pass
    return history

def save_history_snapshot(vault: Path, data: dict):
    """Persist today's values for future trend windows."""
    history_dir = vault / "06 Health" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    snap = history_dir / f"{date.today().strftime('%Y-%m-%d')}.yaml"
    snap.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")

def trend(current, history: list, key: str, invert: bool = False) -> str:
    """
    Compare current to 7-day rolling average.
    Returns 'up', 'down', or 'flat'. Needs at least 3 data points.
    invert=True means lower is better (e.g. RHR).
    """
    if current is None:
        return "flat"
    vals = [h[key] for h in history if key in h and h[key] is not None]
    if len(vals) < 3:
        return "flat"
    avg = sum(vals) / len(vals)
    if avg == 0:
        return "flat"
    delta = (current - avg) / avg
    if delta >  0.03: return "down" if invert else "up"
    if delta < -0.03: return "up"   if invert else "down"
    return "flat"

# ─── NOTE ASSEMBLY ─────────────────────────────────────────────────────────────

def build_note(data: dict, trends: dict) -> str:
    lines = ["---"]

    def add(key, val):
        if val is not None:
            lines.append(f"{key}: {val}")

    add("whoop_recovery",    data["whoop_recovery"])
    add("whoop_strain",      data["whoop_strain"])
    lines.append(f'sleep_total: "{data["sleep_total"]}"')
    add("sleep_hrs",         data["sleep_hrs"])
    lines.append(f'sleep_deep: "{data["sleep_deep"]}"')
    add("deep_mins",         data["deep_mins"])
    lines.append(f'sleep_rem: "{data["sleep_rem"]}"')
    add("rem_mins",          data["rem_mins"])
    add("sleep_efficiency",  data["sleep_efficiency"])
    add("sleep_consistency", data["sleep_consistency"])
    add("respiratory_rate",  data["respiratory_rate"])
    add("hrv",               data["hrv"])
    add("rhr",               data["rhr"])
    add("spo2",              data["spo2"])
    add("skin_temp",         data["skin_temp"])
    add("calories",          data["calories"])
    lines.append(f'updated: "{data["updated"]}"')

    for key, val in trends.items():
        lines.append(f"{key}: {val}")

    lines.append("---")
    lines.append(NOTE_BODY)

    return "\n".join(lines)

def write_note(vault: Path, rel_path: str, content: str):
    note_path = vault / rel_path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")
    print(f"Written: {note_path}")

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Whoop data to Obsidian.")
    parser.add_argument("--auth",    action="store_true", help="Run one-time OAuth flow")
    parser.add_argument("--dry-run", action="store_true", help="Print output without writing")
    args = parser.parse_args()

    validate_config()

    if args.auth:
        run_auth_flow()
        return

    vault = Path(VAULT_PATH)
    if not vault.exists():
        sys.exit(f"Vault not found: {vault}\nCheck OBSIDIAN_VAULT in your .env file.")

    print("Fetching Whoop data...")
    data = get_yesterdays_data()

    trend_keys = ["hrv", "rhr", "sleep_hrs", "deep_mins", "rem_mins",
                  "sleep_efficiency", "spo2", "whoop_recovery", "calories"]
    history = gather_history(vault, TREND_WINDOW, trend_keys)

    trends = {
        "hrv_trend":        trend(data["hrv"],              history, "hrv"),
        "rhr_trend":        trend(data["rhr"],              history, "rhr",             invert=True),
        "sleep_trend":      trend(data["sleep_hrs"],        history, "sleep_hrs"),
        "deep_trend":       trend(data["deep_mins"],        history, "deep_mins"),
        "rem_trend":        trend(data["rem_mins"],         history, "rem_mins"),
        "efficiency_trend": trend(data["sleep_efficiency"], history, "sleep_efficiency"),
        "spo2_trend":       trend(data["spo2"],             history, "spo2"),
        "recovery_trend":   trend(data["whoop_recovery"],   history, "whoop_recovery"),
        "calories_trend":   trend(data["calories"],         history, "calories"),
    }

    note_content = build_note(data, trends)

    if args.dry_run:
        print("\n--- DRY RUN ---")
        print(note_content)
        return

    write_note(vault, HEALTH_NOTE, note_content)
    save_history_snapshot(vault, {**data, **trends})
    print("Done.")

if __name__ == "__main__":
    main()
