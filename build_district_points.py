"""
District Points Calculator - Aggregation Script (v2)
=======================================================
 
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
 
  Season totals:
    avg_T = sum(T) / n_total      (n_total = ALL games TEAM has played, any opponent)
    avg_U = sum(U) / n_total
    avg_X = sum(X) / n_total
    SOS   = (sum(V) + sum(W) - sum(Y)) / (sum(opp wins) + sum(opp losses) - n_sos)
            (n_sos = only games where the opponent's win/loss record is actually
            known -- see "OUT-OF-STATE OPPONENTS" below for why this is a
            different count than n_total)
 
  TOTAL POINTS = avg_T + avg_U + avg_X + SOS
 
v2 CHANGES FROM THE FIRST VERSION
-----------------------------------
1. BUG FIX: the old version filtered OUT any game where the opponent wasn't
   classified before building a team's schedule at all -- which meant a
   team's own win/loss/differential average (T, X) was computed from a
   SHORTENED schedule, undercounting their real games played. T and X don't
   need anything about the opponent except score and result, so every played
   game now counts toward them, regardless of who the opponent is.
2. OUT-OF-STATE CLASSIFICATIONS ADDED: out_of_state_classifications.json
   (built from MSHSAA's enrollment-based methodology, 75%-adjusted enrollment
   compared to Missouri's class breaks, with a couple of manual overrides Tyler
   confirmed directly -- see that file's "note" field) is now used for the
   class-bonus (U) term on out-of-state games. This was previously impossible;
   those games contributed U=0 by omission.
 
OUT-OF-STATE OPPONENTS: WHY THEY STILL DON'T FEED THE SOS TERM
------------------------------------------------------------------
We have a real classification for out-of-state opponents now, but NOT a real
win/loss record -- we only ever see them through however many MSHSAA teams
happen to play them, which is a tiny, incomplete slice of their actual season
(they mostly play other out-of-state teams we never scrape). Building a
"record" from that slice would be actively wrong, not just incomplete, so
out-of-state opponents are deliberately excluded from the V/W sums AND from
n_sos (the SOS term's own game count) -- both numerator and denominator skip
them together, which keeps the SOS term mathematically consistent instead of
penalizing a team for playing a crossover game. This also naturally handles
Tyler's call to skip the OT-loss refinement for out-of-state opponents: since
they never enter the SOS sums at all, there's nothing to special-case.
Per-game output still labels each game "in_state" / "out_of_state" / "unknown"
so this is visible rather than silently baked in.
 
CLASSIFICATION LOOKUP ORDER: in-state classifications.json first, then
out_of_state_classifications.json. A name in neither (e.g. an unresolved
co-op gap) gets class=None -> contributes U=0 for that game and is excluded
from n_sos, same treatment as an out-of-state opponent minus the class bonus.
 
DATA SOURCE: reads football_games_2026_all_district_points.csv (Tyler's
GitHub copy of the scraper's >=1-classified-team output). "Played" means
both scores are present; forfeit/overtime flags come straight from the CSV.
 
Usage:
    python3 build_district_points.py
"""
 
import csv
import json
from collections import defaultdict
 
GAMES_PATH = "football_games_2026_all_district_points.csv"
CLASS_PATH = "classifications.json"
OOS_CLASS_PATH = "out_of_state_classifications.json"
OOS_RECORDS_PATH = "out_of_state_records.json"
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
    """{team_name: class_int} -- flattens the richer {enrollment, class, note} records."""
    data = json.load(open(path))
    return {name: info["class"] for name, info in data.items()}
 
 
def load_out_of_state_records(path):
    """
    {team_name: {"wins": int, "losses": int}} from scrape_out_of_state_records.py.
    Tolerant of the file not existing yet (e.g. first run, before that
    scraper has ever been executed) -- out-of-state opponents just fall
    back to contributing no SOS value, same as before this was added.
    """
    try:
        data = json.load(open(path))
    except FileNotFoundError:
        print(f"NOTE: {path} not found -- out-of-state opponents will get a "
              f"class bonus but no SOS contribution until it's generated.")
        return {}
    return {name: {"wins": info["wins"], "losses": info["losses"]} for name, info in data.items()}
 
 
def load_games(path):
    """
    Every row from the games CSV, both-classified or not -- classification
    filtering used to happen here, but T/X don't need it, so it doesn't
    belong at load time anymore. Types are coerced out of CSV's all-strings.
    """
    games = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            games.append({
                "date": row["date"],
                "team1": row["team1"],
                "team1_classified": _to_bool(row["team1_classified"]),
                "score1": _to_int_or_none(row["score1"]),
                "team2": row["team2"],
                "team2_classified": _to_bool(row["team2_classified"]),
                "score2": _to_int_or_none(row["score2"]),
                "forfeit": _to_bool(row["forfeit"]),
                "overtime": _to_bool(row["overtime"]),
            })
    return games
 
 
def build_schedules_and_records(games):
    """
    schedule[team] = list of that team's played games, from their own
    perspective -- built for EVERY team name seen, in-state or not, since
    we don't know in advance who'll be looked up as an opponent.
    record[team]   = {"wins","losses","ot_losses"} season totals. Only
    trustworthy for in-state teams (we see their whole schedule); an entry
    may exist for an out-of-state name too, but callers must not use it --
    see compute_team_points, which only reads record[] for in-state opponents.
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
 
 
def compute_team_points(team, schedule, record, team_class, oos_class, oos_records):
    games = schedule.get(team, [])
    n_total = len(games)
    if n_total == 0:
        return None
 
    my_class = team_class.get(team)
 
    sum_T = sum_U = sum_X = 0.0
    sum_V = sum_W = sum_Y = 0.0
    sum_opp_wins_raw = sum_opp_losses_raw = 0
    n_sos = 0
    per_game = []
 
    for g in games:
        opp = g["opponent"]
        opp_in_state = opp in team_class
        opp_oos_record = oos_records.get(opp) if not opp_in_state else None
 
        if opp_in_state:
            opp_class = team_class.get(opp)
            source = "in_state"
        elif opp in oos_class:
            opp_class = oos_class.get(opp)
            source = "out_of_state" if opp_oos_record else "out_of_state_no_record"
        else:
            opp_class = None
            source = "unknown"
 
        # T: win/loss/OT-loss/forfeit points -- doesn't need opp info at all
        if g["forfeit"]:
            t_pts = POINTS["forfeit"]
        elif (not g["won"]) and g["overtime"]:
            t_pts = POINTS["ot_loss"]
        else:
            t_pts = POINTS["win"] if g["won"] else POINTS["loss"]
        sum_T += t_pts
 
        # U: playing-up-in-class bonus -- works for out-of-state opponents too
        if my_class is not None and opp_class is not None and opp_class > my_class:
            u_pts = (opp_class - my_class) * POINTS["class_step"]
        else:
            u_pts = 0
        sum_U += u_pts
 
        # X: score differential, capped +-13 -- doesn't need opp info at all
        diff = g["my_score"] - g["opp_score"]
        x_pts = max(-POINTS["diff_cap"], min(POINTS["diff_cap"], diff))
        sum_X += x_pts
 
        # V/W/Y: opponent quality (SOS term) -- needs a real win/loss record.
        # In-state opponents always have one (their full schedule is scraped
        # directly). Out-of-state opponents only have one once
        # scrape_out_of_state_records.py has found them; per Tyler's call,
        # their OT-loss count is always treated as 0 (not scraped, and not
        # worth the added complexity for how few of these games there are).
        # Anyone else (out-of-state with no record yet, or fully unknown)
        # is skipped here AND excluded from n_sos, together, so the SOS
        # term stays mathematically consistent rather than penalizing a
        # team for playing a game we can't fully price yet.
        opp_w = opp_l = opp_ot = 0
        has_record = False
        if opp_in_state:
            opp_rec = record.get(opp, {"wins": 0, "losses": 0, "ot_losses": 0})
            opp_w, opp_l, opp_ot = opp_rec["wins"], opp_rec["losses"], opp_rec["ot_losses"]
            has_record = True
        elif opp_oos_record:
            opp_w, opp_l, opp_ot = opp_oos_record["wins"], opp_oos_record["losses"], 0
            has_record = True
 
        if has_record:
            v_pts = opp_w * POINTS["opp_win"]
            w_pts = (opp_ot * POINTS["opp_ot_loss"]) + ((opp_l - opp_ot) * POINTS["opp_loss"])
            y_pts = POINTS["loss"] if g["won"] else POINTS["win"]
 
            sum_V += v_pts
            sum_W += w_pts
            sum_Y += y_pts
            sum_opp_wins_raw += opp_w
            sum_opp_losses_raw += opp_l
            n_sos += 1
 
        per_game.append({
            "date": g["date"],
            "opponent": opp,
            "opponent_classification_source": source,
            "opponent_class": opp_class,
            "opponent_record": (
                {"wins": opp_w, "losses": opp_l, "ot_losses": opp_ot} if has_record else None
            ),
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
 
    avg_T = sum_T / n_total
    avg_U = sum_U / n_total
    avg_X = sum_X / n_total
 
    denom = sum_opp_wins_raw + sum_opp_losses_raw - n_sos
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
        "games_played": n_total,
        "games_counted_in_sos": n_sos,
        "record": {"wins": record[team]["wins"], "losses": record[team]["losses"]},
        "games": per_game,
    }
 
 
def main():
    team_class, team_district = load_classifications(CLASS_PATH)
    oos_class = load_out_of_state_classifications(OOS_CLASS_PATH)
    oos_records = load_out_of_state_records(OOS_RECORDS_PATH)
    games = load_games(GAMES_PATH)
    schedule, record = build_schedules_and_records(games)
 
    districts = defaultdict(list)
    teams_with_no_games = []
    total_oos_games_scored = 0
    total_oos_games_no_record = 0
    total_unknown_games = 0
 
    for team, cls in team_class.items():
        district = team_district.get(team)
        result = compute_team_points(team, schedule, record, team_class, oos_class, oos_records)
        if result is None:
            teams_with_no_games.append(team)
            continue
 
        for g in result["games"]:
            if g["opponent_classification_source"] == "out_of_state":
                total_oos_games_scored += 1
            elif g["opponent_classification_source"] == "out_of_state_no_record":
                total_oos_games_no_record += 1
            elif g["opponent_classification_source"] == "unknown":
                total_unknown_games += 1
 
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
    print(f"Games scored with an out-of-state opponent, WITH a record (full SOS contribution): {total_oos_games_scored}")
    print(f"Games scored with an out-of-state opponent, NO record yet (class bonus only): {total_oos_games_no_record}")
    print(f"Games with a fully unknown opponent (no class, no SOS contribution): {total_unknown_games}")
 
 
if __name__ == "__main__":
    main()
