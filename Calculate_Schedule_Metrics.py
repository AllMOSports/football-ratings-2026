"""
calculate_schedule_metrics.py
 
Computes Strength of Schedule (SOS) and related opponent/game-log metrics
for a single football season, using:
  - football_ratings_{YEAR}.json  (per-team OVR ratings, produced by the
    ratings engine)
  - football_scoreboard_{YEAR}.csv (Date, Home Team, Home Score, Away Team,
    Away Score)
 
Writes a standalone schedule_metrics_{YEAR}.json file (does NOT modify the
ratings file) containing, per school:
  - sos                 avg OVR rating of every opponent faced (per game,
                         so a team played twice counts twice)
  - sos_rank             1 = toughest schedule in the season
  - sov                  Strength of Victory: avg OVR rating of teams beaten
  - wins / losses / ties
  - points_for / points_against / avg_margin
  - opponent_win_pct     alt/legacy SOS metric some sites show (opponents'
                          combined W-L% for the season, per game)
  - quality_wins         # of wins over teams ranked in the top
                          QUALITY_WIN_RANK_THRESHOLD by ovr_rank
  - home_record / away_record  {wins, losses, ties}
  - longest_win_streak
  - biggest_win          {opponent, margin, date}
  - closest_game         {opponent, margin, date, result}
  - game_log             chronological list of every game with opponent
                          rating/rank context
 
--------------------------------------------------------------------------
HOW TO REUSE THIS FOR EACH SEASON REPO (2026-2025):
Each season lives in its own repo, so this script is written to be copy/
pasted as-is into every season's repo. The ONLY thing you need to change
is SEASON_YEAR below. File names are derived from it automatically.
--------------------------------------------------------------------------
"""
 
import json
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
 
# =========================================================================
# CONFIG - this is the only section you should need to edit per repo/season
# =========================================================================
SEASON_YEAR = 2026
 
# Tried in order; first one found (repo root or any subfolder) is used.
# 2026 games come from the scraper's JSON; older seasons may still use CSV.
RATINGS_FILE_CANDIDATES = [
    f"football_ratings_{SEASON_YEAR}.json",
    f"ratings_{SEASON_YEAR}.json",
    "ratings.json",
]
GAMES_FILE_CANDIDATES = [
    f"football_games_{SEASON_YEAR}_all.json",
    f"football_scoreboard_{SEASON_YEAR}.csv",
]
OUTPUT_FILE = f"schedule_metrics_{SEASON_YEAR}.json"
 
# The games JSON is a flat team1/team2 list with no home/away info, so
# team1 is NOT assumed to be the home team. Home/away records come out null
# for JSON sources. Flip to True only if you confirm team1 is always home.
JSON_TEAM1_IS_HOME = False
 
# A "quality win" is a win over a team ranked this high or better (by ovr_rank)
QUALITY_WIN_RANK_THRESHOLD = 50
 
DATE_FORMAT = "%Y-%m-%d"
# =========================================================================
 
 
def find_file(candidates, label):
    for name in candidates:
        if Path(name).exists():
            return name
    for name in candidates:
        hits = sorted(Path(".").rglob(name))
        if hits:
            return str(hits[0])
    available = sorted(str(p) for p in Path(".").rglob("*") if p.suffix in (".json", ".csv")
                       and ".git" not in p.parts)
    raise SystemExit(
        f"ERROR: couldn't find the {label} file. Tried: {candidates}\n"
        f"JSON/CSV files in this repo: {available}\n"
        f"Add the right name to the candidate list in CONFIG."
    )
 
 
def _first(d, keys):
    """First key present in dict d with a non-empty value (0 counts as a value)."""
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None
 
 
def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None
 
 
def _to_float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None
 
 
def load_ratings(path):
    """Returns dict: school_name -> {ovr_rating, ovr_rank, classification, district}.
    Tolerates a few layouts: {"teams": [...]}, a bare list, or {school: {...}}."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
 
    teams = data
    if isinstance(data, dict):
        teams = _first(data, ["teams", "ratings", "data"])
        if teams is None:
            teams = data  # maybe {school: {...}}
    if isinstance(teams, dict):
        teams = [{"school": k, **v} for k, v in teams.items() if isinstance(v, dict)]
 
    ratings = {}
    for team in teams:
        school = _first(team, ["school", "team", "name", "School", "Team"])
        ovr = _to_float(_first(team, ["ovr_rating", "ovr", "overall", "rating", "OVR"]))
        if school is None or ovr is None:
            continue
        ratings[str(school).strip()] = {
            "ovr_rating": ovr,
            "ovr_rank": _to_int(_first(team, ["ovr_rank", "rank", "overall_rank"])),
            "classification": team.get("classification"),
            "district": team.get("district"),
        }
 
    if not ratings:
        sample = list(teams[0].keys()) if teams else "file is empty"
        raise SystemExit(f"ERROR: no teams parsed from {path}. First entry's keys: {sample}")
 
    # Fill in ranks if the file doesn't carry them
    if any(r["ovr_rank"] is None for r in ratings.values()):
        ordered = sorted(ratings, key=lambda s: ratings[s]["ovr_rating"], reverse=True)
        for i, s in enumerate(ordered, start=1):
            ratings[s]["ovr_rank"] = i
 
    league_average = data.get("league_average") if isinstance(data, dict) else None
    return ratings, league_average
 
 
def load_games(path):
    if path.endswith(".json"):
        return load_games_json(path)
    return load_games_csv(path)
 
 
def load_games_json(path):
    """Flat team1/team2/score1/score2 list. Unplayed games (no scores) are skipped."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data if isinstance(data, list) else (_first(data, ["games", "data", "results"]) or [])
 
    games, skipped = [], 0
    for r in rows:
        t1 = _first(r, ["team1", "home_team", "Home Team"])
        t2 = _first(r, ["team2", "away_team", "Away Team"])
        s1 = _to_int(_first(r, ["score1", "home_score", "Home Score"]))
        s2 = _to_int(_first(r, ["score2", "away_score", "Away Score"]))
        if not t1 or not t2 or s1 is None or s2 is None:
            skipped += 1
            continue
        games.append({
            "date": str(_first(r, ["date", "Date", "game_date"]) or ""),
            "home_team": str(t1).strip(),
            "home_score": s1,
            "away_team": str(t2).strip(),
            "away_score": s2,
            "location_known": JSON_TEAM1_IS_HOME or "home_team" in r,
        })
    if skipped:
        print(f"Skipped {skipped} unplayed/incomplete game(s) in {path}")
    if not games and rows:
        print(f"WARNING: 0 games parsed. First entry's keys: {list(rows[0].keys())}")
 
    games.sort(key=lambda g: g["date"])  # stable: undated games keep file order
    return games
 
 
def load_games_csv(path):
    """Returns list of game dicts with parsed scores/date, in chronological order."""
    games = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            home_score = row["Home Score"].strip()
            away_score = row["Away Score"].strip()
            if home_score == "" or away_score == "":
                # Skip incomplete/unplayed games rather than crash the run
                continue
            games.append({
                "date": row["Date"].strip(),
                "home_team": row["Home Team"].strip(),
                "home_score": int(home_score),
                "away_team": row["Away Team"].strip(),
                "away_score": int(away_score),
                "location_known": True,
            })
 
    games.sort(key=lambda g: g["date"])
    return games
 
 
def result_for(team_score, opp_score):
    if team_score > opp_score:
        return "W"
    if team_score < opp_score:
        return "L"
    return "T"
 
 
def build_team_records(games, ratings):
    """
    First pass: build each team's win/loss/tie record so we can compute
    opponent_win_pct (needs every team's final record before we can average
    opponents' records).
    """
    record = defaultdict(lambda: {"w": 0, "l": 0, "t": 0})
 
    for g in games:
        home_result = result_for(g["home_score"], g["away_score"])
        away_result = result_for(g["away_score"], g["home_score"])
 
        if home_result == "W":
            record[g["home_team"]]["w"] += 1
        elif home_result == "L":
            record[g["home_team"]]["l"] += 1
        else:
            record[g["home_team"]]["t"] += 1
 
        if away_result == "W":
            record[g["away_team"]]["w"] += 1
        elif away_result == "L":
            record[g["away_team"]]["l"] += 1
        else:
            record[g["away_team"]]["t"] += 1
 
    return record
 
 
def win_pct(rec):
    games_played = rec["w"] + rec["l"] + rec["t"]
    if games_played == 0:
        return 0.0
    return round((rec["w"] + 0.5 * rec["t"]) / games_played, 4)
 
 
def longest_streak(game_log):
    """Longest consecutive-win streak within the season, chronologically."""
    longest = current = 0
    for g in game_log:
        if g["result"] == "W":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
 
 
def process_team(school, games, ratings, records):
    opponent_ratings = []
    opponent_win_pcts = []
    wins = losses = ties = 0
    points_for = points_against = 0
    quality_wins = 0
    home_record = {"w": 0, "l": 0, "t": 0}
    away_record = {"w": 0, "l": 0, "t": 0}
    game_log = []
 
    for g in games:
        is_home = g["home_team"] == school
        is_away = g["away_team"] == school
        if not (is_home or is_away):
            continue
 
        loc_known = g.get("location_known", True)
        opponent = g["away_team"] if is_home else g["home_team"]
        team_score = g["home_score"] if is_home else g["away_score"]
        opp_score = g["away_score"] if is_home else g["home_score"]
        result = result_for(team_score, opp_score)
        margin = team_score - opp_score
 
        opp_info = ratings.get(opponent)
        opp_ovr = opp_info["ovr_rating"] if opp_info else None
        opp_rank = opp_info["ovr_rank"] if opp_info else None
 
        if opp_ovr is not None:
            opponent_ratings.append(opp_ovr)
            opponent_win_pcts.append(win_pct(records[opponent]))
 
        if result == "W":
            wins += 1
            if loc_known:
                (home_record if is_home else away_record)["w"] += 1
            if opp_rank is not None and opp_rank <= QUALITY_WIN_RANK_THRESHOLD:
                quality_wins += 1
        elif result == "L":
            losses += 1
            if loc_known:
                (home_record if is_home else away_record)["l"] += 1
        else:
            ties += 1
            if loc_known:
                (home_record if is_home else away_record)["t"] += 1
 
        points_for += team_score
        points_against += opp_score
 
        game_log.append({
            "date": g["date"],
            "opponent": opponent,
            "home_away": ("Home" if is_home else "Away") if loc_known else None,
            "team_score": team_score,
            "opponent_score": opp_score,
            "result": result,
            "margin": margin,
            "opponent_ovr_rating": opp_ovr,
            "opponent_ovr_rank": opp_rank,
        })
 
    games_played = wins + losses + ties
    sos = round(sum(opponent_ratings) / len(opponent_ratings), 2) if opponent_ratings else None
    opponent_win_pct = (
        round(sum(opponent_win_pcts) / len(opponent_win_pcts), 4) if opponent_win_pcts else None
    )
 
    victory_ratings = [
        gl["opponent_ovr_rating"] for gl in game_log
        if gl["result"] == "W" and gl["opponent_ovr_rating"] is not None
    ]
    sov = round(sum(victory_ratings) / len(victory_ratings), 2) if victory_ratings else None
 
    biggest_win = None
    closest_game = None
    if game_log:
        win_games = [gl for gl in game_log if gl["result"] == "W"]
        if win_games:
            bw = max(win_games, key=lambda gl: gl["margin"])
            biggest_win = {"opponent": bw["opponent"], "margin": bw["margin"], "date": bw["date"]}
 
        cg = min(game_log, key=lambda gl: abs(gl["margin"]))
        closest_game = {
            "opponent": cg["opponent"],
            "margin": cg["margin"],
            "date": cg["date"],
            "result": cg["result"],
        }
 
    return {
        "school": school,
        "games_played": games_played,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "sos": sos,
        "sov": sov,
        "opponent_win_pct": opponent_win_pct,
        "quality_wins": quality_wins,
        "points_for": points_for,
        "points_against": points_against,
        "avg_margin": round((points_for - points_against) / games_played, 2) if games_played else None,
        "home_record": home_record if any(gl["home_away"] for gl in game_log) else None,
        "away_record": away_record if any(gl["home_away"] for gl in game_log) else None,
        "longest_win_streak": longest_streak(game_log),
        "biggest_win": biggest_win,
        "closest_game": closest_game,
        "game_log": game_log,
    }
 
 
def main():
    ratings_file = find_file(RATINGS_FILE_CANDIDATES, "ratings")
    games_file = find_file(GAMES_FILE_CANDIDATES, "games")
    print(f"Ratings: {ratings_file}\nGames:   {games_file}")
    ratings, league_average = load_ratings(ratings_file)
    games = load_games(games_file)
    records = build_team_records(games, ratings)
 
    # Warn (don't crash) on any team appearing in the scoreboard but missing
    # from the ratings file - this can happen with co-op/name mismatches
    # across different repos/seasons.
    scoreboard_teams = set()
    for g in games:
        scoreboard_teams.add(g["home_team"])
        scoreboard_teams.add(g["away_team"])
    missing = sorted(scoreboard_teams - set(ratings.keys()))
    if missing:
        print(f"WARNING: {len(missing)} team(s) in scoreboard not found in ratings file "
              f"(their games are still counted, but opponent OVR/rank will be null): {missing}")
 
    results = {}
    for school in ratings.keys():
        results[school] = process_team(school, games, ratings, records)
 
    # Rank teams by SOS (toughest schedule = rank 1). Teams with no SOS
    # (no games found) are excluded from ranking.
    ranked = sorted(
        [s for s in results if results[s]["sos"] is not None],
        key=lambda s: results[s]["sos"],
        reverse=True,
    )
    for i, school in enumerate(ranked, start=1):
        results[school]["sos_rank"] = i
    for school in results:
        if "sos_rank" not in results[school]:
            results[school]["sos_rank"] = None
 
    output = {
        "season": SEASON_YEAR,
        "last_updated": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "league_average_ovr": league_average,
        "quality_win_rank_threshold": QUALITY_WIN_RANK_THRESHOLD,
        "teams": results,
    }
 
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
 
    print(f"Wrote {OUTPUT_FILE}: {len(results)} teams, {len(games)} games processed.")
 
    no_games = [s for s in results if results[s]["games_played"] == 0]
    if no_games:
        print(f"NOTE: {len(no_games)} rated team(s) matched 0 games (likely name mismatch "
              f"between files): {no_games[:15]}{' ...' if len(no_games) > 15 else ''}")
 
    print("\nTop 10 SOS:")
    for school in ranked[:10]:
        r = results[school]
        print(f"  {r['sos_rank']:>3}. {school:<32} SOS {r['sos']:>7}  SOV {r['sov']}  "
              f"({r['wins']}-{r['losses']}{'-' + str(r['ties']) if r['ties'] else ''})")
 
 
if __name__ == "__main__":
    main()
