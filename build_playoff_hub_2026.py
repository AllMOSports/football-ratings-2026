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

DROUGHTS (v2)
  From output/state_tournament_history.json (All_MO_Sports-Data, MSHSAA's
  state series back to 1968): each team's Final Four appearances and state
  titles, the last year of each, and the drought length. A co-op uses its
  base school's history ("Tipton with Bunceton" -> Tipton); a team with no
  football entry has never reached a Final Four. HISTORY_ALIASES covers
  renamed schools.

FIRST-ROUND TRAVEL (v2)
  Every district first-round game, with the higher seed hosting. Drive time
  comes from the drive_times/class_N.json cache (OSRM, same as the bracket
  optimizer). Road miles come from OSRM's route service and are cached in
  drive_distances_2026.json, so each pair is only requested once; if OSRM
  can't be reached, miles fall back to straight-line distance x 1.2 and the
  game is flagged "miles_estimated". Also writes playoff_r1_travel_2026.csv
  and playoff_droughts_2026.csv for graphics.

Usage:
    python3 build_playoff_hub_2026.py
    python3 build_playoff_hub_2026.py --sims 20000 --sigma 16 --seed 7
    DATA_DIR=../All_MO_Sports-Data python3 build_playoff_hub_2026.py
Needs simulate_district_seeds.py, simulate_playoff_odds.py, build_district_points.py
and build_matchup_previews_2026.py (slug lookup) in the same folder.
"""
import argparse
import datetime
import csv
import json
import math
import os
import time
import urllib.request
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
DISTANCE_CACHE_PATH = "drive_distances_2026.json"
TRAVEL_CSV_PATH = "playoff_r1_travel_2026.csv"
DROUGHT_CSV_PATH = "playoff_droughts_2026.csv"
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
ROAD_FACTOR = 1.2                      # straight line -> road miles, fallback only
METERS_PER_MILE = 1609.344
SEASON = 2026
HISTORY_ALIASES = {"De Smet Jesuit": "DeSmet", "Father Tolton": "Father Tolton Regional Catholic"}
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


# --------------------------------------------------------------------------
# Droughts

def team_history(name, sth):
    """Final Four / state title history for a 2026 team name."""
    cands = [HISTORY_ALIASES.get(name, name), name.split(" with ")[0]]
    rec = None
    for c in cands:
        if "football" in sth.get(c, {}):
            rec = sth[c]["football"]
            break
    def years(key):
        if not rec:
            return []
        return sorted({int(str(y["year"])[:4]) for y in rec.get(key, {}).get("years", [])})
    ff, titles = years("final_four_appearances"), years("state_championships")
    return {
        "final_fours": len(ff), "last_final_four": ff[-1] if ff else None,
        "final_four_drought": (SEASON - ff[-1]) if ff else None,
        "titles": len(titles), "last_title": titles[-1] if titles else None,
        "title_drought": (SEASON - titles[-1]) if titles else None,
    }


# --------------------------------------------------------------------------
# First-round travel

def haversine_miles(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def osrm_route_meters(a, b):
    url = f"{OSRM_URL}/route/v1/driving/{a[1]:.5f},{a[0]:.5f};{b[1]:.5f},{b[0]:.5f}?overview=false"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AllMOSports-PlayoffHub/2.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                js = json.loads(r.read().decode("utf-8"))
            if js.get("code") == "Ok":
                return js["routes"][0]["distance"]
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


class Travel:
    """Drive time from the optimizer's cache, road miles from OSRM (cached)."""

    def __init__(self):
        self.sec, self.coords = {}, {}
        for cls in range(1, 7):
            path = f"drive_times/class_{cls}.json"
            if not os.path.exists(path):
                continue
            doc = json.load(open(path))
            self.sec.update(doc["seconds"])
            for name, lat, lon in doc["key"]:
                self.coords[name] = (lat, lon)
        self.cache = json.load(open(DISTANCE_CACHE_PATH)) if os.path.exists(DISTANCE_CACHE_PATH) else {}
        self.osrm_ok = True
        self.fetched = 0

    def game(self, home, away):
        out = {"minutes": None, "miles": None, "miles_estimated": False}
        s = self.sec.get(f"{away}|{home}") or self.sec.get(f"{home}|{away}")
        if s is not None:
            out["minutes"] = round(s / 60)
        a, b = self.coords.get(away), self.coords.get(home)
        if not a or not b:
            return out
        key = f"{away}|{home}|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}"
        m = self.cache.get(key)
        if m is None and self.osrm_ok:
            m = osrm_route_meters(a, b)
            if m is None:
                self.osrm_ok = False           # don't wait on every game if OSRM is down
                print("NOTE: OSRM unreachable; using estimated miles for uncached games.")
            else:
                self.cache[key] = m
                self.fetched += 1
                time.sleep(1)
        if m is not None:
            out["miles"] = round(m / METERS_PER_MILE)
        else:
            out["miles"] = round(haversine_miles(a, b) * ROAD_FACTOR)
            out["miles_estimated"] = True
        return out

    def save(self):
        if self.fetched:
            with open(DISTANCE_CACHE_PATH, "w") as f:
                json.dump(self.cache, f, indent=0, sort_keys=True)
            print(f"Fetched {self.fetched} new road distances from OSRM")


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

    try:
        sth = load_data_file("output/state_tournament_history.json")["teams"]
    except Exception as e:
        print(f"NOTE: state tournament history unavailable ({e}); no drought data.")
        sth = None
    travel = Travel()

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
                    "history": team_history(name, sth) if sth is not None else None,
                    "reach": [round(x, 4) for x in pr],
                    "reach_display": [fmt(x) for x in pr],
                })
            districts[str(dist)] = {"seeds": names, "seed_status": status}
        teams.sort(key=lambda t: (-t["reach"][5], -t["reach"][4], -t["reach"][3], -t["reach"][2]))
        # first-round travel: higher seed hosts
        by_team = {t["team"]: t for t in teams}
        r1 = []
        for dist in sorted(by_class[cls]):
            names = districts[str(dist)]["seeds"]
            for hi, lo in ((1, 8), (4, 5), (2, 7), (3, 6)):
                if lo > len(names):
                    continue
                home, away = names[hi - 1], names[lo - 1]
                g = {"district": dist, "home": home, "away": away, "home_seed": hi, "away_seed": lo,
                     "home_win": by_team[home]["reach"][0]}
                g.update(travel.game(home, away))
                r1.append(g)
        out["classes"][str(cls)] = {"class": cls, "state_bracket_order": order_d,
                                    "districts": districts, "teams": teams, "r1_travel": r1}

    travel.save()
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {OUTPUT_PATH}")

    with open(TRAVEL_CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["class", "district", "home_seed", "home", "away_seed", "away", "miles",
                    "miles_estimated", "drive_minutes", "home_win_prob"])
        for cls, c in out["classes"].items():
            for g in c["r1_travel"]:
                w.writerow([cls, g["district"], g["home_seed"], g["home"], g["away_seed"], g["away"],
                            g["miles"], g["miles_estimated"], g["minutes"], g["home_win"]])
    with open(DROUGHT_CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["class", "district", "seed", "team", "record", "final_fours", "last_final_four",
                    "final_four_drought", "final_four_odds", "titles", "last_title", "title_drought",
                    "title_odds"])
        for cls, c in out["classes"].items():
            for t in c["teams"]:
                h = t["history"] or {}
                w.writerow([cls, t["district"], t["seed"], t["team"], t["record"], h.get("final_fours"),
                            h.get("last_final_four"), h.get("final_four_drought"), t["reach"][3],
                            h.get("titles"), h.get("last_title"), h.get("title_drought"), t["reach"][5]])
    print(f"Wrote {TRAVEL_CSV_PATH} and {DROUGHT_CSV_PATH}")
    for cls, c in out["classes"].items():
        ts = c["teams"]
        st = defaultdict(int)
        for d in c["districts"].values():
            st[d["seed_status"]] += 1
        print(f"  Class {cls}: title sum {sum(t['reach'][5] for t in ts):.3f}  "
              f"favorite {ts[0]['team']} {ts[0]['reach_display'][5]}  seeds {dict(st)}")


if __name__ == "__main__":
    main()
