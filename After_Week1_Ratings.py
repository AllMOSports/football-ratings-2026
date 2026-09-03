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
HOW TO USE
-----------------------------------------------------------------------------
1. Edit the CONFIG section below (file paths, weights, league average PPG).
2. Provide two input JSON files (see REQUIRED INPUT FORMATS below).
3. Run: python "After Week 1 Ratings.py"
4. Output is written to after_week1_ratings.json and printed as a table.
 
-----------------------------------------------------------------------------
REQUIRED INPUT FORMATS
-----------------------------------------------------------------------------
historical_ratings.json — a flat list of one record per team per season:
[
  {"season": 2010, "team": "Rockhurst", "off": 30.1, "def": 25.4, "ovr": 12.3},
  {"season": 2011, "team": "Rockhurst", "off": 31.0, "def": 24.9, "ovr": 13.1},
  ...
]
 
week1_games.json — a flat list of one record per Week 1 game:
[
  {"team_a": "Rockhurst", "team_b": "Some Opponent", "score_a": 42, "score_b": 14},
  ...
]
Only list each game ONCE (not once per team) — the script updates both teams
from a single game record.
 
If your actual JSON files use different key names (e.g. "school" instead of
"team", or nested structures), adjust the KEY NAMES in the CONFIG section
or the small parsing functions marked "ADAPT ME" below — you don't need to
rewrite the rating math to do that.
"""
 
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
 
# =============================================================================
# CONFIG — edit these to match your setup
# =============================================================================
 
HISTORICAL_RATINGS_PATH = "historical_ratings.json"   # input: all seasons, all teams
WEEK1_GAMES_PATH = "week1_games.json"                  # input: this week's games
OUTPUT_JSON_PATH = "Ratings_After_Week1.json"          # output file (JSON)
OUTPUT_CSV_PATH = "Ratings_After_Week1.csv"            # output file (CSV)
 
YEARS_ALL = range(2010, 2026)      # 2010-2025 inclusive, for the 16-year average
YEARS_RECENT = range(2023, 2026)   # 2023-2025 inclusive, for the 3-year average
 
WEIGHT_3YR = 0.65     # weight on the 3-year average when building the Starting rating
WEIGHT_16YR = 0.35    # weight on the 16-year average when building the Starting rating
# WEIGHT_3YR + WEIGHT_16YR should equal 1.0
 
WEIGHT_NEW = 0.20      # weight on the Week 1 "New" rating in the final blend
WEIGHT_STARTING = 0.80 # weight on the "Starting" rating in the final blend
# WEIGHT_NEW + WEIGHT_STARTING should equal 1.0
 
LEAGUE_AVG_PPG = 24.0   # set this to your actual statewide/classification average PPG
 
# =============================================================================
# STEP 1-2: load historical ratings and compute the two averages
# =============================================================================
 
def load_historical_ratings(path):
    """ADAPT ME if your JSON schema differs from {"season","team","off","def","ovr"}."""
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
 
    by_team = defaultdict(list)
    for r in records:
        by_team[r["team"]].append(r)
    return by_team
 
 
def average_ratings(records, years):
    """Average off/def/ovr across the given set of seasons for one team's records."""
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
    """Steps 1-3: 16yr avg, 3yr avg, blended into a Starting rating per team."""
    starting = {}
    for team, records in by_team.items():
        avg_16yr = average_ratings(records, YEARS_ALL)
        avg_3yr = average_ratings(records, YEARS_RECENT)
 
        if avg_16yr is None and avg_3yr is None:
            continue  # no data at all for this team, skip
 
        # Fall back gracefully if a team is missing one window (e.g. new program
        # with < 16 years of history, or a program that didn't field a team
        # 2023-2025) — just use whichever average is available.
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
            "avg_16yr": avg_16yr,
            "avg_3yr": avg_3yr,
        }
    return starting
 
 
# =============================================================================
# STEP 4: one-shot "New" rating from the Week 1 result
# =============================================================================
 
def load_week1_games(path):
    """ADAPT ME if your schema differs from {"team_a","team_b","score_a","score_b"}."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
 
 
def compute_new_ratings_for_game(game, starting):
    """
    Given one Week 1 game and the Starting ratings dict, return the one-shot
    'New' off/def/ovr rating for BOTH teams in the game.
 
    Returns None (and prints a warning) if either team is missing a Starting
    rating (e.g. a new/unrated program) — you'll need to handle that team
    manually, since there's no baseline to adjust from.
    """
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
    for (team, off_a, def_a, ovr_a, off_b, def_b, ovr_b, pts_for, pts_against) in [
        (team_a, a["starting_off"], a["starting_def"], a["starting_ovr"],
         b["starting_off"], b["starting_def"], b["starting_ovr"], score_a, score_b),
        (team_b, b["starting_off"], b["starting_def"], b["starting_ovr"],
         a["starting_off"], a["starting_def"], a["starting_ovr"], score_b, score_a),
    ]:
        # Ovr: predicted margin vs. actual margin
        predicted_margin = ovr_a - ovr_b
        actual_margin = pts_for - pts_against
        new_ovr = ovr_a + (actual_margin - predicted_margin)
 
        # Off: predicted score vs. actual points scored
        predicted_score = off_a - def_b + LEAGUE_AVG_PPG
        new_off = off_a + (pts_for - predicted_score)
 
        # Def: predicted points allowed vs. actual points allowed
        # (sign flipped: allowing FEWER than predicted = defense improved)
        predicted_points_allowed = off_b - def_a + LEAGUE_AVG_PPG
        new_def = def_a + (predicted_points_allowed - pts_against)
 
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
        sys.exit(f"ERROR: week 1 games file not found: {WEEK1_GAMES_PATH}")
 
    print("Loading historical ratings...")
    by_team = load_historical_ratings(HISTORICAL_RATINGS_PATH)
 
    print("Building Starting ratings (16yr + 3yr blend)...")
    starting = build_starting_ratings(by_team)
    print(f"  Starting ratings built for {len(starting)} teams.")
 
    print("Loading Week 1 games...")
    games = load_week1_games(WEEK1_GAMES_PATH)
    print(f"  {len(games)} games loaded.")
 
    print("Computing Week 1 one-shot adjustments and final blend...")
    output = {}
    for game in games:
        new_ratings = compute_new_ratings_for_game(game, starting)
        if new_ratings is None:
            continue
        for team, new_entry in new_ratings.items():
            final = blend_final(starting[team], new_entry)
            output[team] = {
                **{k: v for k, v in starting[team].items() if k.startswith("starting_")},
                **new_entry,
                **final,
            }
 
    teams_played = set(output.keys())
    teams_no_game = set(starting.keys()) - teams_played
    if teams_no_game:
        print(f"  Note: {len(teams_no_game)} teams had a Starting rating but no "
              f"Week 1 game in {WEEK1_GAMES_PATH} (bye week / not yet played / "
              f"not in the games file) — their Final rating is just their Starting rating.")
        for team in teams_no_game:
            output[team] = {
                **{k: v for k, v in starting[team].items() if k.startswith("starting_")},
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
 
    # quick console table, sorted by Final Ovr descending
    print(f"\n{'Team':<30}{'Start Ovr':>10}{'New Ovr':>10}{'Final Ovr':>10}")
    print("-" * 60)
    for team, r in sorted(output.items(), key=lambda kv: kv[1]["final_ovr"], reverse=True):
        new_ovr_display = f"{r['new_ovr']:.1f}" if r["new_ovr"] is not None else "—"
        print(f"{team:<30}{r['starting_ovr']:>10.1f}{new_ovr_display:>10}{r['final_ovr']:>10.1f}")
 
 
if __name__ == "__main__":
    main()
