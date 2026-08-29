"""
Pulls the current NFL season's weekly player stats from nflverse (open data),
aggregates each player's season-to-date PPR fantasy points into a compact
JSON lookup keyed by gsis_id, and writes it to stats.json.

Runs server-side (GitHub Actions), so it isn't subject to browser CORS limits.
The deployed app fetches the resulting stats.json straight from this repo's
raw GitHub URL, which DOES serve Access-Control-Allow-Origin: *.
"""
import csv
import io
import json
import sys
from datetime import date, datetime, timezone
from urllib.request import urlopen, Request

BASE = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
POSITIONS = {"QB", "RB", "WR", "TE", "K"}


def fetch_csv_text(url: str) -> str | None:
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
    # NFL season year is the year it starts in (a "2026 season" runs Sep 2026 - Feb 2027).
    # Before September, the most relevant *available* data is usually still last season's.
    today = date.today()
    return today.year if today.month >= 8 else today.year - 1


def aggregate(csv_text: str):
    agg = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if row.get("season_type") != "REG":
            continue
        pos = row.get("position")
        if pos not in POSITIONS:
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
            "n": d["name"],
            "p": d["pos"],
            "t": d["team"],
            "pts": round(d["pts"], 1),
            "gp": d["games"],
            "ppg": round(d["pts"] / d["games"], 2),
        }
    return out


def main():
    season = guess_season()
    print(f"Trying season {season}...")
    csv_text = fetch_csv_text(BASE.format(season=season))
    used_season = season

    # Fall back to the prior season if this year's file isn't up yet (e.g. off-season)
    if not csv_text:
        print(f"No data for {season} yet, falling back to {season - 1}...")
        csv_text = fetch_csv_text(BASE.format(season=season - 1))
        used_season = season - 1

    if not csv_text:
        print("Could not fetch stats from either season. Leaving stats.json untouched.", file=sys.stderr)
        sys.exit(1)

    stats = aggregate(csv_text)
    payload = {
        "season": used_season,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "players": stats,
    }

    with open("stats.json", "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"Wrote stats.json — season {used_season}, {len(stats)} players.")


if __name__ == "__main__":
    main()
