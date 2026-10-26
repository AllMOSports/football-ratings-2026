"""
Matchup Previews - nightly data builder (v3)
============================================
v2: projected score + win probability now use the Matchup Predictor v8 calculation.
v3: PLAYOFF GAMES come from playoff_hub_2026.json (build_playoff_hub_2026.py):
    the seeded district brackets for all six classes. Every bracket game whose
    two teams are known and that has no real result yet is added - round 1
    from the seeds, later rounds as winners appear in football_games_2026_all.json
    (byes advance automatically). The higher seed is listed first and hosts
    district games. Dates come from PLAYOFF_ROUNDS below unless the games file
    already has that game scheduled, in which case its date wins.
 
 
Writes matchup_previews_2026.json: everything the Matchup Preview snippet needs
for every UNPLAYED game in football_games_2026_all.json, so the page does one
fetch and only renders.
 
  teams{}   one entry per team appearing in an upcoming game (shared across
            games so the file stays small): ratings + ranks, class rank, PPG /
            PAPG / margin, SOS, district points + current seed, Size-Adjusted
            rank, last-5 form vs projection, program history since 2010, slug
            and logo URL.
  games[]   one entry per upcoming game: projected score (raw + snapped), win
            probability, district-game flag, auto storyline tags, common
            opponents, head-to-head since 2010, and STAKES - each team's seed /
            district title / Final Four / state title odds IF IT WINS vs IF IT
            LOSES this game.
 
STAKES come from the same Monte Carlo as simulate_district_seeds.py and
simulate_playoff_odds.py (same game model, sigma, district points formula,
head-to-head switch and brackets) - this script imports both and just keeps
every simulation's per-game winners so results can be split by game outcome.
One run covers every remaining game at once. Games after REGULAR_SEASON_END
(playoffs) get stakes = null; the snippet hides that section.
 
PROJECTED SCORE + WIN PROBABILITY use the exact calculation from the
Hypothetical Matchup Predictor (Football, v8) - see predictor_projection():
  raw score     = own off_rating - opponent def_rating + LEAGUE_AVERAGE (26, fixed)
  negative raw  = rolled over onto the opponent's score, then both capped at 84
  snapped score = nearest of 0, 3, 7, 10, 14, 17 ... (7k and 7k+3), no ties
                  (the side behind on the raw margin steps down one value)
  win prob      = 1 / (1 + e^(-1.702 * raw margin / 14))
Keep the constants below in sync with that snippet if either one changes.
STAKES deliberately keep the seed/playoff simulations' own game model
(Normal margin, --sigma) so they match the Seed Projections and Playoff Odds pages.
 
Inputs (this repo): football_ratings_2026.json, football_games_2026_all.json,
  district_seed_projections_2026.json, schedule_metrics_2026.json,
  per_capita_football_2026.json, plus everything simulate_district_seeds needs.
Inputs (All_MO_Sports-Data): output/schools.json, school_aliases.json,
  football_history.json, output/football_schedule_{2010..2025}.json.
  Read from DATA_DIR if set (a local clone), otherwise fetched from GitHub raw.
 
Usage:
    python3 build_matchup_previews_2026.py
    python3 build_matchup_previews_2026.py --sims 20000 --sigma 16
    DATA_DIR=../All_MO_Sports-Data python3 build_matchup_previews_2026.py
"""
 
import argparse
import datetime
import json
import math
import os
import urllib.request
from collections import defaultdict
 
import numpy as np
 
import simulate_district_seeds as sds
import simulate_playoff_odds as spo
 
OUTPUT_PATH = "matchup_previews_2026.json"
RATINGS_PATH = "football_ratings_2026.json"
GAMES_PATH = "football_games_2026_all.json"
SEEDS_PATH = "district_seed_projections_2026.json"
METRICS_PATH = "schedule_metrics_2026.json"
PER_CAPITA_PATH = "per_capita_football_2026.json"
PLAYOFF_HUB_PATH = "playoff_hub_2026.json"

# Playoff rounds: (label, date, week). Dates are each round's usual Friday --
# edit here if MSHSAA's 2026 calendar differs. A date already in
# football_games_2026_all.json for that game overrides this.
PLAYOFF_ROUNDS = [
    ("District first round", "2026-10-30", 10),
    ("District semifinal",   "2026-11-06", 11),
    ("District final",       "2026-11-13", 12),
    ("State quarterfinal",   "2026-11-20", 13),
    ("State semifinal",      "2026-11-27", 14),
    ("State championship",   "2026-12-05", 15),
]
 
DATA_RAW = "https://raw.githubusercontent.com/AllMOSports/All_MO_Sports-Data/main/"
LOGO_URL = DATA_RAW + "logos_main2/{slug}.png"
HISTORY_YEARS = range(2010, 2026)
 
DEFAULT_SIMS = 20000
DEFAULT_SIGMA = 16.0          # matches the playoff-odds default
FORM_GAMES = 5
 
# ---- Matchup Predictor (Football, v8) constants: keep in sync with that snippet ----
LEAGUE_AVERAGE = 26          # fixed, not season-specific (predictor LEAGUE_AVERAGE)
MARGIN_PROB_SCALE = 14       # logistic win-probability scale (predictor MARGIN_PROB_SCALE)
MAX_SCORE = 84               # raw score cap, applied after the negative rollover
TYPICAL_SCORES = sorted([7 * k for k in range(15)] + [7 * k + 3 for k in range(15)])
 
 
# --------------------------------------------------------------------------
# Loading helpers
# --------------------------------------------------------------------------
 
def load_json(path):
    with open(path) as f:
        return json.load(f)
 
 
def load_data_file(rel):
    """A file from All_MO_Sports-Data: local clone if DATA_DIR is set, else GitHub raw."""
    base = os.environ.get("DATA_DIR")
    if base:
        return load_json(os.path.join(base, rel))
    with urllib.request.urlopen(DATA_RAW + rel, timeout=60) as r:
        return json.load(r)
 
 
def make_slug(name):
    """Same rule as Build_Schools_json.make_slug."""
    s = name.lower().strip().replace("'", "").replace(".", "").replace("(", "").replace(")", "")
    return "-".join(s.split())
 
 
class SlugResolver:
    """add_slugs.py's lookup (mshsaa_name, then name), then school_aliases.json,
    then make_slug, then the base school of a co-op ('Tipton with Bunceton')."""
 
    def __init__(self, schools, aliases):
        self.schools = schools
        self.by_mshsaa, self.by_name = defaultdict(list), defaultdict(list)
        for slug, s in schools.items():
            self.by_mshsaa[s.get("mshsaa_name", "")].append(slug)
            self.by_name[s.get("name", "")].append(slug)
        self.aliases = aliases
 
    def __call__(self, name):
        for table in (self.by_mshsaa, self.by_name):
            if len(table.get(name, [])) == 1:
                return table[name][0]
        if name in self.aliases:
            return self.aliases[name]
        for cand in (name, name.split(" with ")[0]):
            if make_slug(cand) in self.schools:
                return make_slug(cand)
        return None
 
 
def snap_to_typical_score(x):
    """predictor snapToTypicalScore(): first value with the smallest distance wins."""
    best, best_diff = TYPICAL_SCORES[0], abs(x - TYPICAL_SCORES[0])
    for s in TYPICAL_SCORES[1:]:
        d = abs(x - s)
        if d < best_diff:
            best, best_diff = s, d
    return best
 
 
def next_lower_typical_score(score):
    i = TYPICAL_SCORES.index(score)
    return TYPICAL_SCORES[i - 1] if i > 0 else TYPICAL_SCORES[0]
 
 
def win_prob_from_margin(margin):
    """predictor logistic curve over the raw (pre-snap, pre-rollover) margin."""
    return 1.0 / (1.0 + math.exp(-1.702 * margin / MARGIN_PROB_SCALE))
 
 
def predictor_projection(off_a, def_a, off_b, def_b):
    """Line-for-line port of the Matchup Predictor's predict().
    Returns (formula_a, formula_b, score_a, score_b, win_prob_a)."""
    raw_a = off_a - def_b + LEAGUE_AVERAGE
    raw_b = off_b - def_a + LEAGUE_AVERAGE
    formula_a, formula_b = raw_a, raw_b
    margin = raw_a - raw_b
    if raw_a < 0:                       # negative score rolls over onto the opponent
        raw_b += -raw_a
        raw_a = 0
    if raw_b < 0:
        raw_a += -raw_b
        raw_b = 0
    raw_a = min(raw_a, MAX_SCORE)
    raw_b = min(raw_b, MAX_SCORE)
    score_a = snap_to_typical_score(max(0, raw_a))
    score_b = snap_to_typical_score(max(0, raw_b))
    if score_a == score_b:              # no ties: the side behind on the raw margin steps down
        if margin >= 0:
            score_b = next_lower_typical_score(score_b)
        else:
            score_a = next_lower_typical_score(score_a)
    return formula_a, formula_b, score_a, score_b, win_prob_from_margin(margin)
 
 
def r1(x):
    return None if x is None else round(float(x), 1)
 
 
def r2(x):
    return None if x is None else round(float(x), 2)
 
 
# --------------------------------------------------------------------------
# Conditional odds (one Monte Carlo for every remaining game)
# --------------------------------------------------------------------------
 
def conditional_odds(season, sims, sigma, seed=None):
    """For every remaining regular-season game and each in-state team in it:
    seed distribution + district title / Final Four / state title odds,
    split by whether that team wins or loses the game.
 
    Returns {game_index_in_season.remaining: {team_name: {"win": {...}, "loss": {...}}}}
    """
    results = spo.load_playoff_results(season, sds.REGULAR_SEASON_END)
    strength = season.strength[season.out_teams]
    n_out = len(season.out_teams)
    pos_of = {season.names[t]: i for i, t in enumerate(season.out_teams)}
    max_k = max(len(v) for v in season.districts.values())
 
    R = len(season.remaining)
    rem_col = len(season.played) + np.arange(R)
    p1 = np.array([pos_of.get(g["team1"], -1) for g in season.remaining])
    p2 = np.array([pos_of.get(g["team2"], -1) for g in season.remaining])
    p1c, p2c = np.maximum(p1, 0), np.maximum(p2, 0)
 
    by_class = defaultdict(dict)
    for (cls, dist) in season.districts:
        by_class[cls][dist] = (cls, dist)
 
    metrics = ["dt", "ff", "st"] + [f"s{i}" for i in range(max_k)]
    # sums[side][metric][cond] -> array over games; side 0 = team1, 1 = team2
    sums = {side: {m: {c: np.zeros(R) for c in ("win", "loss")} for m in metrics} for side in (0, 1)}
    cnt = {side: {c: np.zeros(R) for c in ("win", "loss")} for side in (0, 1)}
 
    rng = np.random.default_rng(seed)
    done = 0
    while done < sims:
        n = min(sds.CHUNK, sims - done)
        margin, ot = season.simulate_margins(rng, n, sigma)
        pts = sds.break_ties(rng, season.points(margin, ot))
 
        seed_of = np.zeros((n, n_out), dtype=np.int64)
        dt = np.zeros((n, n_out), dtype=bool)
        ff = np.zeros((n, n_out), dtype=bool)
        st = np.zeros((n, n_out), dtype=bool)
        rows = np.arange(n)
 
        champs = {}
        for key, positions in season.districts.items():
            pos = np.array(positions)
            order = np.argsort(-pts[:, pos], axis=1)
            order = sds.apply_switches(order, sds.beat_matrix(margin, season.h2h[key], len(pos)))
            seeded = pos[order]
            for s_i in range(len(pos)):
                seed_of[rows, seeded[:, s_i]] = s_i
            slots = np.full((n, len(spo.DISTRICT_BRACKET_ORDER)), -1, dtype=np.int64)
            for j, s in enumerate(spo.DISTRICT_BRACKET_ORDER):
                if s <= len(pos):
                    slots[:, j] = seeded[:, s - 1]
            alive = slots
            while alive.shape[1] > 1:
                alive = spo.play_round(rng, alive, strength, sigma, results)
            champs[key] = alive[:, 0]
            dt[rows, alive[:, 0]] = True
 
        for cls, dists in by_class.items():
            order_d = [d for d in spo.STATE_BRACKET_ORDER.get(cls, sorted(dists)) if d in dists]
            alive = np.stack([champs[dists[d]] for d in order_d], axis=1)
            while alive.shape[1] > 1:
                if alive.shape[1] == 4:
                    for c in range(4):
                        ff[rows, alive[:, c]] = True
                alive = spo.play_round(rng, alive, strength, sigma, results)
            st[rows, alive[:, 0]] = True
 
        t1_won = margin[:, rem_col] > 0                      # [n, R]
        for side, pc in ((0, p1c), (1, p2c)):
            won = t1_won if side == 0 else ~t1_won
            masks = {"win": won.astype(np.float64), "loss": (~won).astype(np.float64)}
            vals = {"dt": dt[:, pc], "ff": ff[:, pc], "st": st[:, pc]}
            sd = seed_of[:, pc]
            for i in range(max_k):
                vals[f"s{i}"] = sd == i
            for c, mk in masks.items():
                cnt[side][c] += mk.sum(axis=0)
                for m, v in vals.items():
                    sums[side][m][c] += (v * mk).sum(axis=0)
        done += n
 
    out = {}
    for r, g in enumerate(season.remaining):
        entry = {}
        for side, p, name in ((0, p1[r], g["team1"]), (1, p2[r], g["team2"])):
            if p < 0:
                continue
            k = len(season.districts[(season.team_class[name], sds_district(season, p))])
            team = {}
            for c in ("win", "loss"):
                nn = cnt[side][c][r]
                if nn == 0:
                    team[c] = None
                    continue
                team[c] = {
                    "n": int(nn),
                    "seed_probs": [round(sums[side][f"s{i}"][c][r] / nn, 4) for i in range(k)],
                    "district_title": round(sums[side]["dt"][c][r] / nn, 4),
                    "final_four": round(sums[side]["ff"][c][r] / nn, 4),
                    "state_title": round(sums[side]["st"][c][r] / nn, 4),
                }
            entry[name] = team
        out[r] = entry
    return out
 
 
_POS_DISTRICT = {}
 
 
def sds_district(season, pos):
    """District number for an output position (cached)."""
    if not _POS_DISTRICT:
        for (cls, dist), positions in season.districts.items():
            for p in positions:
                _POS_DISTRICT[p] = dist
    return _POS_DISTRICT[pos]
 
 
# --------------------------------------------------------------------------
# Per-team and per-game blocks
# --------------------------------------------------------------------------
 
def game_key(g):
    return (g["date"], g["team1"], g["team2"])
 
 
def team_results(games):
    """name -> list of played results (chronological), from the 2026 games file."""
    out = defaultdict(list)
    for g in games:
        if g["score1"] is None or g["score2"] is None:
            continue
        for me, opp, ms, os_ in ((g["team1"], g["team2"], g["score1"], g["score2"]),
                                 (g["team2"], g["team1"], g["score2"], g["score1"])):
            out[me].append({"date": sds.bdp._normalize_date(g["date"]), "week": g.get("week"),
                            "opp": opp, "pf": ms, "pa": os_, "forfeit": g.get("forfeit", False),
                            "ot": g.get("overtime", False)})
    for v in out.values():
        v.sort(key=lambda x: x["date"])
    return out
 
 
def load_h2h_history(resolver_names):
    """{frozenset(a, b): [meetings]} from output/football_schedule_{year}.json."""
    meetings = defaultdict(list)
    for y in HISTORY_YEARS:
        try:
            doc = load_data_file(f"output/football_schedule_{y}.json")
        except Exception as e:                       # a missing year shouldn't kill the build
            print(f"  WARNING: football_schedule_{y}.json unavailable ({e})")
            continue
        teams = doc.get("teams", doc)
        seen = set()
        for me, games in teams.items():
            if me not in resolver_names:
                continue
            for g in games if isinstance(games, list) else games.get("games", []):
                opp = g.get("opponent")
                if opp not in resolver_names or g.get("team_score") is None:
                    continue
                k = (frozenset((me, opp)), g.get("date"))
                if k in seen:
                    continue
                seen.add(k)
                meetings[frozenset((me, opp))].append({
                    "season": y, "date": g.get("date"), "team": me, "opp": opp,
                    "team_score": g["team_score"], "opp_score": g["opp_score"],
                })
    for k, v in meetings.items():
        v.sort(key=lambda x: x["date"] or "")
        by_year = defaultdict(list)
        for m in v:
            by_year[m["season"]].append(m)
        for ms in by_year.values():                  # label postseason meetings
            for i, m in enumerate(ms):
                month = int((m["date"] or "0000-00")[5:7] or 0)
                m["postseason"] = i > 0 or month >= 11
    return meetings
 
 
def h2h_block(a, b, history, results_2026):
    rows = []
    for m in history.get(frozenset((a, b)), []):
        a_score = m["team_score"] if m["team"] == a else m["opp_score"]
        b_score = m["opp_score"] if m["team"] == a else m["team_score"]
        rows.append({"season": m["season"], "date": m["date"], "a": a_score, "b": b_score,
                     "postseason": m["postseason"]})
    for r in results_2026.get(a, []):
        if r["opp"] == b:
            rows.append({"season": 2026, "date": r["date"], "a": r["pf"], "b": r["pa"], "postseason": False})
    rows.sort(key=lambda x: x["date"] or "")
    if not rows:
        return None
    a_w = sum(1 for x in rows if x["a"] > x["b"])
    b_w = sum(1 for x in rows if x["b"] > x["a"])
    streak_team, streak = None, 0
    for x in reversed(rows):
        w = a if x["a"] > x["b"] else (b if x["b"] > x["a"] else None)
        if w is None:
            break
        if streak_team is None:
            streak_team = w
        if w != streak_team:
            break
        streak += 1
    closest = min(rows, key=lambda x: (abs(x["a"] - x["b"]), x["date"] or ""))
    return {
        "since": rows[0]["season"],
        "meetings": len(rows),
        "wins": [a_w, b_w],
        "avg_margin_a": round(sum(x["a"] - x["b"] for x in rows) / len(rows), 1),
        "streak": {"team": streak_team, "games": streak} if streak_team else None,
        "closest": closest,
        "games": list(reversed(rows)),             # newest first
    }
 
 
def program_block(slug, history_doc, current_rank):
    h = history_doc.get("teams", {}).get(slug)
    if not h:
        return None
    seasons = []
    for y, v in sorted(h.get("history", {}).items()):
        seasons.append({"season": int(y), "w": v.get("wins"), "l": v.get("losses"), "rank": v.get("ovr_rank")})
    ranked = [s for s in seasons if s["rank"]]
    best = min(ranked, key=lambda s: s["rank"]) if ranked else None
    prev_best = best["rank"] if best else None
    return {
        "seasons_on_file": len(seasons),
        "first_season": seasons[0]["season"] if seasons else None,
        "winning_seasons": sum(1 for s in seasons if (s["w"] or 0) > (s["l"] or 0)),
        "best_rank": best["rank"] if best else None,
        "best_rank_season": best["season"] if best else None,
        "best_since_2010": bool(prev_best is None or (current_rank is not None and current_rank < prev_best)),
    }
 
 
def team_block(name, ctx):
    r = ctx["ratings"].get(name)
    res = [x for x in ctx["results"].get(name, []) if not x["forfeit"]]
    allres = ctx["results"].get(name, [])
    slug = ctx["slug"](name)
    w = sum(1 for x in allres if x["pf"] > x["pa"])
    l = sum(1 for x in allres if x["pf"] < x["pa"])
    oos = ctx["oos_records"].get(name)
    if r is None and oos:                 # out-of-state: full record from the targeted scrape
        w, l = oos.get("wins", w), oos.get("losses", l)
    t = {
        "name": name,
        "slug": slug,
        "logo": LOGO_URL.format(slug=slug) if slug else None,
        "rated": r is not None,
        "out_of_state": name not in ctx["team_class"],
        "record": f"{w}-{l}",
        "wins": w, "losses": l,
        "ppg": r1(sum(x["pf"] for x in res) / len(res)) if res else None,
        "papg": r1(sum(x["pa"] for x in res) / len(res)) if res else None,
        "margin": r1(sum(x["pf"] - x["pa"] for x in res) / len(res)) if res else None,
    }
    if r:
        cls = r["classification"]
        t.update({
            "class": cls, "district": r["district"],
            "ovr": r2(r["ovr_rating"]), "ovr_rank": r["ovr_rank"],
            "off": r2(r["off_rating"]), "off_rank": r["off_rank"],
            "def": r2(r["def_rating"]), "def_rank": r["def_rank"],
            "class_rank": ctx["class_rank"].get(name), "class_size": ctx["class_size"].get(cls),
        })
    m = ctx["metrics"].get(name)
    if m:
        t["sos"], t["sos_rank"] = r2(m.get("sos")), m.get("sos_rank")
    sp = ctx["seeds"].get(name)
    if sp:
        t["district_points"], t["current_seed"] = r2(sp.get("current_points")), sp.get("current_seed")
        t["district_size"] = len(sp.get("seed_probs", []))
    pc = ctx["per_capita"].get(name)
    if pc:
        t["size_adj_rank"], t["enrollment"] = pc.get("per_capita_rank"), pc.get("enrollment")
 
    # last-N form vs projection from current ratings (strength incl. estimated unrated)
    form = []
    s_me = ctx["strength"].get(name)
    for x in allres[-FORM_GAMES:][::-1]:
        s_opp = ctx["strength"].get(x["opp"])
        delta = None
        if s_me is not None and s_opp is not None and not x["forfeit"]:
            delta = r1((x["pf"] - x["pa"]) - (s_me - s_opp))
        orr = ctx["ratings"].get(x["opp"])
        form.append({"week": x["week"], "date": x["date"], "opp": x["opp"],
                     "opp_rank": orr["ovr_rank"] if orr else None,
                     "pf": x["pf"], "pa": x["pa"], "ot": x["ot"], "forfeit": x["forfeit"],
                     "delta": delta})
    t["form"] = form
    t["form_beat"] = sum(1 for f in form if f["delta"] is not None and f["delta"] > 0)
    t["program"] = program_block(slug, ctx["history_doc"], t.get("ovr_rank")) if slug else None
    return t
 
 
def common_block(a, b, results):
    ra = defaultdict(list)
    for x in results.get(a, []):
        ra[x["opp"]].append(x)
    rows = []
    for y in results.get(b, []):
        if y["opp"] in ra and y["opp"] not in (a, b):
            for x in ra[y["opp"]]:
                ma, mb = x["pf"] - x["pa"], y["pf"] - y["pa"]
                rows.append({"opp": y["opp"], "a": [x["pf"], x["pa"]], "b": [y["pf"], y["pa"]],
                             "edge": "a" if ma > mb else ("b" if mb > ma else None)})
    # one row per opponent (first meeting each), newest opponents last
    seen, out = set(), []
    for r in rows:
        if r["opp"] not in seen:
            seen.add(r["opp"])
            out.append(r)
    return out
 
 
def headline_seed(cond):
    """Seed whose probability swings most between a win and a loss."""
    if not cond or not cond.get("win") or not cond.get("loss"):
        return None
    w, l = cond["win"]["seed_probs"], cond["loss"]["seed_probs"]
    i = max(range(len(w)), key=lambda i: (abs(w[i] - l[i]), -i))
    return i + 1
 
 
def playoff_games(hub, games):
    """Bracket games with both teams known and no real result yet.

    Returns dicts shaped like games-file rows plus playoff fields:
    round, round_index, seeds [seed1, seed2] (district rounds), home, class, district.
    """
    regular_end = sds.REGULAR_SEASON_END
    real = {}            # frozenset(pair) -> games-file row, playoff period only
    for g in games:
        if sds._parse_date(str(g["date"])) <= regular_end:
            continue
        real[frozenset((g["team1"], g["team2"]))] = g

    def winner(a, b):
        g = real.get(frozenset((a, b)))
        if not g or g["score1"] is None or g["score2"] is None or g["score1"] == g["score2"]:
            return None
        return g["team1"] if g["score1"] > g["score2"] else g["team2"]

    order = hub.get("district_bracket_order", [1, 8, 4, 5, 2, 7, 3, 6])
    out = []

    def emit(round_i, cls, dist, a, b, seed_of):
        """a, b: team names (a = better seed when seeds exist)."""
        label, date, week = PLAYOFF_ROUNDS[round_i]
        sa, sb = seed_of.get(a), seed_of.get(b)
        if sa is not None and sb is not None and sb < sa:
            a, b, sa, sb = b, a, sb, sa
        sched = real.get(frozenset((a, b)))
        if sched:
            date = sds.bdp._normalize_date(str(sched["date"]))
        out.append({"date": date, "week": week, "team1": a, "team2": b, "score1": None, "score2": None,
                    "round": label, "round_index": round_i, "class": cls,
                    "district": dist if round_i <= 2 else None,
                    "seeds": [sa, sb] if (round_i <= 2 and sa is not None and sb is not None) else None,
                    "home": a if round_i <= 2 else None})

    for cls_key, c in hub.get("classes", {}).items():
        cls = c.get("class", int(cls_key))
        champs = {}
        for dist_key, d in c.get("districts", {}).items():
            dist = int(dist_key)
            seeds = d.get("seeds", [])
            seed_of = {t: i + 1 for i, t in enumerate(seeds)}
            # slots in bracket order; None = bye (seed not in this district)
            alive = [seeds[s - 1] if s <= len(seeds) else None for s in order]
            round_i = 0
            while len(alive) > 1:
                nxt = []
                for j in range(0, len(alive), 2):
                    a, b = alive[j], alive[j + 1]
                    if a is None or b is None:            # bye (or the other side is still undecided)
                        nxt.append(a if b is None else b if a is None else None)
                        continue
                    if a == "?" or b == "?":
                        nxt.append("?")
                        continue
                    w = winner(a, b)
                    if w is None:
                        emit(round_i, cls, dist, a, b, seed_of)
                        nxt.append("?")
                    else:
                        nxt.append(w)
                alive = [x for x in nxt]
                round_i += 1
            champs[dist] = alive[0] if alive and alive[0] not in (None, "?") else None
        # state bracket: district champions in state_bracket_order, adjacent pairs
        alive = [champs.get(dn) or "?" for dn in c.get("state_bracket_order", sorted(champs))]
        round_i = 3
        while len(alive) > 1 and round_i < len(PLAYOFF_ROUNDS):
            nxt = []
            for j in range(0, len(alive), 2):
                a, b = alive[j], alive[j + 1]
                if a == "?" or b == "?":
                    nxt.append("?")
                    continue
                w = winner(a, b)
                if w is None:
                    emit(round_i, cls, None, a, b, {})
                    nxt.append("?")
                else:
                    nxt.append(w)
            alive = nxt
            round_i += 1
    return out


def build_tags(g, A, B, proj, h2h, stakes):
    """Auto storylines, best first. Each: {kind, label, text}."""
    tags = []
    a, b = A["name"], B["name"]
    fav, dog = (A, B) if proj["win_prob"][0] >= 0.5 else (B, A)
    dog_p = min(proj["win_prob"])
 
    if stakes:
        sa, sb = stakes.get(a), stakes.get(b)
        if sa and sb and sa.get("win") and sb.get("win"):
            ia = max(range(len(sa["win"]["seed_probs"])), key=lambda i: sa["win"]["seed_probs"][i])
            ib = max(range(len(sb["win"]["seed_probs"])), key=lambda i: sb["win"]["seed_probs"][i])
            if ia == ib and sa["win"]["seed_probs"][ia] >= 0.9 and sb["win"]["seed_probs"][ib] >= 0.9:
                tags.append({"kind": "stakes", "label": "Seed on the line",
                             "text": f"The winner takes the district's #{ia + 1} seed."})
 
    if h2h and h2h["streak"] and h2h["streak"]["games"] >= 4:
        st = h2h["streak"]
        other = b if st["team"] == a else a
        wins = h2h["wins"][0] if st["team"] == a else h2h["wins"][1]
        if st["games"] == h2h["meetings"]:
            text = (f"{st['team']} is {wins}-0 against {other} since {h2h['since']}, winning by an average "
                    f"of {abs(h2h['avg_margin_a']):.0f} points.")
        else:
            text = f"{st['team']} has won {st['games']} straight meetings with {other}."
        streaker = A if st["team"] == a else B
        breaker = B if st["team"] == a else A
        prog = breaker.get("program") or {}
        if prog.get("best_since_2010") and breaker.get("ovr_rank"):
            text += (f" But this is {breaker['name']}'s highest-rated team since {prog['first_season']}"
                     f" (#{breaker['ovr_rank']} statewide).")
        tags.append({"kind": "streak", "label": "The streak", "text": text})
 
    for t in (A, B):
        if t["losses"] == 0 and t["wins"] >= 3:
            tags.append({"kind": "unbeaten", "label": "Unbeaten",
                         "text": f"{t['name']} puts a {t['record']} record on the line."})
 
    if A.get("ovr_rank") and B.get("ovr_rank") and A["ovr_rank"] <= 10 and B["ovr_rank"] <= 10:
        tags.append({"kind": "clash", "label": "Top-10 clash",
                     "text": f"#{A['ovr_rank']} vs #{B['ovr_rank']} in the statewide ratings."})
 
    if 0.2 <= dog_p <= 0.45 and dog.get("form_beat", 0) >= 4:
        tags.append({"kind": "upset", "label": "Upset watch",
                     "text": f"{dog['name']} has beaten its projection in {dog['form_beat']} of its last "
                             f"{len(dog['form'])} games."})
 
    if dog_p >= 0.45:
        tags.append({"kind": "tossup", "label": "Toss-up",
                     "text": f"The ratings have this within {abs(proj['raw'][0] - proj['raw'][1]):.0f} points."})
 
    for t in (A, B):
        prog = t.get("program") or {}
        if prog.get("best_since_2010") and t.get("ovr_rank") and not any(x["kind"] == "streak" for x in tags):
            tags.append({"kind": "program", "label": "Program best",
                         "text": f"{t['name']} is #{t['ovr_rank']} statewide, its best rating since "
                                 f"{prog['first_season']}."})
            break
    return tags
 
 
# --------------------------------------------------------------------------
 
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
 
    ratings_doc = load_json(RATINGS_PATH)
    L = LEAGUE_AVERAGE        # the predictor's fixed constant, not ratings_doc["league_average"]
    ratings = {t["school"]: t for t in ratings_doc["teams"]}
    games = load_json(GAMES_PATH)
    sds.bdp.apply_forfeit_overrides(games)        # MSHSAA forfeits from manual_overrides.json
 
    by_class = defaultdict(list)
    for t in ratings_doc["teams"]:
        by_class[t["classification"]].append(t)
    class_rank, class_size = {}, {}
    for cls, ts in by_class.items():
        ts.sort(key=lambda t: -t["ovr_rating"])
        class_size[cls] = len(ts)
        for i, t in enumerate(ts, 1):
            class_rank[t["school"]] = i
 
    seeds = {}
    for d in load_json(SEEDS_PATH)["districts"].values():
        for t in d["teams"]:
            seeds[t["team"]] = t
    metrics = load_json(METRICS_PATH)["teams"]
    per_capita = {t["school"]: t for t in load_json(PER_CAPITA_PATH)["teams"]}
 
    print("Loading All_MO_Sports-Data files...")
    resolver = SlugResolver(load_data_file("output/schools.json")["schools"],
                            load_data_file("school_aliases.json"))
    history_doc = load_data_file("football_history.json")
 
    # Simulation season (same inputs/loaders as the seed + playoff scripts)
    tc, td, oc, orc, sim_games, ovr, _ = sds.load_inputs()
    season = sds.Season(tc, td, oc, orc, sim_games, ovr)
    strength = {season.names[i]: float(s) for i, s in enumerate(season.strength)}
 
    upcoming = [g for g in games if g["score1"] is None or g["score2"] is None]
    if os.path.exists(PLAYOFF_HUB_PATH):
        bracket = playoff_games(load_json(PLAYOFF_HUB_PATH), games)
        pairs = {frozenset((g["team1"], g["team2"])) for g in bracket}
        # the bracket version carries round / seed / host info, so it replaces any
        # unscored copy of the same playoff game in the games file
        # after the regular season, only bracket games are previewed: that drops unscored
        # copies of bracket games (the bracket version carries round / seed / host info) and
        # non-playoff games such as the Kirkwood-Webster Groves Thanksgiving game
        upcoming = [g for g in upcoming
                    if sds._parse_date(str(g["date"])) <= sds.REGULAR_SEASON_END] + bracket
        print(f"Playoff games from {PLAYOFF_HUB_PATH}: {len(bracket)}")
    print(f"Upcoming games: {len(upcoming)}   (simulating {len(season.remaining)} regular-season games, "
          f"{args.sims} sims, sigma {args.sigma})")
    # stakes only exist for remaining regular-season games; skip the sims once those are done
    cond = conditional_odds(season, args.sims, args.sigma, args.seed) if season.remaining else {}
    rem_index = {}
    for i, g in enumerate(season.remaining):
        rem_index[(sds.bdp._normalize_date(g["date"]), g["team1"], g["team2"])] = i
 
    results = team_results(games)
    names_in_play = {g["team1"] for g in upcoming} | {g["team2"] for g in upcoming}
    print("Loading head-to-head history 2010-2025...")
    h2h_hist = load_h2h_history(set(ratings) | names_in_play)
 
    ctx = {"ratings": ratings, "results": results, "slug": resolver, "class_rank": class_rank,
           "class_size": class_size, "metrics": metrics, "seeds": seeds, "per_capita": per_capita,
           "strength": strength, "history_doc": history_doc,
           "oos_records": orc, "team_class": tc}
 
    teams_out, games_out = {}, []
    for g in upcoming:
        a, b = g["team1"], g["team2"]
        for n in (a, b):
            if n not in teams_out:
                teams_out[n] = team_block(n, ctx)
        A, B = teams_out[a], teams_out[b]
        date = sds.bdp._normalize_date(g["date"])
 
        proj = None
        if A["rated"] and B["rated"]:
            ra, rb = ratings[a], ratings[b]
            pa, pb, sa_, sb_, p = predictor_projection(ra["off_rating"], ra["def_rating"],
                                                       rb["off_rating"], rb["def_rating"])
            proj = {"raw": [r1(pa), r1(pb)], "score": [sa_, sb_], "win_prob": [round(p, 4), round(1 - p, 4)]}
        elif a in strength and b in strength:     # one side unrated: margin only, from estimated strength
            p = win_prob_from_margin(strength[a] - strength[b])   # same logistic curve, estimated strength
            proj = {"raw": None, "score": None, "win_prob": [round(p, 4), round(1 - p, 4)]}
 
        district_game = bool(A.get("class") and A.get("class") == B.get("class")
                             and A.get("district") == B.get("district"))
        playoff = g.get("round") is not None
 
        stakes = None
        ri = rem_index.get((date, a, b))
        if ri is not None and cond.get(ri):
            stakes = {}
            for n in (a, b):
                c = cond[ri].get(n)
                if c:
                    stakes[n] = {**c, "headline_seed": headline_seed(c)}
            stakes = stakes or None
 
        h2h = h2h_block(a, b, h2h_hist, results)
        slug_a, slug_b = A["slug"] or make_slug(a), B["slug"] or make_slug(b)
        entry = {
            "id": f"{date}-{slug_a}-vs-{slug_b}",
            "date": date, "week": g.get("week"),
            "teams": [a, b],
            "class": g.get("class") if playoff else (A.get("class") if district_game else None),
            "district": g.get("district") if playoff else (A.get("district") if district_game else None),
            "district_game": district_game,
            "postseason": playoff,
            "round": g.get("round"),
            "seeds": g.get("seeds"),
            "home": g.get("home"),
            "projection": proj,
            "stakes": stakes,
            "common": common_block(a, b, results),
            "h2h": h2h,
        }
        entry["tags"] = build_tags(entry, A, B, proj, h2h, stakes) if proj else []
        games_out.append(entry)
 
    def sort_key(x):                     # by date, then best combined rank first
        ranks = [teams_out[n].get("ovr_rank") or 999 for n in x["teams"]]
        return (x["date"], sum(ranks))
    games_out.sort(key=sort_key)
 
    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "season": 2026,
        "ratings_last_updated": ratings_doc.get("last_updated"),
        "league_average": L,
        "simulations": args.sims,
        "sigma": args.sigma,
        "weeks": sorted({x["week"] for x in games_out if x["week"] is not None}),
        "teams": teams_out,
        "games": games_out,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    size = os.path.getsize(OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH}: {len(games_out)} games, {len(teams_out)} teams, {size / 1024:.0f} KB")
 
 
if __name__ == "__main__":
    main()
