"""
Playoff Odds - Monte Carlo Simulation (v1)
==========================================

For every MSHSAA 11-man football team (Classes 1-6), estimates the chance of:
  - winning a DISTRICT TITLE
  - making the FINAL FOUR (reaching the state semifinals)
  - reaching the STATE CHAMPIONSHIP game (Show-Me Bowl)
  - winning the STATE TITLE

Each simulation plays out, in order:
  1. The rest of the regular season, scored with the district points formula
     (reuses simulate_district_seeds.py: same game model, same sigma, same
     MSHSAA head-to-head automatic switch when seeding).
  2. Each district tournament, from that simulation's seeds.
  3. The state bracket, from that simulation's district champions.
Because all three happen inside the same simulation, a team's playoff odds
already account for every way its seed can land.

PLAYOFF GAME MODEL
------------------
  P(team A beats team B) = Phi((ovr_A - ovr_B) / SIGMA)
Same normal-margin model as the regular season, just without ties (playoff
games always produce a winner). No home-field term, matching the ratings.

BRACKETS
--------
District bracket (by seed; missing seeds are byes, so in a 5-team district
seeds 1, 2 and 3 get byes):
    Round 1:  1 v 8   4 v 5   2 v 7   3 v 6
    Semis:    (1/8) v (4/5)    (2/7) v (3/6)
    Final:    district champion
State bracket (STATE_BRACKET_ORDER below, paired left to right each round):
    Quarterfinals:  D1 v D2   D3 v D4   D5 v D6   D7 v D8
    Semifinals (Final Four):  (D1/D2) v (D3/D4)   (D5/D6) v (D7/D8)
    Championship
MSHSAA rotates the quarterfinal pairings by season. If a class's 2026
pairings differ, edit STATE_BRACKET_ORDER for that class -- adjacent entries
meet in the quarterfinals, adjacent pairs meet in the semifinals.

REAL PLAYOFF RESULTS
--------------------
Once playoff games are in the games CSV (scored games dated after
REGULAR_SEASON_END), any simulated playoff game between the same two teams
uses the real winner instead of a coin flip. Games listed in
NON_PLAYOFF_GAMES (e.g. the Kirkwood-Webster Groves Thanksgiving game) are
never treated as playoff results.

DISPLAY RULE
------------
Every probability gets a "_display" string: never "100%" (shows ">99%"),
"<1%" for anything below 0.5% that still happened in at least one sim.

Usage:
    python3 simulate_playoff_odds.py                     # 10,000 sims, sigma 16
    python3 simulate_playoff_odds.py --sims 20000 --sigma 16 --seed 7

Needs simulate_district_seeds.py and build_district_points.py in the same folder.
"""
import argparse
import csv
import datetime
import json
import math
from collections import defaultdict

import numpy as np

import build_district_points as bdp
import simulate_district_seeds as sds

OUTPUT_PATH = "playoff_odds_2026.json"
OUTPUT_CSV_PATH = "playoff_odds_2026.csv"
DEFAULT_SIMS = 10000
DEFAULT_SIGMA = 16.0
CHUNK = 1000

# Seed slots in bracket order; adjacent slots meet, then adjacent winners, ...
DISTRICT_BRACKET_ORDER = [1, 8, 4, 5, 2, 7, 3, 6]

# District numbers in state-bracket order, per class (adjacent = quarterfinal opponents).
STATE_BRACKET_ORDER = {c: [1, 2, 3, 4, 5, 6, 7, 8] for c in range(1, 7)}

# Pairs of teams whose post-regular-season games are NOT playoff games.
NON_PLAYOFF_GAMES = [
    ("Kirkwood", "Webster Groves"),
]


# --------------------------------------------------------------------------

def fmt_pct(p, hits):
    """Display string. Never shows 100%."""
    if hits == 0:
        return "0%"
    if p >= 0.995:
        return ">99%"
    if p < 0.005:
        return "<1%"
    return f"{round(p * 100)}%"


def win_prob(diff, sigma):
    return 0.5 * (1.0 + np.vectorize(math.erf)(diff / (sigma * math.sqrt(2.0))))


def play_round(rng, slots, strength, sigma, results):
    """slots: [n, m] team positions (-1 = bye), m even. Returns winners [n, m/2]."""
    a, b = slots[:, 0::2], slots[:, 1::2]
    sa = strength[np.maximum(a, 0)]
    sb = strength[np.maximum(b, 0)]
    p = win_prob(sa - sb, sigma)
    a_wins = rng.random(a.shape) < p
    for (x, y), w in results.items():                  # real playoff results
        m1 = (a == x) & (b == y)
        m2 = (a == y) & (b == x)
        hit = m1 | m2
        if hit.any():
            a_wins[hit] = (a[hit] == w)
    win = np.where(a_wins, a, b)
    win = np.where(b < 0, a, win)                      # bye
    win = np.where(a < 0, b, win)
    return win


def load_playoff_results(season, regular_end):
    """{(pos_a, pos_b): winner_pos} for scored games after the regular season."""
    pos_of = {season.names[t]: i for i, t in enumerate(season.out_teams)}
    skip = {frozenset(p) for p in NON_PLAYOFF_GAMES}
    out = {}
    for g in bdp.load_games(bdp.GAMES_PATH):
        if g["score1"] is None or g["score2"] is None:
            continue
        if sds._parse_date(g["date"]) <= regular_end:
            continue
        if frozenset((g["team1"], g["team2"])) in skip:
            continue
        if g["team1"] not in pos_of or g["team2"] not in pos_of:
            continue
        if g["score1"] == g["score2"]:
            continue
        a, b = pos_of[g["team1"]], pos_of[g["team2"]]
        out[(a, b)] = a if g["score1"] > g["score2"] else b
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    team_class, team_district, oos_class, oos_records, games, ovr, ratings_doc = sds.load_inputs()
    season = sds.Season(team_class, team_district, oos_class, oos_records, games, ovr)
    results = load_playoff_results(season, sds.REGULAR_SEASON_END)
    print(f"Remaining regular-season games simulated: {len(season.remaining)}")
    print(f"Real playoff results locked in: {len(results)}")

    strength = season.strength[season.out_teams]       # by output position
    n_out = len(season.out_teams)
    rng = np.random.default_rng(args.seed)

    # district keys grouped by class
    by_class = defaultdict(dict)
    for (cls, dist) in season.districts:
        by_class[cls][dist] = (cls, dist)

    counts = {k: np.zeros(n_out, dtype=np.int64)
              for k in ("district_title", "final_four", "state_final", "state_title")}

    done = 0
    while done < args.sims:
        n = min(CHUNK, args.sims - done)
        margin, ot = season.simulate_margins(rng, n, args.sigma)
        pts = sds.break_ties(rng, season.points(margin, ot))

        champs = {}
        for key, positions in season.districts.items():
            pos = np.array(positions)
            order = np.argsort(-pts[:, pos], axis=1)
            order = sds.apply_switches(order, sds.beat_matrix(margin, season.h2h[key], len(pos)))
            seeded = pos[order]                         # [n, k] team position at each seed
            k = len(pos)
            slots = np.full((n, len(DISTRICT_BRACKET_ORDER)), -1, dtype=np.int64)
            for j, seed in enumerate(DISTRICT_BRACKET_ORDER):
                if seed <= k:
                    slots[:, j] = seeded[:, seed - 1]
            alive = slots
            while alive.shape[1] > 1:
                alive = play_round(rng, alive, strength, args.sigma, results)
            champ = alive[:, 0]
            np.add.at(counts["district_title"], champ, 1)
            champs[key] = champ

        for cls, dists in by_class.items():
            order_d = [d for d in STATE_BRACKET_ORDER.get(cls, sorted(dists)) if d in dists]
            alive = np.stack([champs[dists[d]] for d in order_d], axis=1)
            while alive.shape[1] > 1:
                if alive.shape[1] == 4:
                    np.add.at(counts["final_four"], alive.ravel(), 1)
                if alive.shape[1] == 2:
                    np.add.at(counts["state_final"], alive.ravel(), 1)
                alive = play_round(rng, alive, strength, args.sigma, results)
            np.add.at(counts["state_title"], alive[:, 0], 1)
        done += n

    # ---------------------------------------------------------------- output
    rec = defaultdict(lambda: [0, 0])
    for g in season.played:
        if sds._parse_date(g["date"]) > sds.REGULAR_SEASON_END:
            continue
        w1 = g["score1"] > g["score2"]
        rec[g["team1"]][0 if w1 else 1] += 1
        rec[g["team2"]][1 if w1 else 0] += 1

    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "ratings_last_updated": ratings_doc.get("last_updated"),
        "simulations": args.sims,
        "sigma": args.sigma,
        "remaining_games_simulated": len(season.remaining),
        "playoff_results_locked": len(results),
        "state_bracket_order": STATE_BRACKET_ORDER,
        "classes": {},
    }
    rows = []
    for cls in sorted(by_class):
        teams = []
        for dist in sorted(by_class[cls]):
            for p in season.districts[(cls, dist)]:
                name = season.names[season.out_teams[p]]
                t = {"team": name, "district": dist, "record": f"{rec[name][0]}-{rec[name][1]}",
                     "ovr_rating": round(float(strength[p]), 2)}
                for k, c in counts.items():
                    prob = c[p] / args.sims
                    t[k] = round(float(prob), 4)
                    t[k + "_display"] = fmt_pct(prob, c[p])
                teams.append(t)
        teams.sort(key=lambda t: (-t["state_title"], -t["final_four"], -t["district_title"]))
        out["classes"][f"class{cls}"] = {"class": cls, "teams": teams}
        for t in teams:
            rows.append({"class": cls, **t})

    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    fields = ["class", "district", "team", "record", "ovr_rating",
              "district_title", "district_title_display", "final_four", "final_four_display",
              "state_final", "state_final_display", "state_title", "state_title_display"]
    with open(OUTPUT_CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUTPUT_PATH} and {OUTPUT_CSV_PATH}")

    # sanity: each class should sum to 1 title, 4 final-four spots, 8 district titles
    for cls in sorted(by_class):
        ts = out["classes"][f"class{cls}"]["teams"]
        print(f"  Class {cls}: titles {sum(t['state_title'] for t in ts):.3f}  "
              f"final four {sum(t['final_four'] for t in ts):.3f}  "
              f"district titles {sum(t['district_title'] for t in ts):.3f}  | favorite: "
              f"{ts[0]['team']} {ts[0]['state_title_display']}")


if __name__ == "__main__":
    main()
