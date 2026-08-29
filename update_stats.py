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
            status = getattr(resp, "status", 200)
            if status != 200:
                print(f"  non-200 status ({status}) for {url}", file=sys.stderr, flush=True)
                return None
            data = resp.read().decode("utf-8")
            print(f"  fetched {url} ({len(data)} bytes)", flush=True)
            return data
    except Exception as e:
        print(f"  fetch failed for {url}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


def guess_season() -> int:
    today = date.today()
    return today.year if today.month >= 8 else today.year - 1


def aggregate_production(csv_text: str):
    agg = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    def num(row, key):
        try:
            return float(row.get(key) or 0)
        except ValueError:
            return 0.0
    for row in reader:
        if row.get("season_type") != "REG":
            continue
        pos = row.get("position")
        if pos not in FANTASY_POSITIONS:
            continue
        pid = row.get("player_id")
        if not pid:
            continue
        d = agg.setdefault(pid, {
            "pts": 0.0, "games": 0, "name": "", "pos": "", "team": "",
            "pass_yds": 0.0, "pass_td": 0.0, "int": 0.0,
            "rush_yds": 0.0, "rush_td": 0.0,
            "rec": 0.0, "rec_yds": 0.0, "rec_td": 0.0,
        })
        d["pts"] += num(row, "fantasy_points_ppr")
        d["games"] += 1
        d["name"] = row.get("player_display_name", "")
        d["pos"] = pos
        d["team"] = row.get("team", "")
        d["pass_yds"] += num(row, "passing_yards")
        d["pass_td"] += num(row, "passing_tds")
        d["int"] += num(row, "passing_interceptions")
        d["rush_yds"] += num(row, "rushing_yards")
        d["rush_td"] += num(row, "rushing_tds")
        d["rec"] += num(row, "receptions")
        d["rec_yds"] += num(row, "receiving_yards")
        d["rec_td"] += num(row, "receiving_tds")
    out = {}
    for pid, d in agg.items():
        if d["games"] == 0:
            continue
        out[pid] = {
            "n": d["name"], "p": d["pos"], "t": d["team"],
            "pts": round(d["pts"], 1), "gp": d["games"], "ppg": round(d["pts"] / d["games"], 2),
            "py": round(d["pass_yds"]), "ptd": round(d["pass_td"]), "int": round(d["int"]),
            "ry": round(d["rush_yds"]), "rtd": round(d["rush_td"]),
            "rec": round(d["rec"]), "recy": round(d["rec_yds"]), "rectd": round(d["rec_td"]),
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

    print(f"Fetching production stats for season {season}...", flush=True)
    prod_csv = fetch_csv_text(STATS_URL.format(season=season))
    used_season = season
    if not prod_csv:
        print(f"No production data for {season} yet, falling back to {season - 1}...", flush=True)
        prod_csv = fetch_csv_text(STATS_URL.format(season=season - 1))
        used_season = season - 1

    production = aggregate_production(prod_csv) if prod_csv else {}
    print(f"  parsed production for {len(production)} players", flush=True)

    print("Fetching draft capital...", flush=True)
    draft_csv = fetch_csv_text(DRAFT_PICKS_URL)
    draft_capital = load_draft_capital(draft_csv)
    print(f"  parsed draft capital for {len(draft_capital)} name/position keys", flush=True)

    print(f"Fetching depth chart for season {season}...", flush=True)
    depth_csv = fetch_csv_text(DEPTH_CHART_URL.format(season=season))
    if not depth_csv:
        print(f"No depth chart for {season} yet, falling back to {season - 1}...", flush=True)
        depth_csv = fetch_csv_text(DEPTH_CHART_URL.format(season=season - 1))
    depth_chart = load_depth_chart(depth_csv)
    print(f"  parsed depth chart for {len(depth_chart)} players", flush=True)

    all_ids = set(production) | set(depth_chart)
    merged = {}
    for pid in all_ids:
        prod = production.get(pid, {})
        dep = depth_chart.get(pid, {})
        name = prod.get("n") or dep.get("n") or ""
        pos = prod.get("p") or dep.get("p") or ""
        team = prod.get("t") or dep.get("t") or ""
        if not name or not pos:
            continue
        entry = {"n": name, "p": pos, "t": team}
        if prod:
            entry["pts"] = prod["pts"]
            entry["gp"] = prod["gp"]
            entry["ppg"] = prod["ppg"]
            for k in ("py", "ptd", "int", "ry", "rtd", "rec", "recy", "rectd"):
                if prod.get(k):
                    entry[k] = prod[k]
        if dep:
            entry["dr"] = dep["dr"]
        dc = draft_capital.get((norm_name(name), pos))
        if dc:
            entry["dc"] = {"rd": dc["rd"], "pk": dc["pk"], "yr": dc["yr"]}
        merged[pid] = entry

    payload = {
        "season": used_season,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "players": merged,
    }

    with open("stats.json", "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    with_prod = sum(1 for p in merged.values() if "ppg" in p)
    with_draft = sum(1 for p in merged.values() if "dc" in p)
    with_depth = sum(1 for p in merged.values() if "dr" in p)
    print(f"Wrote stats.json — season {used_season}, {len(merged)} players total "
          f"({with_prod} with production, {with_draft} with draft capital, {with_depth} with depth chart).",
          flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        print("FATAL ERROR in update_stats.py:", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.stderr.flush()
        sys.exit(1)
