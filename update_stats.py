"""
Pulls current NFL analytics from nflverse (open data) and writes a compact
stats.json lookup keyed by gsis_id. Runs server-side (GitHub Actions), so
it isn't subject to browser CORS limits — the deployed app fetches the
resulting file from this repo's raw GitHub URL, which does serve
Access-Control-Allow-Origin: *.

Three signals, each useful even when the others are missing:
  - Season production (pts/gp/ppg) — real games played, most predictive
    once it exists, but doesn't exist yet for rookies or very early season.
  - Draft capital (round/pick) — exists the moment a player is drafted,
    a well-established predictor of opportunity even with zero NFL snaps.
  - Depth chart position (starter vs backup) — updated by teams daily,
    including during preseason, so it's live before Week 1 even happens.
"""
import csv
import io
import json
import re
import sys
from datetime import date, datetime, timezone
from urllib.request import urlopen, Request

STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
DRAFT_PICKS_URL = "https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv"
DEPTH_CHART_URL = "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv"

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K"}
DEPTH_POS_MAP = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "PK": "K"}  # depth chart uses "PK" for kicker


def norm_name(name: str) -> str:
    """Loose name normalization for matching across datasets that don't share
    a reliable ID for brand-new rookies (see load_draft_capital docstring)."""
    name = name.lower()
    name = re.sub(r"[.\']", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    name = re.sub(r"[^a-z ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def fetch_csv_text(url: str):
    req = Request(url, headers={"User-Agent": "sleeper-stats-updater/1.0"})
    try:
        with urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  fetch failed for {url}: {e}", file=sys.stderr)
        return None


def guess_season() -> int:
    today = date.today()
    return today.year if today.month >= 8 else today.year - 1


def aggregate_production(csv_text: str):
    agg = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if row.get("season_type") != "REG":
            continue
        pos = row.get("position")
        if pos not in FANTASY_POSITIONS:
            continue
        pid = row.get("player_id")
        if not pid:
            continue
        try:
            pts = float(row.get("fantasy_points_ppr") or 0)
        except ValueError:
            pts = 0.0
        d = agg.setdefault(pid, {"pts": 0.0, "games": 0, "name": "", "pos": "", "team": ""})
        d["pts"] += pts
        d["games"] += 1
        d["name"] = row.get("player_display_name", "")
        d["pos"] = pos
        d["team"] = row.get("team", "")
    out = {}
    for pid, d in agg.items():
        if d["games"] == 0:
            continue
        out[pid] = {
            "n": d["name"], "p": d["pos"], "t": d["team"],
            "pts": round(d["pts"], 1), "gp": d["games"], "ppg": round(d["pts"] / d["games"], 2),
        }
    return out


def load_draft_capital(csv_text):
    """(normalized_name, position) -> {rd, pk, yr}.

    NOTE: draft_picks.csv's own gsis_id column uses a placeholder ID for
    players who haven't taken an NFL snap yet (e.g. "LOV121782" instead of
    a real "00-00XXXXX" GSIS ID) — that placeholder never matches anything
    else. The depth chart, by contrast, already has the real GSIS ID for
    rookies the moment they're added to a team roster. So instead of joining
    by ID here, we key by name+position and match it onto already-verified
    players (from production or depth chart) afterward in main().
    """
    out = {}
    if not csv_text:
        return out
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        pos = row.get("position")
        if pos not in FANTASY_POSITIONS:
            continue
        name = row.get("pfr_player_name", "")
        if not name:
            continue
        try:
            rd = int(row["round"])
            pk = int(row["pick"])
            yr = int(row["season"])
        except (ValueError, KeyError, TypeError):
            continue
        key = (norm_name(name), pos)
        existing = out.get(key)
        if not existing or yr >= existing["yr"]:
            out[key] = {"rd": rd, "pk": pk, "yr": yr}
    return out


def load_depth_chart(csv_text):
    out = {}
    if not csv_text:
        return out
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        return out
    dts = [r["dt"] for r in rows if r.get("dt")]
    if not dts:
        return out
    latest_dt = max(dts)
    for row in rows:
        if row.get("dt") != latest_dt:
            continue
        pos_abb = row.get("pos_abb")
        pos = DEPTH_POS_MAP.get(pos_abb)
        if not pos:
            continue
        gsis_id = row.get("gsis_id")
        if not gsis_id:
            continue
        try:
            rank = int(row["pos_rank"])
        except (ValueError, KeyError, TypeError):
            continue
        existing = out.get(gsis_id)
        if not existing or rank < existing["dr"]:
            out[gsis_id] = {"dr": rank, "t": row.get("team", ""), "p": pos,
                             "n": row.get("player_name", "")}
    return out


def main():
    season = guess_season()

    print(f"Fetching production stats for season {season}...")
    prod_csv = fetch_csv_text(STATS_URL.format(season=season))
    used_season = season
    if not prod_csv:
        print(f"No production data for {season} yet, falling back to {season - 1}...")
        prod_csv = fetch_csv_text(STATS_URL.format(season=season - 1))
        used_season = season - 1

    production = aggregate_production(prod_csv) if prod_csv else {}

    print("Fetching draft capital...")
    draft_csv = fetch_csv_text(DRAFT_PICKS_URL)
