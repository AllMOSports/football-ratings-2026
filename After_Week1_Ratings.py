"""
After Week 1 Ratings.py
 
Computes updated Off/Def/Ovr ratings for AllMOSports football teams from
any number of played weeks, by:
  1. Averaging each team's Off/Def/Ovr ratings over the last 16 seasons (2010-2025)
  2. Averaging each team's Off/Def/Ovr ratings over the last 3 seasons (2023-2025)
  3. Blending #1 and #2 into a "Starting Rating" (default: 35% 16yr / 65% 3yr)
  4. For every IN-STATE game a team has actually played (any score present,
     not a forfeit), computing a one-shot "game estimate" of that team's
     Off/Def/Ovr using the standard prediction formula
     (Off_A - Def_B + League Avg PPG), based on the team's Starting rating
  5. Blending the Starting rating with the AVERAGE of however many game
     estimates a team has, using a shrinkage formula so the blend adapts
     automatically to how many in-state games a team has on record:
 
        Final = (K * Starting + sum(game estimates)) / (K + n)
 
     where n = number of in-state games played and K = PRIOR_GAMES_EQUIVALENT,
     a constant representing how many games' worth of trust the Starting
     rating gets. K=4 reproduces the original 80/20 Week-1-only blend exactly
     when n=1 (4*Starting + 1*game) / (4+1) = 0.8*Starting + 0.2*game.
     At n=0 (no in-state games at all — e.g. a team that has only played
     out-of-state opponents so far, like Jackson), Final = Starting exactly.
     At n=2, each game individually counts for less than it did at n=1, but
     the two games TOGETHER carry more total weight than one game alone —
     this is standard shrinkage/regression-to-the-mean behavior, not a bug.
 
This script does NOT scrape MSHSAA — it only reads two local JSON files you
already have (see INPUT FILES below) and does arithmetic on them.
 
-----------------------------------------------------------------------------
INPUT FILES (matched to AllMOSports' actual schemas)
-----------------------------------------------------------------------------
HISTORICAL_RATINGS_PATH — a local copy of:
  AllMOSports/All_MO_Sports-Data:
  output/mshsaa_historical_records/football/Football_Ratings_History_2010-2025.json
  Shape: {"seasons": [{"year": 2010, "league_average": 24.73,
           "teams": [{"school": "Rockhurst", "off_rating": ..., "def_rating": ...,
                       "ovr_rating": ...}, ...]}, ...]}
 
GAMES_PATH — a local copy of:
  AllMOSports/football-ratings-2026: football_games_2026.json
  Shape: a flat list of ALL games in the 2026 season (played and unplayed):
  [{"date": "2026-08-27", "team1": "Diamond", "score1": 14,
    "team2": "Buffalo", "score2": 55, "forfeit": false, "overtime": false}, ...]
  This script automatically uses every game that HAS scores (score1 and
  score2 are not null) and isn't a forfeit — no matter how many weeks that
  spans. Future/unplayed games (null scores) are ignored automatically, so
  you don't need to update a date range each week; just re-run it against
  the latest games file.
 
-----------------------------------------------------------------------------
HOW TO USE
-----------------------------------------------------------------------------
1. Edit the CONFIG section below if your file paths or weights differ.
2. Run: python "After Week 1 Ratings.py"
3. Output: Ratings_After_Week1.json and Ratings_After_Week1.csv
   (filenames kept as-is so the existing GitHub Actions workflow needs no
   changes — the content now reflects ALL played weeks, not just Week 1)
"""
 
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
 
# =============================================================================
# CONFIG
# =============================================================================
 
HISTORICAL_RATINGS_PATH = "Football_Ratings_History_2010-2025.json"
GAMES_PATH = "football_games_2026.json"
OUTPUT_JSON_PATH = "Ratings_After_Week1.json"
OUTPUT_CSV_PATH = "Ratings_After_Week1.csv"
 
YEARS_ALL = range(2010, 2026)      # 2010-2025 inclusive, for the 16-year average
YEARS_RECENT = range(2023, 2026)   # 2023-2025 inclusive, for the 3-year average
 
WEIGHT_3YR = 0.65
WEIGHT_16YR = 0.35
# WEIGHT_3YR + WEIGHT_16YR should equal 1.0
 
# How many "virtual games" the Starting rating is worth when blending against
# actual in-state game results. Higher = trust the multi-year prior more /
# move ratings more slowly as games accumulate. K=4 matches the original
# Week-1-only 80/20 split exactly at n=1 game played. Tune this once you can
# backtest against a full season.
PRIOR_GAMES_EQUIVALENT = 4.0
 
# Only include games on/before this date (inclusive), as an ISO string
# e.g. "2026-09-05". Leave as None to include every played game in the file
# (recommended — unplayed games are automatically excluded via null scores,
# so this is only useful if you want to reproduce an earlier point in time).
THROUGH_DATE = None
 
# Forfeits produce rule-based scores (e.g. 1-0, 8-0), not real performance —
# excluded by default.
EXCLUDE_FORFEITS = True
 
# League average PPG used in the Off/Def prediction formula.
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
# STEP 4: per-game "game estimate" of each team's Off/Def/Ovr, from the
# STARTING ratings basis (kept non-circular — see module docstring)
# =============================================================================
 
def load_played_games(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
 
    # The games file's format has changed over the course of the season —
    # this loader supports both so it keeps working either way:
    #   (a) OLD flat-list format: [{"date","team1","team2","score1","score2","forfeit"}, ...]
    #   (b) NEW team-keyed format: {"season","generated","teams": {
    #         "Diamond": [{"date","opponent","team_score","opp_score","forfeit"}, ...], ...}}
    #       Each real game appears TWICE in (b) — once under each team's own
    #       list — so it's deduplicated below by (date, the pair of teams).
    if isinstance(raw, list):
        all_games = [
            {
                "date": g["date"],
                "team_a": g["team1"],
                "team_b": g["team2"],
                "score_a": g.get("score1"),
                "score_b": g.get("score2"),
                "forfeit": g.get("forfeit", False),
            }
            for g in raw
        ]
    elif isinstance(raw, dict) and "teams" in raw:
        seen_games = set()
        all_games = []
        for team, schedule in raw["teams"].items():
            for g in schedule:
                opponent = g.get("opponent")
                date = g.get("date")
                if opponent is None or date is None:
                    continue
                dedup_key = (date, frozenset((team, opponent)))
                if dedup_key in seen_games:
                    continue
                seen_games.add(dedup_key)
                all_games.append({
                    "date": date,
                    "team_a": team,
                    "team_b": opponent,
                    "score_a": g.get("team_score"),
                    "score_b": g.get("opp_score"),
                    "forfeit": g.get("forfeit", False),
                })
    else:
        sys.exit(f"ERROR: unrecognized games file format in {path} — expected either a "
                  f"flat list of games, or a dict with a top-level 'teams' key.")
 
    played = []
    skipped_forfeits = 0
    skipped_unplayed = 0
    skipped_after_cutoff = 0
    for g in all_games:
        if THROUGH_DATE is not None and g["date"] > THROUGH_DATE:
            skipped_after_cutoff += 1
            continue
        if EXCLUDE_FORFEITS and g.get("forfeit"):
            skipped_forfeits += 1
            continue
        if g["score_a"] is None or g["score_b"] is None:
            skipped_unplayed += 1
            continue
        played.append({
            "date": g["date"],
            "team_a": g["team_a"],
            "team_b": g["team_b"],
            "score_a": g["score_a"],
            "score_b": g["score_b"],
        })
 
    print(f"  {len(played)} played in-state games found "
          f"({skipped_unplayed} unplayed/no-score, {skipped_forfeits} forfeits excluded"
          + (f", {skipped_after_cutoff} after THROUGH_DATE" if THROUGH_DATE else "")
          + ")")
    if played:
        weeks = sorted(set(g["date"] for g in played))
        print(f"  Dates covered: {weeks[0]} through {weeks[-1]} ({len(weeks)} distinct dates)")
    return played
 
 
def compute_game_estimate(game, starting, league_avg_ppg):
    """One-shot Off/Def/Ovr estimate for BOTH teams in a single game, based on
    each team's Starting rating (not their evolving in-season rating)."""
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
        game_ovr = ovr_x + (actual_margin - predicted_margin)
 
        predicted_score = off_x - def_y + league_avg_ppg
        game_off = off_x + (pts_for - predicted_score)
 
        predicted_points_allowed = off_y - def_x + league_avg_ppg
        game_def = def_x + (predicted_points_allowed - pts_against)
 
        results[team] = {"game_off": game_off, "game_def": game_def, "game_ovr": game_ovr}
 
    return results
 
 
def accumulate_game_estimates(games, starting, league_avg_ppg):
    """Sum each team's per-game estimates so the shrinkage blend can average
    them against however many games (n) each team actually has."""
    accum = defaultdict(lambda: {"off_sum": 0.0, "def_sum": 0.0, "ovr_sum": 0.0, "n": 0})
    for game in games:
        result = compute_game_estimate(game, starting, league_avg_ppg)
        if result is None:
            continue
        for team, vals in result.items():
            entry = accum[team]
            entry["off_sum"] += vals["game_off"]
            entry["def_sum"] += vals["game_def"]
            entry["ovr_sum"] += vals["game_ovr"]
            entry["n"] += 1
    return accum
 
 
# =============================================================================
# STEP 5: shrinkage blend — Starting vs. average of that team's game estimates
# =============================================================================
 
def blend_final(starting_entry, accum_entry, k):
    n = accum_entry["n"] if accum_entry else 0
    if n == 0:
        # No in-state games on record for this team (e.g. Jackson early
        # season) — Final rating is just the Starting rating, unchanged.
        return {
            "final_off": starting_entry["starting_off"],
            "final_def": starting_entry["starting_def"],
            "final_ovr": starting_entry["starting_ovr"],
            "games_played": 0,
            "avg_game_off": None,
            "avg_game_def": None,
            "avg_game_ovr": None,
        }
 
    final_off = (k * starting_entry["starting_off"] + accum_entry["off_sum"]) / (k + n)
    final_def = (k * starting_entry["starting_def"] + accum_entry["def_sum"]) / (k + n)
    final_ovr = (k * starting_entry["starting_ovr"] + accum_entry["ovr_sum"]) / (k + n)
    return {
        "final_off": final_off,
        "final_def": final_def,
        "final_ovr": final_ovr,
        "games_played": n,
        "avg_game_off": accum_entry["off_sum"] / n,
        "avg_game_def": accum_entry["def_sum"] / n,
        "avg_game_ovr": accum_entry["ovr_sum"] / n,
    }
 
 
# =============================================================================
# MAIN
# =============================================================================
 
def main():
    if not Path(HISTORICAL_RATINGS_PATH).exists():
        sys.exit(f"ERROR: historical ratings file not found: {HISTORICAL_RATINGS_PATH}")
    if not Path(GAMES_PATH).exists():
        sys.exit(f"ERROR: games file not found: {GAMES_PATH}")
 
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
 
    print("Loading played games (all weeks with scores)...")
    games = load_played_games(GAMES_PATH)
 
    print(f"Computing per-game estimates and shrinkage blend (K={PRIOR_GAMES_EQUIVALENT})...")
    accum = accumulate_game_estimates(games, starting, league_avg_ppg)
 
    output = {}
    for team, starting_entry in starting.items():
        blended = blend_final(starting_entry, accum.get(team), PRIOR_GAMES_EQUIVALENT)
        output[team] = {**starting_entry, **blended}
 
    games_played_counts = [v["games_played"] for v in output.values()]
    zero_game_teams = sum(1 for n in games_played_counts if n == 0)
    print(f"  {zero_game_teams} teams have 0 in-state games on record "
          f"(kept at their Starting rating — e.g. teams that have only "
          f"played out-of-state opponents so far)")
 
    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, sort_keys=True)
 
    csv_columns = [
        "team", "games_played",
        "starting_off", "starting_def", "starting_ovr",
        "avg_game_off", "avg_game_def", "avg_game_ovr",
        "final_off", "final_def", "final_ovr",
    ]
    with open(OUTPUT_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_columns)
        writer.writeheader()
        for team in sorted(output, key=lambda t: output[t]["final_ovr"], reverse=True):
            row = {"team": team, **output[team]}
            writer.writerow({k: row.get(k, "") for k in csv_columns})
 
    print(f"\nDone. Wrote {len(output)} teams to {OUTPUT_JSON_PATH} and {OUTPUT_CSV_PATH}")
 
    print(f"\n{'Team':<30}{'GP':>4}{'Start Ovr':>10}{'Avg Game':>10}{'Final Ovr':>10}")
    print("-" * 64)
    for team, r in sorted(output.items(), key=lambda kv: kv[1]["final_ovr"], reverse=True)[:20]:
        avg_display = f"{r['avg_game_ovr']:.1f}" if r["avg_game_ovr"] is not None else "—"
        print(f"{team:<30}{r['games_played']:>4}{r['starting_ovr']:>10.1f}{avg_display:>10}{r['final_ovr']:>10.1f}")
 
 
if __name__ == "__main__":
    main()
 
