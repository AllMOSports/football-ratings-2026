"""
After Week 1 Ratings.py
 
Computes "After Week 1" Off/Def/Ovr ratings for AllMOSports football teams by:
  1. Averaging each team's Off/Def/Ovr ratings over the last 16 seasons (2010-2025)
  2. Averaging each team's Off/Def/Ovr ratings over the last 3 seasons (2023-2025)
  3. Blending #1 and #2 into a "Starting Rating" (default: 35% 16yr / 65% 3yr)
  4. Computing a one-shot "New Rating" from each team's actual Week 1 game result,
     using the standard prediction formula (Off_A - Def_B + League Avg PPG)
  5. Blending Starting + New into a Final rating (default: 80% Starting / 20% New)
 
-----------------------------------------------------------------------------
INPUT FILES (matched to AllMOSports' actual schemas)
-----------------------------------------------------------------------------
HISTORICAL_RATINGS_PATH — a local copy of:
  AllMOSports/All_MO_Sports-Data:
  output/mshsaa_historical_records/football/Football_Ratings_History_2010-2025.json
  Shape: {"seasons": [{"year": 2010, "league_average": 24.73,
           "teams": [{"school": "Rockhurst", "off_rating": ..., "def_rating": ...,
                       "ovr_rating": ...}, ...]}, ...]}
 
WEEK1_GAMES_PATH — a local copy of:
  AllMOSports/football-ratings-2026: football_games_2026.json
  Shape: a flat list of ALL games in the 2026 season (not just Week 1):
  [{"date": "2026-08-27", "team1": "Diamond", "score1": 14,
    "team2": "Buffalo", "score2": 55, "forfeit": false, "overtime": false}, ...]
  This script filters that full-season list down to Week 1 games itself —
  see WEEK1_DATES below.
 
-----------------------------------------------------------------------------
HOW TO USE
-----------------------------------------------------------------------------
1. Edit the CONFIG section below if your file paths, dates, or weights differ.
2. Run: python "After Week 1 Ratings.py"
3. Output: Ratings_After_Week1.json and Ratings_After_Week1.csv
"""
 
import csv
import json
import sys
from collections import defaultdict
from datetime import date as _date
from pathlib import Path
 
# =============================================================================
# CONFIG
# =============================================================================
 
HISTORICAL_RATINGS_PATH = "Football_Ratings_History_2010-2025.json"
WEEK1_GAMES_PATH = "football_games_2026.json"
OUTPUT_JSON_PATH = "Ratings_After_Week1.json"
OUTPUT_CSV_PATH = "Ratings_After_Week1.csv"
 
YEARS_ALL = range(2010, 2026)      # 2010-2025 inclusive, for the 16-year average
YEARS_RECENT = range(2023, 2026)   # 2023-2025 inclusive, for the 3-year average
 
WEIGHT_3YR = 0.65
WEIGHT_16YR = 0.35
# WEIGHT_3YR + WEIGHT_16YR should equal 1.0
 
WEIGHT_NEW = 0.20
WEIGHT_STARTING = 0.80
# WEIGHT_NEW + WEIGHT_STARTING should equal 1.0
 
# Set to an explicit list of ISO date strings (e.g. ["2026-08-27","2026-08-28"])
# to hard-code which dates count as "Week 1". Leave as None to auto-detect:
# the script takes the earliest date in the games file, then keeps adding
# consecutive game dates until it hits a gap of more than WEEK1_MAX_GAP_DAYS
# (this naturally separates "Week 1" from "Week 2" since MSHSAA weeks cluster
# on Thu/Fri/Sat then jump ~5 days to the next week).
WEEK1_DATES = None
WEEK1_MAX_GAP_DAYS = 3
 
# Forfeits produce rule-based scores (e.g. 1-0, 8-0), not real performance —
# excluded from the Week 1 adjustment by default.
EXCLUDE_FORFEITS = True
 
# League average PPG used in the Off/Def prediction formula for Week 1.
# Set to a number to hard-code it. Leave as None to auto-compute it as the
# average of the historical file's per-season league_average over YEARS_RECENT
# (2023-2025) — a reasonable proxy until the 2026 season has its own number.
LEAGUE_AVG_PPG = None
 
# =============================================================================
# STEP 1-2: load historical ratings and compute the two averages
# =============================================================================
 
def load_historical_ratings(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
 
    by_team = defaultdict(list)
    league_averages_by_year = {}
    for season in data["seasons"]:
        year = season["year"]
        league_averages_by_year[year] = season.get("league_average")
        for team in season["teams"]:
            by_team[team["school"]].append({
                "season": year,
                "off": team["off_rating"],
                "def": team["def_rating"],
                "ovr": team["ovr_rating"],
            })
    return by_team, league_averages_by_year
 
 
def average_ratings(records, years):
    filtered = [r for r in records if r["season"] in years]
    if not filtered:
        return None
    n = len(filtered)
    return {
        "off": sum(r["off"] for r in filtered) / n,
        "def": sum(r["def"] for r in filtered) / n,
        "ovr": sum(r["ovr"] for r in filtered) / n,
        "seasons_used": n,
    }
 
 
def build_starting_ratings(by_team):
    starting = {}
    for team, records in by_team.items():
        avg_16yr = average_ratings(records, YEARS_ALL)
        avg_3yr = average_ratings(records, YEARS_RECENT)
 
        if avg_16yr is None and avg_3yr is None:
            continue
 
        if avg_16yr is None:
            blended = avg_3yr
        elif avg_3yr is None:
            blended = avg_16yr
        else:
            blended = {
                "off": WEIGHT_16YR * avg_16yr["off"] + WEIGHT_3YR * avg_3yr["off"],
                "def": WEIGHT_16YR * avg_16yr["def"] + WEIGHT_3YR * avg_3yr["def"],
                "ovr": WEIGHT_16YR * avg_16yr["ovr"] + WEIGHT_3YR * avg_3yr["ovr"],
            }
 
        starting[team] = {
            "starting_off": blended["off"],
            "starting_def": blended["def"],
            "starting_ovr": blended["ovr"],
        }
    return starting
 
 
# =============================================================================
# STEP 4: one-shot "New" rating from the Week 1 result
# =============================================================================
 
def auto_detect_week1_dates(games, max_gap_days):
    dates = sorted(set(g["date"] for g in games))
    if not dates:
        return set()
    selected = [dates[0]]
    prev = _date.fromisoformat(dates[0])
    for d in dates[1:]:
        cur = _date.fromisoformat(d)
        if (cur - prev).days > max_gap_days:
            break
        selected.append(d)
        prev = cur
    return set(selected)
 
 
def load_week1_games(path):
    with open(path, "r", encoding="utf-8") as f:
        all_games = json.load(f)
 
    week1_dates = set(WEEK1_DATES) if WEEK1_DATES else auto_detect_week1_dates(
        all_games, WEEK1_MAX_GAP_DAYS
    )
    print(f"  Week 1 dates: {sorted(week1_dates)}")
 
    filtered = []
    skipped_forfeits = 0
    for g in all_games:
        if g["date"] not in week1_dates:
            continue
        if EXCLUDE_FORFEITS and g.get("forfeit"):
            skipped_forfeits += 1
            continue
        if g.get("score1") is None or g.get("score2") is None:
            continue  # game not yet played / no score reported
        filtered.append({
            "team_a": g["team1"],
            "team_b": g["team2"],
            "score_a": g["score1"],
            "score_b": g["score2"],
        })
    if skipped_forfeits:
        print(f"  Excluded {skipped_forfeits} forfeit game(s) from Week 1.")
    return filtered
 
 
def compute_new_ratings_for_game(game, starting, league_avg_ppg):
    team_a, team_b = game["team_a"], game["team_b"]
    score_a, score_b = game["score_a"], game["score_b"]
 
    if team_a not in starting or team_b not in starting:
        missing = [t for t in (team_a, team_b) if t not in starting]
        print(f"  WARNING: skipping game {team_a} vs {team_b} — "
              f"no starting rating for: {', '.join(missing)}")
        return None
 
    a = starting[team_a]
    b = starting[team_b]
 
    results = {}
    for (team, off_x, def_x, ovr_x, off_y, def_y, ovr_y, pts_for, pts_against) in [
        (team_a, a["starting_off"], a["starting_def"], a["starting_ovr"],
         b["starting_off"], b["starting_def"], b["starting_ovr"], score_a, score_b),
        (team_b, b["starting_off"], b["starting_def"], b["starting_ovr"],
         a["starting_off"], a["starting_def"], a["starting_ovr"], score_b, score_a),
    ]:
        predicted_margin = ovr_x - ovr_y
        actual_margin = pts_for - pts_against
        new_ovr = ovr_x + (actual_margin - predicted_margin)
 
        predicted_score = off_x - def_y + league_avg_ppg
        new_off = off_x + (pts_for - predicted_score)
 
        predicted_points_allowed = off_y - def_x + league_avg_ppg
        new_def = def_x + (predicted_points_allowed - pts_against)
 
        results[team] = {"new_off": new_off, "new_def": new_def, "new_ovr": new_ovr}
 
    return results
 
 
# =============================================================================
# STEP 5: final blend
# =============================================================================
 
def blend_final(starting_entry, new_entry):
    return {
        "final_off": WEIGHT_STARTING * starting_entry["starting_off"] + WEIGHT_NEW * new_entry["new_off"],
        "final_def": WEIGHT_STARTING * starting_entry["starting_def"] + WEIGHT_NEW * new_entry["new_def"],
        "final_ovr": WEIGHT_STARTING * starting_entry["starting_ovr"] + WEIGHT_NEW * new_entry["new_ovr"],
    }
 
 
# =============================================================================
# MAIN
# =============================================================================
 
def main():
    if not Path(HISTORICAL_RATINGS_PATH).exists():
        sys.exit(f"ERROR: historical ratings file not found: {HISTORICAL_RATINGS_PATH}")
    if not Path(WEEK1_GAMES_PATH).exists():
        sys.exit(f"ERROR: games file not found: {WEEK1_GAMES_PATH}")
 
    print("Loading historical ratings...")
    by_team, league_averages_by_year = load_historical_ratings(HISTORICAL_RATINGS_PATH)
 
    league_avg_ppg = LEAGUE_AVG_PPG
    if league_avg_ppg is None:
        recent_vals = [v for y, v in league_averages_by_year.items()
                        if y in YEARS_RECENT and v is not None]
        league_avg_ppg = sum(recent_vals) / len(recent_vals)
        print(f"  Auto-computed LEAGUE_AVG_PPG = {league_avg_ppg:.2f} "
              f"(avg of {sorted(YEARS_RECENT)} league_average values)")
 
    print("Building Starting ratings (16yr + 3yr blend)...")
    starting = build_starting_ratings(by_team)
    print(f"  Starting ratings built for {len(starting)} teams.")
 
    print("Loading and filtering Week 1 games...")
    games = load_week1_games(WEEK1_GAMES_PATH)
    print(f"  {len(games)} Week 1 games loaded.")
 
    print("Computing Week 1 one-shot adjustments and final blend...")
    output = {}
    for game in games:
        new_ratings = compute_new_ratings_for_game(game, starting, league_avg_ppg)
        if new_ratings is None:
            continue
        for team, new_entry in new_ratings.items():
            final = blend_final(starting[team], new_entry)
            output[team] = {**starting[team], **new_entry, **final}
 
    teams_no_game = set(starting.keys()) - set(output.keys())
    if teams_no_game:
        print(f"  Note: {len(teams_no_game)} teams had a Starting rating but no "
              f"Week 1 game found (bye/out-of-state opponent/not in file) — "
              f"their Final rating is just their Starting rating.")
        for team in teams_no_game:
            output[team] = {
                **starting[team],
                "new_off": None, "new_def": None, "new_ovr": None,
                "final_off": starting[team]["starting_off"],
                "final_def": starting[team]["starting_def"],
                "final_ovr": starting[team]["starting_ovr"],
            }
 
    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, sort_keys=True)
 
    csv_columns = [
        "team", "starting_off", "starting_def", "starting_ovr",
        "new_off", "new_def", "new_ovr",
        "final_off", "final_def", "final_ovr",
    ]
    with open(OUTPUT_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_columns)
        writer.writeheader()
        for team in sorted(output, key=lambda t: output[t]["final_ovr"], reverse=True):
            row = {"team": team, **output[team]}
            writer.writerow({k: row.get(k, "") for k in csv_columns})
 
    print(f"\nDone. Wrote {len(output)} teams to {OUTPUT_JSON_PATH} and {OUTPUT_CSV_PATH}")
 
    print(f"\n{'Team':<30}{'Start Ovr':>10}{'New Ovr':>10}{'Final Ovr':>10}")
    print("-" * 60)
    for team, r in sorted(output.items(), key=lambda kv: kv[1]["final_ovr"], reverse=True)[:20]:
        new_ovr_display = f"{r['new_ovr']:.1f}" if r["new_ovr"] is not None else "—"
        print(f"{team:<30}{r['starting_ovr']:>10.1f}{new_ovr_display:>10}{r['final_ovr']:>10.1f}")
 
 
if __name__ == "__main__":
    main()
