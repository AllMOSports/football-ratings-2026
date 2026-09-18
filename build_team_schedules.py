"""
Team Schedules Builder - District Points Calculator (interactive page)
=========================================================================
 
build_district_points.py only ever looks at PLAYED games -- that's correct
for computing real points, but the interactive calculator page needs each
team's FULL season, including games that haven't happened yet (so it has
something to show a "Manually Enter Score" / "Auto Populate" choice next
to). This script produces that: one entry per MSHSAA team, with every game
on their schedule, played or not, plus their current real total points/rank
where available (pulled straight from district_points_2026.json so the page
doesn't have to recompute anything that's already been computed correctly).
 
WHAT THIS DOES NOT DO: no predictions, no ratings. Deliberately kept
separate from ratings.json -- the page fetches that directly at load time
so predictions always use whatever the ratings pipeline most recently
computed, rather than this script baking in a snapshot that goes stale the
moment ratings next update. This script only answers "what does this team's
schedule look like," not "what will the unplayed games probably score."
 
OUTPUT SCHEMA -- team_schedules_2026.json:
    {
      "<team name>": {
        "class": int,
        "district": int,
        "current_total_points": float | null,   // null if 0 games played
        "current_rank": int | null,
        "games": [
          {
            "date": "8/28/2026",
            "opponent": "...",
            "opponent_type": "in_state" | "out_of_state" | "unknown",
            "opponent_class": int | null,
            "played": true | false,
            "my_score": int | null,
            "opp_score": int | null,
            "won": true | false | null,
            "overtime": bool,
            "forfeit": bool
          },
          ...
        ]
      },
      ...
    }
 
"opponent_type" tells the page whether an auto-populate prediction is even
possible: "in_state" opponents have a rating to predict from; "out_of_state"
and "unknown" opponents don't, so those games should force manual entry
(and for out_of_state specifically, both a score AND the opponent's
win/loss record -- see the earlier out-of-state design discussion).
 
Usage:
    python3 build_team_schedules.py
"""
 
import csv
import json
 
GAMES_PATH = "football_games_2026_all_district_points.csv"
CLASS_PATH = "classifications.json"
OOS_CLASS_PATH = "out_of_state_classifications.json"
DISTRICT_POINTS_PATH = "district_points_2026.json"
OUTPUT_PATH = "team_schedules_2026.json"
 
 
def _to_bool(s):
    return str(s).strip().upper() == "TRUE"
 
 
def _to_int_or_none(s):
    s = (s or "").strip()
    return int(s) if s != "" else None
 
 
def load_classifications(path):
    data = json.load(open(path))
    team_class = {}
    team_district = {}
    for t in data["teams"]:
        team_class[t["school"]] = t["classification"]
        team_district[t["school"]] = t["district"]
    return team_class, team_district
 
 
def load_out_of_state_classifications(path):
    data = json.load(open(path))
    return {name: info["class"] for name, info in data.items()}
 
 
def load_games(path):
    """Identical to build_district_points.py's load_games -- every row, played or not."""
    games = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            games.append({
                "date": row["date"],
                "team1": row["team1"],
                "score1": _to_int_or_none(row["score1"]),
                "team2": row["team2"],
                "score2": _to_int_or_none(row["score2"]),
                "forfeit": _to_bool(row["forfeit"]),
                "overtime": _to_bool(row["overtime"]),
            })
    return games
 
 
def load_current_points(path):
    """
    {team_name: {"total_points": float, "rank": int}} flattened out of
    district_points_2026.json's class/district grouping. Tolerant of the
    file not existing (pipeline hasn't run yet) -- every team just gets
    null current points, same as a team with 0 games played would.
    """
    try:
        data = json.load(open(path))
    except FileNotFoundError:
        print(f"NOTE: {path} not found -- no current points/rank will be included.")
        return {}
    current = {}
    for teams in data.values():
        for t in teams:
            current[t["team"]] = {"total_points": t["total_points"], "rank": t["rank"]}
    return current
 
 
def classify_opponent(opp, team_class, oos_class):
    if opp in team_class:
        return "in_state", team_class[opp]
    if opp in oos_class:
        return "out_of_state", oos_class[opp]
    return "unknown", None
 
 
def main():
    team_class, team_district = load_classifications(CLASS_PATH)
    oos_class = load_out_of_state_classifications(OOS_CLASS_PATH)
    games = load_games(GAMES_PATH)
    current_points = load_current_points(DISTRICT_POINTS_PATH)
 
    schedules = {}
    for team in team_class:
        schedules[team] = {
            "class": team_class[team],
            "district": team_district.get(team),
            "current_total_points": current_points.get(team, {}).get("total_points"),
            "current_rank": current_points.get(team, {}).get("rank"),
            "games": [],
        }
 
    for g in games:
        sides = [
            (g["team1"], g["team2"], g["score1"], g["score2"]),
            (g["team2"], g["team1"], g["score2"], g["score1"]),
        ]
        for me, opp, my_score, opp_score in sides:
            if me not in schedules:
                continue  # only building entries for real MSHSAA teams
 
            played = my_score is not None and opp_score is not None
            opp_type, opp_class = classify_opponent(opp, team_class, oos_class)
 
            won = None
            if played:
                won = my_score > opp_score
 
            schedules[me]["games"].append({
                "date": g["date"],
                "opponent": opp,
                "opponent_type": opp_type,
                "opponent_class": opp_class,
                "played": played,
                "my_score": my_score,
                "opp_score": opp_score,
                "won": won,
                "overtime": g["overtime"] if played else False,
                "forfeit": g["forfeit"] if played else False,
            })
 
    # Keep each team's games in schedule order (the CSV's own row order
    # already reflects the season's date order within each team's slice).
    with open(OUTPUT_PATH, "w") as f:
        json.dump(schedules, f, indent=2)
 
    total_games = sum(len(v["games"]) for v in schedules.values())
    played_games = sum(1 for v in schedules.values() for gm in v["games"] if gm["played"])
    remaining_games = total_games - played_games
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Teams: {len(schedules)}")
    print(f"Total game-entries across all teams: {total_games} "
          f"({played_games} played, {remaining_games} remaining)")
 
 
if __name__ == "__main__":
    main()
