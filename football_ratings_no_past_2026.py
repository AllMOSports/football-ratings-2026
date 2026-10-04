"""
football_ratings_no_past_2026.py

2026 version of football_ratings_2025.py. The rating methodology is
IDENTICAL to the 2025 engine -- the engine functions below are copied
unchanged -- and it uses NO prior-season data: every team starts at 0 OFF /
0 DEF and the ratings are fit only from 2026 game results.

WHAT CHANGED FROM THE 2025 SCRIPT
-----------------------------------------------------------------------------
  - NO MSHSAA SCRAPING. Games are read from football_games_2026.json (the
    same file the other scripts use) instead of being scraped from the
    MSHSAA scoreboard. Everything that existed only to support scraping is
    gone: the HTTP session/retry code, the school-ID -> name lookup
    (MANUAL_OVERRIDES / mshsaa_schools.csv), and the requests/bs4 imports.
  - The same game filters the scraper applied are applied to the JSON, so
    the same kinds of games feed the fit:
        * unfinished games (no score yet) are skipped
        * forfeits are skipped
        * a game is skipped unless BOTH teams are in classifications.json
        * a game is skipped if either score is outside 0..MAX_POINTS
  - Output files are renamed:
        football_ratings_no_past_2026.json   (same layout as the 2025 JSON)
        football_ratings_no_past_2026.csv    (same columns as the 2025
                                              football_rankings_*_all.csv)
    Per-class JSON/CSV files (which the 2025 script always wrote) are now
    OFF by default -- set SAVE_CLASS_FILES = True to get them back.
  - Teams are sorted before the fit so results and rank tie-breaks are
    identical from run to run (the 2025 script iterated a Python set, whose
    order changes between runs; the rating math itself is unaffected).
  - MANUAL_GAMES / SCORE_CORRECTIONS / EXCLUDED_GAMES keep working but start
    EMPTY: the 2025 entries were specific to 2025 games.
  - The scoreboard CSV the 2025 script wrote (football_scoreboard_2025.csv)
    is no longer produced, since there is no scrape to record.

INPUT FILES (same folder as this script)
-----------------------------------------------------------------------------
  football_games_2026.json   either format is accepted:
        flat list:   [{"date","team1","score1","team2","score2","forfeit"}, ...]
        team-keyed:  {"teams": {"Team": [{"date","opponent","team_score",
                                          "opp_score","forfeit"}, ...], ...}}
  classifications.json       {"teams": [{"school","classification","district"}, ...]}

Usage:
    python football_ratings_no_past_2026.py
"""

import csv
import json
import os
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

SEASON_YEAR           = 2026
GAMES_PATH            = "football_games_2026.json"
CLASSIFICATIONS_PATH  = "classifications.json"
MAX_POINTS            = 100       # a score above this is treated as bad data (same as 2025)
OUTPUT_PATH           = f"football_ratings_no_past_{SEASON_YEAR}.json"
CSV_PATH              = f"football_ratings_no_past_{SEASON_YEAR}.csv"
SAVE_CLASS_FILES      = False     # True = also write one JSON + CSV per class (1-6)
ITERATIONS            = 1000
LEARNING_RATE         = 0.1

# --- rating engine settings (unchanged from the 2025 script) ---
COMPETITIVE_THRESHOLD = 40    # the "half-weight" point of a smooth decay curve
REGULARIZATION_K      = 3.0   # pseudo-games added to every team's denominator (shrinkage)
MOV_CAP               = 28    # max points of "error" any single game can contribute

# ---------------------------------------------------------------------------
# MANUAL GAMES / SCORE CORRECTIONS / EXCLUDED GAMES  (all start empty for 2026)
# ---------------------------------------------------------------------------
# Same formats as the 2025 script. Team names must match classifications.json.
#
#   MANUAL_GAMES      ("YYYY-MM-DD", "Team 1", score1, "Team 2", score2)
#                     games missing from football_games_2026.json
#   SCORE_CORRECTIONS ("YYYY-MM-DD", "Team A", correct_score_A, "Team B", correct_score_B)
#                     fixes a bad score; matched by date + team pair (any order)
#   EXCLUDED_GAMES    ("YYYY-MM-DD", "Team A", "Team B")
#                     drops a game entirely; matched by date + team pair (any order)

MANUAL_GAMES      = []
SCORE_CORRECTIONS = []
EXCLUDED_GAMES    = []

# ---------------------------------------------------------------------------
# CLASSIFICATIONS
# ---------------------------------------------------------------------------

def load_classifications(path=CLASSIFICATIONS_PATH):
    """Return team_to_class and team_to_district dicts keyed by school name."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    team_to_class    = {}
    team_to_district = {}
    for entry in data["teams"]:
        school = entry["school"]
        team_to_class[school]    = entry["classification"]
        team_to_district[school] = entry["district"]
    return team_to_class, team_to_district


# ---------------------------------------------------------------------------
# GAMES INPUT (replaces the MSHSAA scraper)
# ---------------------------------------------------------------------------

def _flatten_games(raw):
    """Normalize either supported games-file format into a flat list of dicts
    with keys: date, team1, score1, team2, score2, forfeit."""
    if isinstance(raw, list):
        return [
            {
                "date":    g.get("date"),
                "team1":   g.get("team1"),
                "score1":  g.get("score1"),
                "team2":   g.get("team2"),
                "score2":  g.get("score2"),
                "forfeit": bool(g.get("forfeit", False)),
            }
            for g in raw
        ]

    if isinstance(raw, dict) and "teams" in raw:
        # Team-keyed format: every game appears twice (once under each team),
        # so keep only the first sighting of each (date, team pair).
        flat, seen = [], set()
        for team, schedule in raw["teams"].items():
            for g in schedule:
                opponent = g.get("opponent")
                if opponent is None:
                    continue
                key = (g.get("date"), frozenset((team, opponent)))
                if key in seen:
                    continue
                seen.add(key)
                flat.append({
                    "date":    g.get("date"),
                    "team1":   team,
                    "score1":  g.get("team_score"),
                    "team2":   opponent,
                    "score2":  g.get("opp_score"),
                    "forfeit": bool(g.get("forfeit", False)),
                })
        return flat

    sys.exit(f"ERROR: unrecognized games file format in {GAMES_PATH} -- expected a "
             f"flat list of games or a dict with a top-level 'teams' key.")


def load_games(path, known_teams):
    """
    Read the games file and keep only games that would have survived the 2025
    scraper's filters. Returns (games, stats) where games is a list of
    (date_str, team1, score1, team2, score2) tuples -- the same shape the 2025
    scraper produced.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    flat = _flatten_games(raw)

    games = []
    stats = {"in_file": len(flat), "unplayed": 0, "forfeit": 0,
             "unknown_team": 0, "bad_score": 0}
    unknown_names = set()

    for g in flat:
        s1, s2 = g["score1"], g["score2"]
        if s1 is None or s2 is None:
            stats["unplayed"] += 1
            continue
        if g["forfeit"]:
            stats["forfeit"] += 1
            continue
        t1, t2 = g["team1"], g["team2"]
        if t1 not in known_teams or t2 not in known_teams:
            stats["unknown_team"] += 1
            unknown_names.update(t for t in (t1, t2) if t not in known_teams)
            continue
        if not (isinstance(s1, (int, float)) and isinstance(s2, (int, float))
                and 0 <= s1 <= MAX_POINTS and 0 <= s2 <= MAX_POINTS):
            stats["bad_score"] += 1
            continue
        games.append((g["date"], t1, int(s1), t2, int(s2)))

    stats["kept"] = len(games)
    stats["unknown_names"] = sorted(unknown_names)
    return games, stats


# ---------------------------------------------------------------------------
# GAME CLEANUP (unchanged from the 2025 script)
# ---------------------------------------------------------------------------

def apply_score_corrections(all_games, corrections=SCORE_CORRECTIONS):
    """
    Fix known-bad scores in place. Matches each game by date + the two team
    names (order-independent), then overwrites each named team's score with
    the corrected value -- regardless of which position (t1/t2) that team
    is listed in.
    """
    lookup = {}
    for date_str, team_a, score_a, team_b, score_b in corrections:
        lookup[(date_str, frozenset([team_a, team_b]))] = {team_a: score_a, team_b: score_b}

    corrected = 0
    fixed_games = []
    for date_str, t1, s1, t2, s2 in all_games:
        key = (date_str, frozenset([t1, t2]))
        fix = lookup.get(key)
        if fix is not None:
            new_s1 = fix.get(t1, s1)
            new_s2 = fix.get(t2, s2)
            if (new_s1, new_s2) != (s1, s2):
                corrected += 1
            fixed_games.append((date_str, t1, new_s1, t2, new_s2))
        else:
            fixed_games.append((date_str, t1, s1, t2, s2))

    if corrected:
        print(f"  Corrected {corrected} game score(s) via SCORE_CORRECTIONS.")
    else:
        print("  No SCORE_CORRECTIONS matched (nothing changed).")

    return fixed_games


def apply_exclusions(all_games, exclusions=EXCLUDED_GAMES):
    """
    Drop games confirmed bad/unverifiable. Matches by date + the two team
    names (order-independent).
    """
    exclude_keys = {(date_str, frozenset([team_a, team_b]))
                    for date_str, team_a, team_b in exclusions}

    filtered_games = [
        g for g in all_games
        if (g[0], frozenset([g[1], g[3]])) not in exclude_keys
    ]

    removed = len(all_games) - len(filtered_games)
    if removed:
        print(f"  Removed {removed} excluded game(s) via EXCLUDED_GAMES.")
    else:
        print("  No EXCLUDED_GAMES matched (nothing removed).")

    return filtered_games


def deduplicate_games(all_games):
    """
    Remove duplicate games where the same two teams played on the same date,
    regardless of which team is listed first. The key is date + the two team
    names only (scores intentionally excluded), exactly as in the 2025 script.
    """
    seen         = set()
    unique_games = []
    duplicates   = 0

    for game in all_games:
        date_str, t1, s1, t2, s2 = game
        key = (date_str, frozenset([t1, t2]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique_games.append(game)

    if duplicates:
        print(f"  Removed {duplicates} duplicate game(s). "
              f"{len(unique_games)} unique games remain.")
    else:
        print(f"  No duplicates found. {len(unique_games)} games.")

    return unique_games


def report_missing_teams(all_games, team_to_class):
    """
    Compare every team in classifications.json against the teams that appear
    in the games. Print the teams with zero games -- they get no rating.
    """
    teams_with_games = set()
    for _, t1, _, t2, _ in all_games:
        teams_with_games.add(t1)
        teams_with_games.add(t2)

    missing = sorted(t for t in team_to_class if t not in teams_with_games)

    if missing:
        print(f"\n  MISSING TEAMS: {len(missing)} classification schools have "
              f"no games in the games file and will NOT appear in the ratings.")
        print(f"  Missing: {missing}\n")
    else:
        print("\n  All classification schools have at least one game. \n")


# ---------------------------------------------------------------------------
# RATING ENGINE (identical to the 2025 script -- soft competitiveness
# weighting + shrinkage regularization + MOV cap)
# ---------------------------------------------------------------------------
#
#   1. competitiveness_weight() gives every game a smooth weight based on
#      the current rating gap, instead of an all-or-nothing cutoff.
#   2. REGULARIZATION_K shrinks updates for teams with little competitive
#      signal, instead of letting a tiny sample fully drive their rating.
#   3. MOV_CAP bounds how much error any single game -- even a fully-weighted
#      one -- can contribute, so no one result can swing a rating too hard.
#
# No prior-season data is used anywhere: all ratings start at 0.0.

def competitiveness_weight(gap, scale=COMPETITIVE_THRESHOLD):
    """
    Smooth weight in (0, 1] based on the current OVR gap between two teams.
    gap=0            -> weight 1.0 (fully counted)
    gap=scale (40)   -> weight 0.5 (half counted)
    gap=2*scale (80) -> weight 0.2 (mostly discounted, never fully zero)
    """
    return 1.0 / (1.0 + (gap / scale) ** 2)


def run_iterations(games, teams, off_rating, def_rating, league_avg,
                   iterations, phase_label="Fit"):
    for iteration in range(iterations):
        off_error  = {t: 0.0 for t in teams}
        def_error  = {t: 0.0 for t in teams}
        weight_sum = {t: 0.0 for t in teams}

        for t1, t2, actual_s1, actual_s2 in games:
            gap = abs((off_rating[t1] + def_rating[t1]) -
                      (off_rating[t2] + def_rating[t2]))
            w = competitiveness_weight(gap)

            predicted_s1 = off_rating[t1] - def_rating[t2] + league_avg
            predicted_s2 = off_rating[t2] - def_rating[t1] + league_avg

            error_s1 = actual_s1 - predicted_s1
            error_s2 = actual_s2 - predicted_s2

            # MOV cap: bound the raw error before it's weighted/accumulated
            error_s1 = max(-MOV_CAP, min(MOV_CAP, error_s1))
            error_s2 = max(-MOV_CAP, min(MOV_CAP, error_s2))

            off_error[t1] += w * error_s1
            off_error[t2] += w * error_s2
            def_error[t1] += -w * error_s2
            def_error[t2] += -w * error_s1

            weight_sum[t1] += w
            weight_sum[t2] += w

        for team in teams:
            # Shrinkage: denominator is (weighted games) + K, not just raw
            # games played.
            denom = weight_sum[team] + REGULARIZATION_K
            off_rating[team] += (off_error[team] / denom) * LEARNING_RATE
            def_rating[team] += (def_error[team] / denom) * LEARNING_RATE

        if (iteration + 1) % 100 == 0:
            print(f"  [{phase_label}] Iteration {iteration + 1}/{iterations} complete")


def calculate_ratings(all_games, iterations=ITERATIONS):
    games = [(t1, t2, s1, s2) for _, t1, s1, t2, s2 in all_games]

    # sorted() (not a bare set) so team order -- and therefore rank
    # tie-breaks -- is the same on every run. Does not affect the math.
    teams = sorted({t for t1, t2, _, _ in games for t in (t1, t2)})
    if not teams:
        return {}, {}, {}, 0

    all_scores = [s for _, _, s1, s2 in games for s in (s1, s2)]
    league_avg = sum(all_scores) / len(all_scores)
    print(f"  League average: {league_avg:.2f} points per game")

    off_rating = {t: 0.0 for t in teams}
    def_rating = {t: 0.0 for t in teams}

    print(f"\n  Running rating fit ({iterations} iterations, soft-weighted "
          f"by competitiveness [scale={COMPETITIVE_THRESHOLD}], "
          f"shrinkage K={REGULARIZATION_K}, MOV cap={MOV_CAP})...")
    print(f"  [TIMING] {len(teams)} teams, {len(games)} games going into the fit.")
    engine_t0 = time.perf_counter()
    run_iterations(games, teams, off_rating, def_rating, league_avg,
                   iterations=iterations, phase_label="Fit")
    print(f"  [TIMING] Rating fit took {time.perf_counter() - engine_t0:.1f}s.")

    ovr_rating = {t: round(off_rating[t] + def_rating[t], 2) for t in teams}
    return off_rating, def_rating, ovr_rating, league_avg


# ---------------------------------------------------------------------------
# JSON OUTPUT (same layout as the 2025 script)
# ---------------------------------------------------------------------------

def build_team_entries(off_rating, def_rating, ovr_rating,
                       team_to_class, team_to_district,
                       class_filter=None):
    all_teams = list(ovr_rating.keys())

    pool = (
        [t for t in all_teams if team_to_class.get(t) == class_filter]
        if class_filter is not None
        else all_teams
    )

    ovr_sorted = sorted(pool, key=lambda t: ovr_rating[t], reverse=True)
    off_sorted = sorted(pool, key=lambda t: off_rating[t], reverse=True)
    def_sorted = sorted(pool, key=lambda t: def_rating[t], reverse=True)

    ovr_rank = {t: i + 1 for i, t in enumerate(ovr_sorted)}
    off_rank = {t: i + 1 for i, t in enumerate(off_sorted)}
    def_rank = {t: i + 1 for i, t in enumerate(def_sorted)}

    return [
        {
            "ovr_rank":       ovr_rank[t],
            "school":         t,
            "classification": team_to_class.get(t),
            "district":       team_to_district.get(t),
            "ovr_rating":     ovr_rating[t],
            "off_rating":     round(off_rating[t], 2),
            "off_rank":       off_rank[t],
            "def_rating":     round(def_rating[t], 2),
            "def_rank":       def_rank[t],
        }
        for t in ovr_sorted
    ]


def save_overall_json(off_rating, def_rating, ovr_rating, league_avg,
                      team_to_class, team_to_district):
    entries = build_team_entries(off_rating, def_rating, ovr_rating,
                                 team_to_class, team_to_district)
    output = {
        "last_updated":   datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "league_average": round(league_avg, 2),
        "teams": entries,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Saved {len(entries)} teams to {OUTPUT_PATH}")
    print("Top 5 overall:")
    for e in entries[:5]:
        print(f"  {e['ovr_rank']}. {e['school']} (Class {e['classification']}) "
              f"| OVR: {e['ovr_rating']:+.2f} "
              f"| OFF: {e['off_rating']:+.2f} "
              f"| DEF: {e['def_rating']:+.2f}")


def save_class_jsons(off_rating, def_rating, ovr_rating, league_avg,
                     team_to_class, team_to_district):
    for cls in range(1, 7):
        entries = build_team_entries(off_rating, def_rating, ovr_rating,
                                     team_to_class, team_to_district,
                                     class_filter=cls)
        if not entries:
            print(f"  Class {cls}: no teams found -- skipping.")
            continue

        path = f"football_ratings_no_past_{SEASON_YEAR}_class{cls}.json"
        output = {
            "last_updated":   datetime.now().strftime("%B %d, %Y at %I:%M %p"),
            "league_average": round(league_avg, 2),
            "classification": cls,
            "teams": entries,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        print(f"  Class {cls}: {len(entries)} teams -> {path}")


# ---------------------------------------------------------------------------
# CSV OUTPUT (same columns as the 2025 football_rankings_*_all.csv)
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["School", "OFF Rating", "DEF Rating", "OVR Rating",
               "OFF Rank", "DEF Rank", "OVR Rank"]


def save_rankings_csv(off_rating, def_rating, ovr_rating,
                      team_to_class, team_to_district,
                      class_filter=None):
    """
    Save a rankings CSV for all teams (class_filter=None, written to CSV_PATH)
    or for one class. Ranks are computed within the pool, so class CSVs show
    class-specific ranks.
    """
    entries = build_team_entries(off_rating, def_rating, ovr_rating,
                                 team_to_class, team_to_district,
                                 class_filter=class_filter)
    if not entries:
        label = f"Class {class_filter}" if class_filter else "Overall"
        print(f"  {label}: no teams -- skipping CSV.")
        return

    if class_filter is None:
        path, label = CSV_PATH, "All teams"
    else:
        path  = f"football_ratings_no_past_{SEASON_YEAR}_class{class_filter}.csv"
        label = f"Class {class_filter}"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for e in entries:
            writer.writerow([e["school"], e["off_rating"], e["def_rating"],
                             e["ovr_rating"], e["off_rank"], e["def_rank"],
                             e["ovr_rank"]])
    print(f"  {label}: {len(entries)} teams -- {path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"=== MSHSAA Football Ratings {SEASON_YEAR} (no prior-season data, no scraping) ===")

    for required in (CLASSIFICATIONS_PATH, GAMES_PATH):
        if not os.path.exists(required):
            sys.exit(f"ERROR: required input file not found: {required} "
                     f"(it must be in the same folder the script runs from).")

    print("\nLoading classifications...")
    team_to_class, team_to_district = load_classifications()
    known_teams = set(team_to_class.keys())
    print(f"  Loaded {len(team_to_class)} teams from {CLASSIFICATIONS_PATH}")

    print(f"\nLoading games from {GAMES_PATH}...")
    all_games, stats = load_games(GAMES_PATH, known_teams)
    print(f"  {stats['in_file']} games in file -> {stats['kept']} usable")
    print(f"  skipped: {stats['unplayed']} unplayed (no score yet), "
          f"{stats['forfeit']} forfeit, "
          f"{stats['unknown_team']} with a team not in classifications.json, "
          f"{stats['bad_score']} with a score outside 0-{MAX_POINTS}")
    if stats["unknown_names"]:
        print(f"  teams not in classifications.json: {stats['unknown_names']}")
    if not all_games:
        print("No games found -- exiting.")
        sys.exit(1)

    if MANUAL_GAMES:
        print(f"\nAdding {len(MANUAL_GAMES)} manual game(s)...")
        all_games.extend(MANUAL_GAMES)

    print("\nApplying score corrections...")
    all_games = apply_score_corrections(all_games)

    print("\nApplying game exclusions...")
    all_games = apply_exclusions(all_games)

    print("\nDeduplicating games...")
    all_games = deduplicate_games(all_games)

    print("\nChecking for missing teams...")
    report_missing_teams(all_games, team_to_class)

    print(f"Running ratings engine ({ITERATIONS} iterations)...")
    off_rating, def_rating, ovr_rating, league_avg = calculate_ratings(all_games)

    print("\nSaving overall ratings JSON...")
    save_overall_json(off_rating, def_rating, ovr_rating, league_avg,
                      team_to_class, team_to_district)

    print("\nSaving rankings CSV...")
    save_rankings_csv(off_rating, def_rating, ovr_rating,
                      team_to_class, team_to_district)

    if SAVE_CLASS_FILES:
        print("\nSaving per-class JSONs and CSVs...")
        save_class_jsons(off_rating, def_rating, ovr_rating, league_avg,
                         team_to_class, team_to_district)
        for cls in range(1, 7):
            save_rankings_csv(off_rating, def_rating, ovr_rating,
                              team_to_class, team_to_district,
                              class_filter=cls)

    print("\n=== Done ===")
