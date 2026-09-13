"""
After Week 1 Ratings.py  (v2 -- anchored iterative engine)
 
Computes in-season Off/Def/Ovr ratings for AllMOSports football teams from
any number of played weeks. This version REPLACES the earlier one-shot/
shrinkage-average approach with an iterative gradient-descent fit that
reuses your football_ratings_2025.py engine's own machinery
(competitiveness_weight, MOV_CAP, REGULARIZATION_K) -- the exact
safeguards your full-season engine already uses to keep blowouts and
mismatched-prior games from dominating a rating -- and adds the one thing
that engine doesn't need for a full season but an early-season snapshot
does: a historical PRIOR ANCHOR, so a team with 1-3 games doesn't get a
rating decided almost entirely by those 1-3 games.
 
WHY THIS REPLACED THE OLDER VERSION
-----------------------------------------------------------------------------
The old version computed each game's implied rating in isolation (a
"one-shot" estimate against each team's STATIC Starting rating), with no
cap on how large that estimate could be, then averaged those one-shot
estimates with a simple shrinkage blend. That has two real problems, both
confirmed on 2026 Week 3 data:
  1. NO MOV CAP: a single blowout (or an upset over a team whose Starting
     rating turned out to be wrong) could imply a +40, +50-point single-game
     rating swing, with nothing to bound it -- e.g. Raymore-Peculiar's 35-3
     win over Liberty (Starting Ovr 46.98) implied a single-game Ovr
     estimate of ~79, which then dominated their Final rating even after
     shrinkage.
  2. STATIC, NON-ITERATIVE: every game estimate used each team's ORIGINAL
     Starting rating for both teams, never letting opponents' in-season
     performance adjust the picture -- so if an opponent's Starting rating
     was stale, every game against them inherited that error individually
     instead of the whole system converging together.
 
This version fixes both, using the same techniques your 2025 full-season
engine already uses:
  - competitiveness_weight(gap): a smooth (not hard-cutoff) weight based on
    the CURRENT rating gap between the two teams. A blowout between two
    teams already known to be mismatched barely moves anything; a blowout
    between two teams rated close to each other counts fully. This weight
    is recomputed every iteration as ratings evolve, so it naturally
    adjusts as the system converges.
  - MOV_CAP: the raw scoring error (actual - predicted) is capped (default
    28, taken directly from your 2025 engine) BEFORE it's weighted and
    accumulated, so no single game -- however lopsided -- can contribute
    more than a bounded amount of "error" to a team's rating in one pass.
  - Iterative, simultaneous fitting: instead of computing each game against
    a frozen Starting rating, ALL teams' ratings are solved together via
    gradient descent (same architecture as calculate_ratings() /
    run_iterations() in football_ratings_2025.py), so a team's rating is
    influenced by its opponents' CURRENT (evolving) rating, not their
    stale preseason number.
 
WHAT'S NEW vs. football_ratings_2025.py: THE PRIOR ANCHOR
-----------------------------------------------------------------------------
Your full-season engine starts every team's off/def rating at 0.0 and lets
~1000 iterations of real games pull it to wherever the data says -- that's
correct for a full season with hundreds of games, but with only 1-3 games
per team in September, that same approach would be wildly underdetermined
(exactly the plain-iterative-fit problem this script needs to avoid).
 
Instead:
  1. Off/def ratings are INITIALIZED at each team's Starting rating (the
     16yr/3yr historical blend -- same as the old version's Step 1-3),
     instead of 0.0.
  2. Every iteration, EVERY team (even one with zero games so far) gets a
     persistent "pull" term back toward its Starting rating, with strength
     PRIOR_ANCHOR_K (in the same units as REGULARIZATION_K -- think of it
     as "this many games' worth of trust in the historical prior"). This
     is what makes the anchor a true equilibrium point rather than just a
     slower starting position -- without it, 1000 iterations would
     eventually erase the prior's influence entirely, same as it does in
     the full-season engine (where that's desired; here it isn't).
  3. A team with ZERO in-state games on record therefore converges exactly
     back to its Starting rating (the games-error term is always zero for
     them, so the only remaining force is the anchor pulling them to
     Starting) -- e.g. Jackson, when it's only played out-of-state games.
  4. A team with a FEW games gets pulled toward what those games imply,
     but capped (MOV_CAP), softly weighted (competitiveness_weight), and
     balanced against PRIOR_ANCHOR_K "games" of trust in its multi-year
     history -- so one blowout or one upset against a mis-rated opponent
     can meaningfully move the rating, but can no longer single-handedly
     dominate it the way it did in the old version.
 
This script does NOT scrape MSHSAA -- it only reads two local JSON files
(see INPUT FILES below) and does arithmetic on them.
 
-----------------------------------------------------------------------------
INPUT FILES (matched to AllMOSports' actual schemas)
-----------------------------------------------------------------------------
HISTORICAL_RATINGS_PATH -- a local copy of:
  AllMOSports/All_MO_Sports-Data:
  output/mshsaa_historical_records/football/Football_Ratings_History_2010-2025.json
  Shape: {"seasons": [{"year": 2010, "league_average": 24.73,
           "teams": [{"school": "Rockhurst", "off_rating": ..., "def_rating": ...,
                       "ovr_rating": ...}, ...]}, ...]}
 
GAMES_PATH -- a local copy of football_games_2026.json. Supports BOTH
formats seen this season:
  (a) flat list: [{"date","team1","score1","team2","score2","forfeit"}, ...]
  (b) team-keyed: {"season","generated","teams": {"TeamName": [{"date",
      "opponent","team_score","opp_score","forfeit"}, ...], ...}}
  Every game with real scores (not a forfeit) is used, regardless of how
  many weeks that spans -- unplayed games (null scores) are skipped
  automatically.
 
-----------------------------------------------------------------------------
HOW TO USE
-----------------------------------------------------------------------------
1. Edit the CONFIG section below if your file paths, weights, or engine
   constants (MOV_CAP, COMPETITIVE_THRESHOLD, PRIOR_ANCHOR_K) differ from
   what you want.
2. Run: python "After Week 1 Ratings.py"
3. Output: Ratings_After_Week1.json and Ratings_After_Week1.csv
   (filenames kept as-is so the existing GitHub Actions workflow needs no
   changes)
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
 
# --- v2 engine settings -- taken directly from football_ratings_2025.py,
#     plus PRIOR_ANCHOR_K which is new for this in-season, prior-anchored
#     version (see module docstring's "THE PRIOR ANCHOR" section). ---
COMPETITIVE_THRESHOLD = 40   # same "half-weight" scale as football_ratings_2025.py
MOV_CAP               = 28   # same cap as football_ratings_2025.py
LEARNING_RATE         = 0.1  # same as football_ratings_2025.py
ITERATIONS            = 1000 # same as football_ratings_2025.py -- cheap even with few games
 
# How many "games' worth" of trust the historical Starting rating gets,
# persistently, every iteration (this is what REGULARIZATION_K does in
# football_ratings_2025.py, generalized here to anchor toward Starting
# instead of toward 0). Reused at the same value (3.0) as your full-season
# engine's REGULARIZATION_K for consistency -- tune independently once you
# can backtest against a full season's worth of in-season snapshots.
PRIOR_ANCHOR_K = 3.0
 
# Only include games on/before this date (inclusive), as an ISO string
# e.g. "2026-09-05". Leave as None to include every played game in the file.
THROUGH_DATE = None
 
# Forfeits produce rule-based scores (e.g. 1-0, 8-0), not real performance --
# excluded by default.
EXCLUDE_FORFEITS = True
 
# League average PPG used in the Off/Def prediction formula.
# Set to a number to hard-code it. Leave as None to auto-compute it as the
# average of the historical file's per-season league_average over YEARS_RECENT
# (2023-2025) -- a reasonable proxy until the 2026 season has its own number.
LEAGUE_AVG_PPG = None
 
# =============================================================================
# STEP 1-2: load historical ratings and compute the two averages (unchanged)
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
# GAMES LOADING (unchanged from the previous version -- supports both the
# flat-list and team-keyed football_games_2026.json formats)
# =============================================================================
 
def load_played_games(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
 
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
        sys.exit(f"ERROR: unrecognized games file format in {path} -- expected either a "
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
 
 
# =============================================================================
# v2 ITERATIVE ENGINE -- ported from football_ratings_2025.py, with a
# persistent prior anchor added (see module docstring)
# =============================================================================
 
def competitiveness_weight(gap, scale=COMPETITIVE_THRESHOLD):
    """Identical to football_ratings_2025.py's competitiveness_weight()."""
    return 1.0 / (1.0 + (gap / scale) ** 2)
 
 
def run_iterations(games, teams, off_rating, def_rating, starting, league_avg,
                    iterations, prior_anchor_k, mov_cap, learning_rate):
    for _ in range(iterations):
        # Seed every team's error/weight with the persistent prior-anchor
        # pull term BEFORE any games are folded in. This is the piece that
        # doesn't exist in football_ratings_2025.py -- there, off_error/
        # weight_sum start at 0 for every team each iteration, so a team
        # with no games has no gradient at all and just sits at its 0.0
        # init forever. Here, a team with no games still gets pulled
        # exactly to its Starting rating every iteration, which is what
        # makes Starting a true equilibrium instead of just an initial
        # value that erodes over iterations.
        off_error = {t: prior_anchor_k * (starting[t]["starting_off"] - off_rating[t]) for t in teams}
        def_error = {t: prior_anchor_k * (starting[t]["starting_def"] - def_rating[t]) for t in teams}
        weight_sum = {t: prior_anchor_k for t in teams}
 
        for t1, t2, actual_s1, actual_s2 in games:
            gap = abs((off_rating[t1] + def_rating[t1]) -
                      (off_rating[t2] + def_rating[t2]))
            w = competitiveness_weight(gap)
 
            predicted_s1 = off_rating[t1] - def_rating[t2] + league_avg
            predicted_s2 = off_rating[t2] - def_rating[t1] + league_avg
 
            error_s1 = actual_s1 - predicted_s1
            error_s2 = actual_s2 - predicted_s2
 
            # MOV cap: bound the raw error before it's weighted/accumulated
            error_s1 = max(-mov_cap, min(mov_cap, error_s1))
            error_s2 = max(-mov_cap, min(mov_cap, error_s2))
 
            off_error[t1] += w * error_s1
            off_error[t2] += w * error_s2
            def_error[t1] += -w * error_s2
            def_error[t2] += -w * error_s1
 
            weight_sum[t1] += w
            weight_sum[t2] += w
 
        for team in teams:
            denom = weight_sum[team]  # always >= prior_anchor_k, never zero
            off_rating[team] += (off_error[team] / denom) * learning_rate
            def_rating[team] += (def_error[team] / denom) * learning_rate
 
 
def fit_in_season_ratings(games, starting, league_avg_ppg):
    """
    Runs the anchored iterative fit for every team that has a Starting
    rating (teams with no historical data at all are still excluded here,
    same as the old version -- there's no prior to anchor them to).
    Games where either team is missing from `starting` are skipped with a
    warning, same behavior as before.
    """
    teams = list(starting.keys())
    off_rating = {t: starting[t]["starting_off"] for t in teams}
    def_rating = {t: starting[t]["starting_def"] for t in teams}
 
    games_used = []
    games_per_team = defaultdict(int)
    for g in games:
        t1, t2 = g["team_a"], g["team_b"]
        if t1 not in starting or t2 not in starting:
            missing = [t for t in (t1, t2) if t not in starting]
            print(f"  WARNING: skipping game {t1} vs {t2} — "
                  f"no starting rating for: {', '.join(missing)}")
            continue
        games_used.append((t1, t2, g["score_a"], g["score_b"]))
        games_per_team[t1] += 1
        games_per_team[t2] += 1
 
    print(f"  Running anchored fit: {len(teams)} teams, {len(games_used)} games, "
          f"{ITERATIONS} iterations (PRIOR_ANCHOR_K={PRIOR_ANCHOR_K}, "
          f"MOV_CAP={MOV_CAP}, competitiveness scale={COMPETITIVE_THRESHOLD})...")
    run_iterations(games_used, teams, off_rating, def_rating, starting, league_avg_ppg,
                   iterations=ITERATIONS, prior_anchor_k=PRIOR_ANCHOR_K,
                   mov_cap=MOV_CAP, learning_rate=LEARNING_RATE)
 
    output = {}
    for t in teams:
        output[t] = {
            **starting[t],
            "games_played": games_per_team[t],
            "final_off": off_rating[t],
            "final_def": def_rating[t],
            "final_ovr": off_rating[t] + def_rating[t],
        }
    return output
 
 
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
 
    print("Fitting anchored in-season ratings...")
    output = fit_in_season_ratings(games, starting, league_avg_ppg)
 
    zero_game_teams = sum(1 for v in output.values() if v["games_played"] == 0)
    print(f"  {zero_game_teams} teams have 0 in-state games on record "
          f"(converged back to their Starting rating exactly)")
 
    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, sort_keys=True)
 
    csv_columns = [
        "team", "games_played",
        "starting_off", "starting_def", "starting_ovr",
        "final_off", "final_def", "final_ovr",
    ]
    with open(OUTPUT_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_columns)
        writer.writeheader()
        for team in sorted(output, key=lambda t: output[t]["final_ovr"], reverse=True):
            row = {"team": team, **output[team]}
            writer.writerow({k: row.get(k, "") for k in csv_columns})
 
    print(f"\nDone. Wrote {len(output)} teams to {OUTPUT_JSON_PATH} and {OUTPUT_CSV_PATH}")
 
    print(f"\n{'Team':<30}{'GP':>4}{'Start Ovr':>10}{'Final Ovr':>10}")
    print("-" * 54)
    for team, r in sorted(output.items(), key=lambda kv: kv[1]["final_ovr"], reverse=True)[:20]:
        print(f"{team:<30}{r['games_played']:>4}{r['starting_ovr']:>10.1f}{r['final_ovr']:>10.1f}")
 
 
if __name__ == "__main__":
    main()
