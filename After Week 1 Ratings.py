"""
After Week 1 Ratings.py  (v4 -- restricted to classifications.json's team list)

Computes in-season Off/Def/Ovr ratings for AllMOSports football teams from
any number of played weeks, using the same anchored iterative engine as
before (competitiveness_weight, MOV_CAP, a persistent prior-anchor pull --
all ported from football_ratings_2025.py), but with a different Starting
rating:

WHAT CHANGED FROM THE PREVIOUS VERSION
-----------------------------------------------------------------------------
The previous version's Starting rating was a 65%/35% blend of each team's
3-year (2023-2025) and 16-year (2010-2025) historical averages. This
version drops that blend entirely -- 2010-2024 data is no longer used at
all. Starting rating is now simply:

  - Each team's actual 2025 season rating (off/def/ovr), taken directly
    from Football_Ratings_History_2010-2025.json, no averaging.
  - FALLBACK: for a team with no 2025 record at all (64 of 362 teams in a
    spot-check -- mostly co-ops or programs that show up under a slightly
    different name year to year, e.g. "Clopton" vs. "Clopton with
    Elsberry"), the script falls back to that team's most recent available
    season before 2025, and prints exactly which teams needed the
    fallback and which year was used. This is the one place pre-2025 data
    still enters the picture, and only because the alternative is that
    team having no starting point at all. If you'd rather those teams get
    NO starting rating instead (excluded entirely, same as any other team
    with a totally unresolvable name), set ALLOW_PRE_2025_FALLBACK = False
    below.

LEAGUE_AVG_PPG is similarly now sourced from 2025's own league_average
value (not an average across recent years) by default.

WHAT'S NEW IN v4: RESTRICTED TO classifications.json
-----------------------------------------------------------------------------
Only teams that appear in CLASSIFICATIONS_PATH (the 2026-27 projected
classifications file) are rated at all now. Previously the script rated
every team it found anywhere in the historical ratings file -- 362 of
them -- even though only 300 are actually current programs per
classifications.json. The other 62 were old/discontinued/renamed programs
that have no business being in a current-season ratings output.

One thing this does NOT fix on its own: 14 teams in classifications.json
are co-op configurations (e.g. "Cuba with Steelville", "Tipton with
Bunceton") that have ZERO historical rating under that exact combined
name -- they're brand new names for 2026-27, not typos, so there's
nothing in Football_Ratings_History_2010-2025.json to filter down to for
them. These are the same 14 teams that have shown up as "no starting
rating" warnings on every game involving them throughout this season's
testing. They are printed out explicitly below (MISSING_FROM_HISTORY) so
you can see the full list in one place. Two ways to actually give them a
starting rating instead of leaving them unrated:
  1. Parse each co-op name into its member schools (e.g. "Cuba with
     Steelville" -> "Cuba" + "Steelville"), look up each member's most
     recent individual rating, and combine them (e.g. an average, or
     enrollment-weighted if you have enrollment figures) into a proxy
     Starting rating for the co-op.
  2. Manually assign a reasonable Starting rating to each of the 14 by
     hand, the same way you've built manual override lists elsewhere.
  Neither is implemented here -- say the word if you want option 1 built
  (it's a name-parsing exercise, "X with Y" / "X with Y, Z" patterns,
  same shape as the MANUAL_OVERRIDES co-op names already in your scraper
  scripts) and I'll add it.

Everything else -- the anchored iterative engine, MOV_CAP, competitiveness
weighting, PRIOR_ANCHOR_K -- is unchanged from the previous version. See
that version's docstring (or ask me again) for the full explanation of why
those exist; the short version: ratings are fit iteratively across ALL
teams and games together, each team anchored to its Starting rating with
persistent strength PRIOR_ANCHOR_K, blowouts/upsets are capped (MOV_CAP)
and softly weighted by how close the two teams are currently rated
(competitiveness_weight), so no single game can swing a rating unbounded.

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
  Only the 2025 season entry is used directly; earlier seasons are read
  solely to supply the pre-2025 fallback described above.

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
1. Edit the CONFIG section below if your file paths or engine constants
   (MOV_CAP, COMPETITIVE_THRESHOLD, PRIOR_ANCHOR_K, STARTING_YEAR) differ
   from what you want.
2. Run: python "After Week 1 Ratings.py"
3. Output: Ratings_After_Week1.json and Ratings_After_Week1.csv
   (filenames kept as-is so the existing GitHub Actions workflow needs no
   changes)
"""

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# =============================================================================
# CONFIG
# =============================================================================

HISTORICAL_RATINGS_PATH = "Football_Ratings_History_2010-2025.json"
CLASSIFICATIONS_PATH = "classifications.json"
GAMES_PATH = "football_games_2026.json"
OUTPUT_JSON_PATH = "Ratings_After_Week1.json"
OUTPUT_CSV_PATH = "Ratings_After_Week1.csv"

# Website-facing output, matching the EXACT schema the "Historical Rankings
# Page" WPCode snippet's loadCurrentSeasonRows() expects for football:
#   {"last_updated": "...", "league_average": <num>,
#    "teams": [{"ovr_rank","school","ovr_rating","off_rating","off_rank",
#                "def_rating","def_rank","classification"}, ...]}
# This is the SAME shape football_ratings_2025.py's save_overall_json()
# already produces -- ratings here are RAW off+def (no +100 shift; that
# offset only ever existed in the Twitter-graphics code, never in the
# website's JSON). Ranks are STATEWIDE (computed across every team in this
# output), matching save_overall_json()'s class_filter=None behavior.
# This file needs to end up at the URL the snippet's CURRENT_RATINGS_URLS.
# football points to: https://allmosports.github.io/football-ratings-2026/
# football_ratings_2026.json -- i.e. pushed to the football-ratings-2026
# repo with GitHub Pages enabled (serving from the branch/folder this file
# is committed to).
WEBSITE_JSON_PATH = "football_ratings_2026.json"

# Starting rating is this season's actual rating -- no more 16yr/3yr blend.
STARTING_YEAR = 2025

# If a team has no STARTING_YEAR record, fall back to their most recent
# season strictly before STARTING_YEAR instead of leaving them with no
# starting point at all. Set to False to exclude such teams entirely
# (same treatment as a name that never resolves at all).
ALLOW_PRE_2025_FALLBACK = True

# --- v2 engine settings -- taken directly from football_ratings_2025.py,
#     plus PRIOR_ANCHOR_K which anchors to Starting for in-season use. ---
COMPETITIVE_THRESHOLD = 40   # same "half-weight" scale as football_ratings_2025.py
MOV_CAP               = 28   # same cap as football_ratings_2025.py
LEARNING_RATE         = 0.1  # same as football_ratings_2025.py
ITERATIONS            = 1000 # same as football_ratings_2025.py -- cheap even with few games

# How many "games' worth" of trust the Starting rating gets, persistently,
# every iteration (same mechanism as football_ratings_2025.py's
# REGULARIZATION_K, generalized to anchor toward Starting instead of 0).
# Lowered from 3.0 to 1.5 after a real case (Blue Springs South, elite 2025
# rating but a rough 1-2 start including a 21-point loss) barely moved at
# 3.0 -- lower K means real in-season results carry more weight relative to
# last season's rating. Don't push this much lower without checking teams
# with only 1 game played: a single-game blowout has MORE leverage on the
# final rating as K drops, so going too low re-introduces the instability
# problem this anchor exists to prevent (see the module docstring's
# "WHY THIS REPLACED THE OLDER VERSION" section) -- just in fewer-games form.
PRIOR_ANCHOR_K = 1.5

# RATING DIFFERENTIAL GUIDE: a game is only counted toward the fit at all if
# the two teams' STARTING rating gap is <= this value. Added after a real
# case (Jackson 41, Sikeston 7 -- Jackson pulled starters in the 3rd
# quarter) where the formula PREDICTED Jackson would score 74 points
# against Sikeston's weak defense, read the actual 41 as a 33-point
# underperformance (capped to -28 by MOV_CAP), and dragged Jackson's rating
# DOWN after a dominant win, simply because no real scoreline could ever
# satisfy what the formula expected from that big a mismatch. competitive-
# ness_weight() already soft-discounts this game (to ~24.5% weight in
# Jackson's case), but 24.5% of a -28-point capped error was still enough
# to move the rating the wrong direction -- soft discounting alone wasn't
# enough. This hard cutoff removes such games from the fit ENTIRELY rather
# than just discounting them, and is checked ONCE against each team's fixed
# Starting rating (not the evolving in-iteration rating), so which games
# get excluded is decided up front and doesn't shift mid-fit. Default of 60
# excludes about 3% of a typical week's games (the genuine blowout
# mismatches) while leaving the vast majority -- competitive games between
# similarly-rated teams -- untouched.
RATING_GAP_CUTOFF = 60

# Only include games on/before this date (inclusive), as an ISO string
# e.g. "2026-09-05". Leave as None to include every played game in the file.
THROUGH_DATE = None

# Forfeits produce rule-based scores (e.g. 1-0, 8-0), not real performance --
# excluded by default.
EXCLUDE_FORFEITS = True

# League average PPG used in the Off/Def prediction formula.
# Set to a number to hard-code it. Leave as None to auto-use STARTING_YEAR's
# own league_average value from the historical file (falls back to the most
# recent year with a league_average on record if STARTING_YEAR is missing it).
LEAGUE_AVG_PPG = None

# =============================================================================
# STEP 0: load classifications.json -- the authoritative current team list
# =============================================================================

def load_classifications(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    team_to_class = {}
    team_to_district = {}
    for entry in data["teams"]:
        team_to_class[entry["school"]] = entry.get("classification")
        team_to_district[entry["school"]] = entry.get("district")
    return team_to_class, team_to_district


# =============================================================================
# STEP 1: load historical ratings, take STARTING_YEAR directly (with fallback)
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


def build_starting_ratings(by_team, classified_teams):
    """Only teams in classified_teams (from classifications.json) get a
    Starting rating at all -- everything else is out of scope, however
    much historical data it has."""
    starting = {}
    fallback_used = {}  # team -> year actually used, for teams missing STARTING_YEAR

    for team in sorted(classified_teams):
        records = by_team.get(team, [])
        exact = next((r for r in records if r["season"] == STARTING_YEAR), None)
        if exact is not None:
            starting[team] = {
                "starting_off": exact["off"],
                "starting_def": exact["def"],
                "starting_ovr": exact["ovr"],
                "starting_year": STARTING_YEAR,
            }
            continue

        if not ALLOW_PRE_2025_FALLBACK:
            continue  # no STARTING_YEAR record and fallback disabled -- excluded

        earlier = [r for r in records if r["season"] < STARTING_YEAR]
        if not earlier:
            continue  # no usable record at all -- excluded
        most_recent = max(earlier, key=lambda r: r["season"])
        starting[team] = {
            "starting_off": most_recent["off"],
            "starting_def": most_recent["def"],
            "starting_ovr": most_recent["ovr"],
            "starting_year": most_recent["season"],
        }
        fallback_used[team] = most_recent["season"]

    if fallback_used:
        print(f"  {len(fallback_used)} team(s) had no {STARTING_YEAR} record -- "
              f"fell back to their most recent earlier season:")
        for team, year in sorted(fallback_used.items()):
            print(f"    {team}: using {year}")

    missing_entirely = sorted(classified_teams - set(starting.keys()))
    if missing_entirely:
        print(f"  MISSING_FROM_HISTORY: {len(missing_entirely)} team(s) in "
              f"{CLASSIFICATIONS_PATH} have NO historical rating at all (not even "
              f"a pre-{STARTING_YEAR} one to fall back to) -- almost certainly new "
              f"co-op names for {STARTING_YEAR + 1} that don't exist under that "
              f"exact combined name in the historical file. These teams will NOT "
              f"appear in the output, and any game involving them will be skipped:")
        for team in missing_entirely:
            print(f"    {team}")

    return starting


# =============================================================================
# GAMES LOADING (unchanged -- supports both football_games_2026.json formats)
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
# v2 ITERATIVE ENGINE -- ported from football_ratings_2025.py, anchored to
# Starting (now last season's rating) instead of 0
# =============================================================================

def competitiveness_weight(gap, scale=COMPETITIVE_THRESHOLD):
    """Identical to football_ratings_2025.py's competitiveness_weight()."""
    return 1.0 / (1.0 + (gap / scale) ** 2)


def run_iterations(games, teams, off_rating, def_rating, starting, league_avg,
                    iterations, prior_anchor_k, mov_cap, learning_rate):
    for _ in range(iterations):
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


def fit_in_season_ratings(games, starting, league_avg_ppg, team_to_class, team_to_district):
    teams = list(starting.keys())
    off_rating = {t: starting[t]["starting_off"] for t in teams}
    def_rating = {t: starting[t]["starting_def"] for t in teams}

    games_used = []
    games_per_team = defaultdict(int)       # games that actually count toward the fit
    total_games_played = defaultdict(int)   # all real games, including excluded mismatches
    excluded_mismatches = []
    for g in games:
        t1, t2 = g["team_a"], g["team_b"]
        if t1 not in starting or t2 not in starting:
            missing = [t for t in (t1, t2) if t not in starting]
            print(f"  WARNING: skipping game {t1} vs {t2} — "
                  f"no starting rating for: {', '.join(missing)}")
            continue
        total_games_played[t1] += 1
        total_games_played[t2] += 1
        starting_gap = abs(starting[t1]["starting_ovr"] - starting[t2]["starting_ovr"])
        if starting_gap > RATING_GAP_CUTOFF:
            excluded_mismatches.append((t1, t2, starting_gap, g["score_a"], g["score_b"]))
            continue
        games_used.append((t1, t2, g["score_a"], g["score_b"]))
        games_per_team[t1] += 1
        games_per_team[t2] += 1

    if excluded_mismatches:
        print(f"  RATING_GAP_CUTOFF ({RATING_GAP_CUTOFF}): excluded {len(excluded_mismatches)} "
              f"game(s) entirely as blowout mismatches (Starting rating gap too large to be "
              f"informative -- these do NOT count toward either team's rating):")
        for t1, t2, gap, s1, s2 in sorted(excluded_mismatches, key=lambda x: -x[2]):
            print(f"    {t1} {s1} - {s2} {t2} (Starting gap: {gap:.1f})")

    print(f"  Running anchored fit: {len(teams)} teams, {len(games_used)} games, "
          f"{ITERATIONS} iterations (PRIOR_ANCHOR_K={PRIOR_ANCHOR_K}, "
          f"MOV_CAP={MOV_CAP}, competitiveness scale={COMPETITIVE_THRESHOLD}, "
          f"RATING_GAP_CUTOFF={RATING_GAP_CUTOFF})...")
    run_iterations(games_used, teams, off_rating, def_rating, starting, league_avg_ppg,
                   iterations=ITERATIONS, prior_anchor_k=PRIOR_ANCHOR_K,
                   mov_cap=MOV_CAP, learning_rate=LEARNING_RATE)

    output = {}
    for t in teams:
        output[t] = {
            **starting[t],
            "classification": team_to_class.get(t),
            "district": team_to_district.get(t),
            "games_played": total_games_played.get(t, 0),
            "games_used_in_rating": games_per_team.get(t, 0),
            "final_off": off_rating[t],
            "final_def": def_rating[t],
            "final_ovr": off_rating[t] + def_rating[t],
        }
    return output


# =============================================================================
# WEBSITE OUTPUT -- exact schema for the "Historical Rankings Page" snippet
# =============================================================================

def write_website_json(output, league_avg_ppg, path):
    """output: the dict from fit_in_season_ratings() (team -> rating record).
    Writes the {"last_updated","league_average","teams":[...]} shape the
    WPCode snippet's loadCurrentSeasonRows() parses directly -- see the
    WEBSITE_JSON_PATH config comment for the exact field-by-field mapping."""
    teams = list(output.keys())
    off_sorted = sorted(teams, key=lambda t: output[t]["final_off"], reverse=True)
    def_sorted = sorted(teams, key=lambda t: output[t]["final_def"], reverse=True)
    ovr_sorted = sorted(teams, key=lambda t: output[t]["final_ovr"], reverse=True)
    off_rank = {t: i + 1 for i, t in enumerate(off_sorted)}
    def_rank = {t: i + 1 for i, t in enumerate(def_sorted)}
    ovr_rank = {t: i + 1 for i, t in enumerate(ovr_sorted)}

    entries = []
    for t in ovr_sorted:  # order doesn't matter to the snippet, but sorted is easier to eyeball
        r = output[t]
        entries.append({
            "ovr_rank": ovr_rank[t],
            "school": t,
            "ovr_rating": round(r["final_ovr"], 2),
            "off_rating": round(r["final_off"], 2),
            "off_rank": off_rank[t],
            "def_rating": round(r["final_def"], 2),
            "def_rank": def_rank[t],
            "classification": r.get("classification"),
        })

    payload = {
        "last_updated": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "league_average": round(league_avg_ppg, 2),
        "teams": entries,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"  Wrote website JSON ({len(entries)} teams) to {path}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    if not Path(HISTORICAL_RATINGS_PATH).exists():
        sys.exit(f"ERROR: historical ratings file not found: {HISTORICAL_RATINGS_PATH}")
    if not Path(CLASSIFICATIONS_PATH).exists():
        sys.exit(f"ERROR: classifications file not found: {CLASSIFICATIONS_PATH}")
    if not Path(GAMES_PATH).exists():
        sys.exit(f"ERROR: games file not found: {GAMES_PATH}")

    print("Loading classifications (the authoritative current team list)...")
    team_to_class, team_to_district = load_classifications(CLASSIFICATIONS_PATH)
    classified_teams = set(team_to_class.keys())
    print(f"  {len(classified_teams)} teams loaded from {CLASSIFICATIONS_PATH}")

    print("Loading historical ratings...")
    by_team, league_averages_by_year = load_historical_ratings(HISTORICAL_RATINGS_PATH)

    league_avg_ppg = LEAGUE_AVG_PPG
    if league_avg_ppg is None:
        league_avg_ppg = league_averages_by_year.get(STARTING_YEAR)
        if league_avg_ppg is not None:
            print(f"  Using {STARTING_YEAR}'s own LEAGUE_AVG_PPG = {league_avg_ppg:.2f}")
        else:
            earlier_years = sorted(
                (y for y, v in league_averages_by_year.items() if y < STARTING_YEAR and v is not None),
                reverse=True,
            )
            if not earlier_years:
                sys.exit(f"ERROR: no league_average found for {STARTING_YEAR} or any earlier season.")
            fallback_year = earlier_years[0]
            league_avg_ppg = league_averages_by_year[fallback_year]
            print(f"  {STARTING_YEAR} has no league_average on record -- "
                  f"falling back to {fallback_year}'s value = {league_avg_ppg:.2f}")

    print(f"Building Starting ratings (last season = {STARTING_YEAR}, no pre-{STARTING_YEAR} blend, "
          f"restricted to {CLASSIFICATIONS_PATH}'s team list)...")
    starting = build_starting_ratings(by_team, classified_teams)
    print(f"  Starting ratings built for {len(starting)} of {len(classified_teams)} classified teams.")

    print("Loading played games (all weeks with scores)...")
    games = load_played_games(GAMES_PATH)

    print("Fitting anchored in-season ratings...")
    output = fit_in_season_ratings(games, starting, league_avg_ppg, team_to_class, team_to_district)

    zero_game_teams = sum(1 for v in output.values() if v["games_used_in_rating"] == 0)
    print(f"  {zero_game_teams} teams have 0 games counting toward their rating "
          f"(no in-state games played, or their only game(s) were excluded by "
          f"RATING_GAP_CUTOFF) -- converged back to their Starting rating exactly")

    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, sort_keys=True)

    csv_columns = [
        "team", "classification", "district", "games_played", "games_used_in_rating", "starting_year",
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

    print("Writing website JSON (for the Historical Rankings Page snippet)...")
    write_website_json(output, league_avg_ppg, WEBSITE_JSON_PATH)

    print(f"\n{'Team':<30}{'GP':>4}{'Used':>6}{'Start Ovr':>10}{'Final Ovr':>10}")
    print("-" * 60)
    for team, r in sorted(output.items(), key=lambda kv: kv[1]["final_ovr"], reverse=True)[:20]:
        print(f"{team:<30}{r['games_played']:>4}{r['games_used_in_rating']:>6}{r['starting_ovr']:>10.1f}{r['final_ovr']:>10.1f}")


if __name__ == "__main__":
    main()
