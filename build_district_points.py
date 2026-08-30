"""
District Points Calculator - Aggregation Script
=================================================

Ports the scoring formula from "District Points Calculation Spreadsheet -
2026.xlsx" (Team 1-7 sheets) to run automatically across every MSHSAA
football team, instead of one manually-selected district of 7 at a time.

SCORING FORMULA (verified against the original workbook's formulas,
cell-by-cell, in Team 1!T4:Y14):

  Per game, from TEAM's perspective vs OPPONENT:
    T (win/loss pts)   = 5 if TEAM forfeited this game
                          15 if TEAM lost this game in overtime
                          20 if TEAM won, 10 if TEAM lost (otherwise)
    U (class bonus)    = 10 x (opponent's class - TEAM's class), if positive, else 0
    V (opp win value)  = OPPONENT's season win total x 20
    W (opp loss value) = (OPPONENT's OT losses x 15) + (OPPONENT's other losses x 10)
    X (differential)   = TEAM's score - OPPONENT's score, capped to [-13, 13]
    Y (minus pts)      = 10 if TEAM won this game, 20 if TEAM lost (self-correction
                          term; deliberately the *inverse* of T's win/loss split,
                          confirmed from the original formula, not a bug)

  Season totals (sums over TEAM's played games so far, n = games played):
    avg_T = sum(T) / n
    avg_U = sum(U) / n
    avg_X = sum(X) / n
    SOS   = (sum(V) + sum(W) - sum(Y)) / (sum(opp wins) + sum(opp losses) - n)

  TOTAL POINTS = avg_T + avg_U + avg_X + SOS

DELIBERATE IMPROVEMENT OVER THE SPREADSHEET: the workbook has an
"# of Opp Overtime Losses" input (used in the W term) that was always
left at 0 -- filling it in by hand for every opponent every week wasn't
practical. Since this script now tracks the "overtime" flag for every
game statewide (via scrape_football_games_2026.py), each opponent's real
OT-loss count is used instead of assuming 0. Flag to Tyler: this means
scores here won't exactly reproduce old spreadsheet output for the same
games -- tell me if you'd rather force OT-loss count to 0 for parity.

DATA SOURCE CHOICE: this reads football_games_2026_all.json but only
keeps games where BOTH teams are classified (team1_classified and
team2_classified both true). Class and district lookups are required by
the formula, so a game against an unclassified opponent can't be scored
-- this matches what the original spreadsheet's VLOOKUPs would have done
(errored out) for the same case. Games are otherwise unfiltered by date;
"played" just means both scores are present.

Usage:
    python3 build_district_points.py
"""

import json
from collections import defaultdict

GAMES_PATH = "football_games_2026_all.json"
CLASS_PATH = "classifications.json"
OUTPUT_PATH = "district_points_2026.json"

POINTS = {
    "win": 20,
    "loss": 10,
    "ot_loss": 15,
    "forfeit": 5,
    "class_step": 10,
    "opp_win": 20,
    "opp_loss": 10,
    "opp_ot_loss": 15,
    "diff_cap": 13,
}


def load_classifications(path):
    data = json.load(open(path))
    team_class = {}
    team_district = {}
    for t in data["teams"]:
        team_class[t["school"]] = t["classification"]
        team_district[t["school"]] = t["district"]
    return team_class, team_district


def load_playable_games(path):
    """Games with both teams classified -- the only ones the formula can score."""
    games = json.load(open(path))
    return [g for g in games if g.get("team1_classified") and g.get("team2_classified")]


def build_schedules_and_records(games):
    """
    schedule[team] = list of that team's played games, from their own
    perspective (won/lost, their score, opponent's score, etc).
    record[team]   = {"wins", "losses", "ot_losses"} -- season totals used
    to look up OPPONENT quality when scoring someone else's game.
    """
    played = [g for g in games if g["score1"] is not None and g["score2"] is not None]

    record = defaultdict(lambda: {"wins": 0, "losses": 0, "ot_losses": 0})
    schedule = defaultdict(list)

    for g in played:
        sides = [
            (g["team1"], g["team2"], g["score1"], g["score2"]),
            (g["team2"], g["team1"], g["score2"], g["score1"]),
        ]
        for me, opp, my_score, opp_score in sides:
            won = my_score > opp_score
            schedule[me].append({
                "opponent": opp,
                "date": g["date"],
                "my_score": my_score,
                "opp_score": opp_score,
                "won": won,
                "overtime": g["overtime"],
                "forfeit": g["forfeit"],
            })
            if won:
                record[me]["wins"] += 1
            else:
                record[me]["losses"] += 1
                if g["overtime"]:
                    record[me]["ot_losses"] += 1

    return schedule, record


def compute_team_points(team, schedule, record, team_class):
    games = schedule.get(team, [])
    n = len(games)
    if n == 0:
        return None

    my_class = team_class.get(team)

    sum_T = sum_U = sum_X = 0.0
    sum_V = sum_W = sum_Y = 0.0
    sum_opp_wins_raw = sum_opp_losses_raw = 0
    per_game = []

    for g in games:
        opp = g["opponent"]
        opp_class = team_class.get(opp)
        opp_rec = record.get(opp, {"wins": 0, "losses": 0, "ot_losses": 0})

        # T: win/loss/OT-loss/forfeit points
        if g["forfeit"]:
            t_pts = POINTS["forfeit"]
        elif (not g["won"]) and g["overtime"]:
            t_pts = POINTS["ot_loss"]
        else:
            t_pts = POINTS["win"] if g["won"] else POINTS["loss"]
        sum_T += t_pts

        # U: playing-up-in-class bonus
        if my_class is not None and opp_class is not None and opp_class > my_class:
            u_pts = (opp_class - my_class) * POINTS["class_step"]
        else:
            u_pts = 0
        sum_U += u_pts

        # V/W: opponent quality (feeds the SOS term, not per-game points directly)
        opp_ot = opp_rec["ot_losses"]
        opp_l = opp_rec["losses"]
        opp_w = opp_rec["wins"]
        v_pts = opp_w * POINTS["opp_win"]
        w_pts = (opp_ot * POINTS["opp_ot_loss"]) + ((opp_l - opp_ot) * POINTS["opp_loss"])
        sum_V += v_pts
        sum_W += w_pts
        sum_opp_wins_raw += opp_w
        sum_opp_losses_raw += opp_l

        # X: score differential, capped +-13
        diff = g["my_score"] - g["opp_score"]
        x_pts = max(-POINTS["diff_cap"], min(POINTS["diff_cap"], diff))
        sum_X += x_pts

        # Y: self-correction minus-points (inverse of T's plain win/loss)
        y_pts = POINTS["loss"] if g["won"] else POINTS["win"]
        sum_Y += y_pts

        per_game.append({
            "date": g["date"],
            "opponent": opp,
            "opponent_class": opp_class,
            "opponent_record": {"wins": opp_w, "losses": opp_l, "ot_losses": opp_ot},
            "my_score": g["my_score"],
            "opp_score": g["opp_score"],
            "won": g["won"],
            "overtime": g["overtime"],
            "forfeit": g["forfeit"],
            "points": {
                "win_loss": round(t_pts, 4),
                "class_bonus": round(u_pts, 4),
                "differential": round(x_pts, 4),
            },
        })

    avg_T = sum_T / n
    avg_U = sum_U / n
    avg_X = sum_X / n

    denom = sum_opp_wins_raw + sum_opp_losses_raw - n
    sos_term = (sum_V + sum_W - sum_Y) / denom if denom != 0 else 0.0

    total = avg_T + avg_U + avg_X + sos_term

    return {
        "total_points": round(total, 4),
        "components": {
            "avg_win_loss": round(avg_T, 4),
            "avg_class_bonus": round(avg_U, 4),
            "avg_differential": round(avg_X, 4),
            "sos_term": round(sos_term, 4),
        },
        "games_played": n,
        "record": {"wins": record[team]["wins"], "losses": record[team]["losses"]},
        "games": per_game,
    }


def main():
    team_class, team_district = load_classifications(CLASS_PATH)
    games = load_playable_games(GAMES_PATH)
    schedule, record = build_schedules_and_records(games)

    districts = defaultdict(list)
    teams_with_no_games = []

    for team, cls in team_class.items():
        district = team_district.get(team)
        result = compute_team_points(team, schedule, record, team_class)
        if result is None:
            teams_with_no_games.append(team)
            continue
        entry = {
            "team": team,
            "class": cls,
            "district": district,
            **result,
        }
        key = f"class{cls}_district{district}"
        districts[key].append(entry)

    # Rank within each district
    output = {}
    for key, teams in districts.items():
        teams.sort(key=lambda t: t["total_points"], reverse=True)
        for i, t in enumerate(teams, start=1):
            t["rank"] = i
        output[key] = teams

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Districts: {len(output)}")
    print(f"Teams with points computed: {sum(len(v) for v in output.values())}")
    print(f"Teams with no played games yet (excluded): {len(teams_with_no_games)}")


if __name__ == "__main__":
    main()
