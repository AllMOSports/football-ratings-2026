"""
build_quad_records_football_2026.py

Quad 1-4 records for every rated Missouri football team (2026 season).

How quads work
--------------
- For each class (1-6), the OVR ratings of that class's teams are split into
  quartiles. The 75th / 50th / 25th percentile rating values are that class's
  quad cutoffs.
- Every opponent is judged against the cutoffs of the TEAM'S OWN class, not
  the opponent's class:
      opp OVR >= 75th pct  -> Quad 1
      opp OVR >= 50th pct  -> Quad 2
      opp OVR >= 25th pct  -> Quad 3
      otherwise            -> Quad 4
  A rating exactly on a cutoff goes to the higher (better) quad.
  So one game can be Q1 for one side and Q3 for the other; that's intended.
- Quads use CURRENT ratings, recomputed every run, so a game's quad can move
  as the season plays out (NCAA NET-style).

What counts
-----------
- All played regular-season AND postseason games in football_games_2026_all.json.
- Out-of-state / non-MSHSAA-classified opponents are NOT counted at all.
- Forfeits are skipped by default (same as the ratings script). Flip
  COUNT_FORFEITS to True to count them.
- Unplayed games (no score) are skipped.

Inputs  (repo root): football_ratings_2026.json, football_games_2026_all.json
Outputs (repo root): quad_records_football_2026.json, quad_records_football_2026.csv

Standard library only.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

RATINGS_PATH = "football_ratings_2026.json"
GAMES_PATH = "football_games_2026_all.json"
OUTPUT_JSON_PATH = "quad_records_football_2026.json"
OUTPUT_CSV_PATH = "quad_records_football_2026.csv"

COUNT_FORFEITS = False
QUADS = ("q1", "q2", "q3", "q4")


def percentile(sorted_vals, pct):
    """Linear-interpolated percentile (same method as numpy's default)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def build_cutoffs(teams):
    """{class: {"q1": 75th pct, "q2": 50th pct, "q3": 25th pct, "team_count": n}}"""
    by_class = defaultdict(list)
    for t in teams:
        if t.get("classification") is None or t.get("ovr_rating") is None:
            continue
        by_class[t["classification"]].append(float(t["ovr_rating"]))

    cutoffs = {}
    for cls, vals in by_class.items():
        vals.sort()
        cutoffs[cls] = {
            "q1": round(percentile(vals, 75), 2),
            "q2": round(percentile(vals, 50), 2),
            "q3": round(percentile(vals, 25), 2),
            "team_count": len(vals),
        }
    return cutoffs


def quad_for(opp_ovr, cut):
    if opp_ovr >= cut["q1"]:
        return "q1"
    if opp_ovr >= cut["q2"]:
        return "q2"
    if opp_ovr >= cut["q3"]:
        return "q3"
    return "q4"


def fmt(rec):
    return f"{rec[0]}-{rec[1]}"


def main():
    with open(RATINGS_PATH, encoding="utf-8") as f:
        ratings = json.load(f)
    with open(GAMES_PATH, encoding="utf-8") as f:
        games = json.load(f)

    teams = ratings["teams"]
    by_school = {t["school"]: t for t in teams}
    cutoffs = build_cutoffs(teams)

    # records[school][quad] = [wins, losses]
    records = {s: {q: [0, 0] for q in QUADS} for s in by_school}
    game_log = defaultdict(list)

    skipped = defaultdict(int)

    for g in games:
        s1, s2 = g.get("score1"), g.get("score2")
        if s1 is None or s2 is None:
            skipped["unplayed"] += 1
            continue
        if g.get("forfeit") and not COUNT_FORFEITS:
            skipped["forfeit"] += 1
            continue
        if not g.get("team1_classified") or not g.get("team2_classified"):
            skipped["out_of_state"] += 1
            continue
        if s1 == s2:
            skipped["tie"] += 1
            continue

        t1, t2 = g["team1"], g["team2"]
        r1, r2 = by_school.get(t1), by_school.get(t2)
        if r1 is None or r2 is None:
            # Classified but missing from ratings -- treat like unrated.
            skipped["unrated_team"] += 1
            print(f"  ! unrated team in game: {t1} vs {t2} ({g.get('date')})")
            continue

        for team, opp, ts, os_ in ((r1, r2, s1, s2), (r2, r1, s2, s1)):
            cut = cutoffs.get(team["classification"])
            if cut is None:
                continue
            q = quad_for(float(opp["ovr_rating"]), cut)
            won = ts > os_
            records[team["school"]][q][0 if won else 1] += 1
            game_log[team["school"]].append({
                "date": g.get("date"),
                "week": g.get("week"),
                "opponent": opp["school"],
                "opp_class": opp["classification"],
                "opp_ovr": opp["ovr_rating"],
                "quad": q.upper(),
                "result": "W" if won else "L",
                "score": f"{ts}-{os_}",
            })

    out_teams = []
    for t in sorted(teams, key=lambda x: (x["classification"], x.get("ovr_rank", 9999))):
        rec = records[t["school"]]
        out_teams.append({
            "school": t["school"],
            **({"slug": t["slug"]} if t.get("slug") else {}),
            "classification": t["classification"],
            "district": t.get("district"),
            "ovr_rank": t.get("ovr_rank"),
            "ovr_rating": t.get("ovr_rating"),
            "q1": fmt(rec["q1"]),
            "q2": fmt(rec["q2"]),
            "q3": fmt(rec["q3"]),
            "q4": fmt(rec["q4"]),
            "q1_wins": rec["q1"][0],
            "q1_losses": rec["q1"][1],
            "q2_wins": rec["q2"][0],
            "q2_losses": rec["q2"][1],
            "q3_wins": rec["q3"][0],
            "q3_losses": rec["q3"][1],
            "q4_wins": rec["q4"][0],
            "q4_losses": rec["q4"][1],
            "games": sorted(game_log[t["school"]], key=lambda x: (x["date"] or "")),
        })

    now = datetime.now(ZoneInfo("America/Chicago"))
    payload = {
        "season": 2026,
        "sport": "football",
        "last_updated": now.strftime("%B %d, %Y at %I:%M %p"),
        "ratings_last_updated": ratings.get("last_updated"),
        "method": (
            "Opponent quads use the 75th/50th/25th percentile OVR rating of the "
            "team's own class. Current ratings. Regular season + postseason. "
            "Out-of-state opponents and forfeits not counted."
        ),
        "cutoffs": {str(k): v for k, v in sorted(cutoffs.items())},
        "teams": out_teams,
    }

    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    cols = ["classification", "district", "ovr_rank", "school", "ovr_rating",
            "q1", "q2", "q3", "q4"]
    with open(OUTPUT_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_teams)

    print("Quad cutoffs (OVR) by class:")
    for cls, c in sorted(cutoffs.items()):
        print(f"  Class {cls} ({c['team_count']} teams): "
              f"Q1 >= {c['q1']}, Q2 >= {c['q2']}, Q3 >= {c['q3']}")
    counted = sum(len(v) for v in game_log.values()) // 2
    print(f"Games counted: {counted}; skipped: {dict(skipped)}")
    print(f"Wrote {OUTPUT_JSON_PATH} and {OUTPUT_CSV_PATH} ({len(out_teams)} teams)")


if __name__ == "__main__":
    main()
