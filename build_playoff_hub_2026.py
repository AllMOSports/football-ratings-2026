"""
Playoff Hub - nightly data builder (v1)
=======================================

Writes playoff_hub_2026.json: everything the Playoff Hub snippet needs for all
six classes, in one file -- the seeded district brackets, every team's chance
to survive each round, and any real playoff results.

SEEDS (per district, first match wins)
  1. official_seeds_2026.json   -- MSHSAA's published seeds, typed in once:
        {"class1_district1": ["Thayer", "Portageville", "Charleston", ...], ...}
     (team names exactly as in the games file, seed 1 first). Any district
     listed here is marked "official" on the page.
  2. Calculated from district points with the same code as the District Points
     page (build_district_points formula + MSHSAA head-to-head switch, via
     simulate_district_seeds). If every regular-season game is in, these are
     marked "final"; if games are still missing, the seeds are the average
     simulated finish and marked "projected" so the page can say so.

Regular-season scores missing from football_games_2026_all_district_points.csv
are filled in from football_games_2026_all.json (the scraper's file), so seeds
don't wait on the manual CSV upload.

ODDS
  With the seeds fixed, the bracket is played --sims times with the same game
  model as simulate_playoff_odds.py (P(A beats B) = Phi((ovr_A - ovr_B)/sigma),
  no home field). Real playoff results from football_games_2026_all.json are
  locked in: the real winner advances in every simulation, so eliminated teams
  drop to 0% automatically.

  Per team, "reach" is the chance to still be alive at the START of each stage:
    [0] District semifinal    (1.0 for a bye)
    [1] District final
    [2] District champion     -> quarterfinal
    [3] Final Four            (won the quarterfinal)
    [4] State championship game
    [5] State champion
  Every probability also gets a display string (never "100%", ">99%" instead).

Usage:
    python3 build_playoff_hub_2026.py
    python3 build_playoff_hub_2026.py --sims 20000 --sigma 16 --seed 7
    DATA_DIR=../All_MO_Sports-Data python3 build_playoff_hub_2026.py
Needs simulate_district_seeds.py, simulate_playoff_odds.py, build_district_points.py
and build_matchup_previews_2026.py (slug lookup) in the same folder.
"""
import argparse
import datetime
import json
import math
import os
from collections import defaultdict

import numpy as np

import build_district_points as bdp
import simulate_district_seeds as sds
import simulate_playoff_odds as spo
from build_matchup_previews_2026 import LOGO_URL, SlugResolver, load_data_file

OUTPUT_PATH = "playoff_hub_2026.json"
OFFICIAL_SEEDS_PATH = "official_seeds_2026.json"
GAMES_JSON_PATH = "football_games_2026_all.json"
RATINGS_PATH = "football_ratings_2026.json"
DEFAULT_SIMS = 20000
DEFAULT_SIGMA = spo.DEFAULT_SIGMA
SEED_SIMS = 4000                       # only used if regular-season games are missing
STAGES = ["District semifinal", "District final", "District champion",
          "Final Four", "State final", "State champion"]


# --------------------------------------------------------------------------
# Games: fill missing regular-season scores in the CSV from the scraper JSON

def _gkey(date, a, b):
    return (bdp._normalize_date(str(date)), frozenset((a, b)))


def load_json_games():
    with open(GAMES_JSON_PATH) as f:
        return json.load(f)


def patched_loader(json_games):
    scored = {}
    for g in json_games:
        if g.get("score1") is None or g.get("score2") is None:
            continue
        scored[_gkey(g["date"], g["team1"], g["team2"])] = g
    original = bdp.load_games

    def load_games(path):
        games = original(path)
        filled = 0
        for g in games:
            if g["score1"] is not None and g["score2"] is not None:
                continue
            j = scored.get(_gkey(g["date"], g["team1"], g["team2"]))
            if not j:
                continue
            same = j["team1"] == g["team1"]
            g["score1"] = int(j["score1"] if same else j["score2"])
            g["score2"] = int(j["score2"] if same else j["score1"])
            g["forfeit"] = bool(j.get("forfeit", False))
            g["overtime"] = bool(j.get("overtime", False))
            filled += 1
        if filled:
            print(f"Filled {filled} regular-season scores from {GAMES_JSON_PATH}")
        return games
    return load_games


# --------------------------------------------------------------------------
# Seeds

def calculated_seeds(season, rng, sigma):
    """{(cls, dist): ([positions in seed order], status)}"""
    n = 1 if not season.remaining else SEED_SIMS
    margin, ot = season.simulate_margins(rng, n, sigma)
    pts = sds.break_ties(rng, season.points(margin, ot))
    out = {}
    for key, positions in season.districts.items():
        pos = np.array(positions)
        order = np.argsort(-pts[:, pos], axis=1)
        order = sds.apply_switches(order, sds.beat_matrix(margin, season.h2h[key], len(pos)))
        if n == 1:
            out[key] = (list(pos[order[0]]), "final")
        else:
            seat = np.empty_like(order)
            np.put_along_axis(seat, order, np.arange(len(pos))[None, :], axis=1)
            avg = seat.mean(axis=0)
            out[key] = (list(pos[np.argsort(avg, kind="stable")]), "projected")
    return out


def official_seeds(season):
    if not os.path.exists(OFFICIAL_SEEDS_PATH):
        return {}
    with open(OFFICIAL_SEEDS_PATH) as f:
        doc = json.load(f)
    pos_of = {season.names[t]: i for i, t in enumerate(season.out_teams)}
    out = {}
    for key, names in doc.items():
        if key.startswith("_"):
            continue
        cls, dist = key.replace("class", "").split("_district")
        bad = [n for n in names if n not in pos_of]
        if bad:
            raise SystemExit(f"{OFFICIAL_SEEDS_PATH} {key}: unknown team name(s) {bad}")
        out[(int(cls), int(dist))] = ([pos_of[n] for n in names], "official")
    return out


# --------------------------------------------------------------------------
# Real playoff results (from the scraper JSON, not the manual CSV)

def playoff_results(season, json_games):
    pos_of = {season.names[t]: i for i, t in enumerate(season.out_teams)}
    skip = {frozenset(p) for p in spo.NON_PLAYOFF_GAMES}
    locked, shown = {}, []
    for g in json_games:
        if g.get("score1") is None or g.get("score2") is None:
            continue
        if sds._parse_date(str(g["date"])) <= sds.REGULAR_SEASON_END:
            continue
        if frozenset((g["team1"], g["team2"])) in skip:
            continue
        if g["team1"] not in pos_of or g["team2"] not in pos_of or g["score1"] == g["score2"]:
            continue
        a, b = pos_of[g["team1"]], pos_of[g["team2"]]
        locked[(a, b)] = a if g["score1"] > g["score2"] else b
        shown.append({"date": bdp._normalize_date(str(g["date"])), "teams": [g["team1"], g["team2"]],
                      "score": [int(g["score1"]), int(g["score2"])],
                      "overtime": bool(g.get("overtime")), "forfeit": bool(g.get("forfeit"))})
    return locked, shown


def fmt(p):
    if p <= 0:
        return "0%"
    if p >= 0.995:
        return ">99%"
    if p < 0.005:
        return "<1%"
    return f"{round(p * 100)}%"


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    json_games = load_json_games()
    bdp.load_games = patched_loader(json_games)

    tc, td, oc, orc, games, ovr, ratings_doc = sds.load_inputs()
    season = sds.Season(tc, td, oc, orc, games, ovr)
    print(f"Regular-season games still missing a score: {len(season.remaining)}")

    seeds = calculated_seeds(season, rng, args.sigma)
    seeds.update(official_seeds(season))
    locked, shown = playoff_results(season, json_games)
    print(f"Real playoff results locked in: {len(locked)}")

    strength = season.strength[season.out_teams]
    n_out = len(season.out_teams)
    reach = np.zeros((6, n_out), dtype=np.int64)

    by_class = defaultdict(dict)
    for (cls, dist) in season.districts:
        by_class[cls][dist] = (cls, dist)

    done = 0
    while done < args.sims:
        n = min(spo.CHUNK, args.sims - done)
        champs = {}
        for key, (order, _) in seeds.items():
            slots = np.full((n, 8), -1, dtype=np.int64)
            for j, seed in enumerate(spo.DISTRICT_BRACKET_ORDER):
                if seed <= len(order):
                    slots[:, j] = order[seed - 1]
            alive = slots
            for stage in range(3):
                alive = spo.play_round(rng, alive, strength, args.sigma, locked)
                np.add.at(reach[stage], alive[alive >= 0], 1)
            champs[key] = alive[:, 0]
        for cls, dists in by_class.items():
            order_d = [d for d in spo.STATE_BRACKET_ORDER.get(cls, sorted(dists)) if d in dists]
            alive = np.stack([champs[dists[d]] for d in order_d], axis=1)
            for stage in (3, 4, 5):
                alive = spo.play_round(rng, alive, strength, args.sigma, locked)
                np.add.at(reach[stage], alive.ravel(), 1)
        done += n
    prob = reach / args.sims

    # ----------------------------------------------------------- team details
    rated = {t["school"]: t for t in ratings_doc["teams"]}
    class_rank = {}
    for cls in range(1, 7):
        ts = sorted([t for t in ratings_doc["teams"] if t["classification"] == cls],
                    key=lambda t: -t["ovr_rating"])
        for i, t in enumerate(ts, 1):
            class_rank[t["school"]] = (i, len(ts))
    rec = defaultdict(lambda: [0, 0])
    for g in season.played:
        if sds._parse_date(g["date"]) > sds.REGULAR_SEASON_END:
            continue
        w1 = g["score1"] > g["score2"]
        rec[g["team1"]][0 if w1 else 1] += 1
        rec[g["team2"]][1 if w1 else 0] += 1

    try:
        resolver = SlugResolver(load_data_file("output/schools.json")["schools"],
                                load_data_file("school_aliases.json"))
    except Exception as e:                          # page still works without logos
        print(f"NOTE: slug lookup unavailable ({e}); writing no slugs/logos.")
        resolver = lambda name: None

    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "season": 2026,
        "ratings_last_updated": ratings_doc.get("last_updated"),
        "simulations": args.sims,
        "sigma": args.sigma,
        "stages": STAGES,
        "district_bracket_order": spo.DISTRICT_BRACKET_ORDER,
        "results": shown,
        "classes": {},
    }
    for cls in sorted(by_class):
        order_d = [d for d in spo.STATE_BRACKET_ORDER.get(cls, sorted(by_class[cls]))
                   if d in by_class[cls]]
        districts, teams = {}, []
        for dist in sorted(by_class[cls]):
            order, status = seeds[(cls, dist)]
            names = []
            for s, p in enumerate(order, 1):
                name = season.names[season.out_teams[p]]
                names.append(name)
                r = rated.get(name, {})
                slug = resolver(name)
                cr = class_rank.get(name)
                pr = [float(x) for x in prob[:, p]]
                if s <= 8 - len(order):                       # top seeds in a short district
                    pr[0] = 1.0
                teams.append({
                    "team": name, "district": dist, "seed": s,
                    "bye": s <= 8 - len(order),
                    "record": f"{rec[name][0]}-{rec[name][1]}",
                    "ovr": round(float(strength[p]), 2),
                    "off": r.get("off_rating"), "def": r.get("def_rating"),
                    "ovr_rank": r.get("ovr_rank"),
                    "class_rank": cr[0] if cr else None,
                    "slug": slug, "logo": LOGO_URL.format(slug=slug) if slug else None,
                    "reach": [round(x, 4) for x in pr],
                    "reach_display": [fmt(x) for x in pr],
                })
            districts[str(dist)] = {"seeds": names, "seed_status": status}
        teams.sort(key=lambda t: (-t["reach"][5], -t["reach"][4], -t["reach"][3], -t["reach"][2]))
        out["classes"][str(cls)] = {"class": cls, "state_bracket_order": order_d,
                                    "districts": districts, "teams": teams}

    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {OUTPUT_PATH}")
    for cls, c in out["classes"].items():
        ts = c["teams"]
        st = defaultdict(int)
        for d in c["districts"].values():
            st[d["seed_status"]] += 1
        print(f"  Class {cls}: title sum {sum(t['reach'][5] for t in ts):.3f}  "
              f"favorite {ts[0]['team']} {ts[0]['reach_display'][5]}  seeds {dict(st)}")


if __name__ == "__main__":
    main()
