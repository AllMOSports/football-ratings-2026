"""
District Seed Projections - Monte Carlo Simulation (v1)
=========================================================

Simulates the rest of the MSHSAA football regular season N times and runs
every simulated season through the exact district-points formula from
build_district_points.py, then tallies how often each team finishes at each
seed in its district.

WHY SIMULATE THE WHOLE STATE, NOT JUST DISTRICT GAMES
-----------------------------------------------------
District points include an SOS term built from every opponent's FINAL
win/loss record. A non-district game between two teams in another corner of
the state can move a district's seeding, so every remaining regular-season
game is simulated every time, and opponent records are recomputed per sim.

GAME MODEL
----------
  predicted margin (team1 - team2) = ovr_rating(team1) - ovr_rating(team2)
      (same thing as the Matchup Predictor: each side's predicted score is
       league_average + own off_rating - opp def_rating; the league average
       cancels out of the margin)
  simulated margin ~ Normal(predicted margin, SIGMA), rounded to a whole point
  a rounded margin of 0 is treated as an OVERTIME game: the winner is the
  side the unrounded draw favored, final margin = 3, overtime=True (so the
  loser gets the 15-point OT-loss value, same as a real OT loss)

  SIGMA defaults to 16 (this season's in-sample residual SD is ~14.6). Calibrate it (see --sigma) from historical
  end-of-season ratings vs. actual margins; the in-season residual this
  script prints is only a lower bound because the ratings were fit to those
  same games.

UNRATED OPPONENTS (out-of-state, mostly): their strength is estimated from
their played games against rated Missouri teams as mean(opp_ovr + margin),
shrunk toward 0 (statewide average) with SHRINK_GAMES pseudo-games.

THINGS DELIBERATELY NOT SIMULATED
---------------------------------
- Forfeits (all simulated games are played normally).
- Out-of-state opponents' records stay at their scraped values
  (out_of_state_records.json); their simulated games vs. MO teams don't
  update them, matching how build_district_points treats them.
- Games after REGULAR_SEASON_END (e.g. a Thanksgiving rivalry game) are ignored.

TIEBREAKERS: exact point ties within a district are broken at random
(break_ties()). Replace that function with MSHSAA's published district
tiebreaker order if you want it exact; ties at 4-decimal precision are rare.

Usage:
    python3 simulate_district_seeds.py                # 10,000 sims
    python3 simulate_district_seeds.py --sims 20000 --sigma 13.5 --seed 7
    python3 simulate_district_seeds.py --validate     # check vectorized scoring
                                                      # against build_district_points
"""

import argparse
import csv
import datetime
import json
from collections import defaultdict

import numpy as np

import build_district_points as bdp

RATINGS_PATH = "football_ratings_2026.json"
DISTRICT_POINTS_PATH = "district_points_2026.json"   # fallback source only
OUTPUT_PATH = "district_seed_projections_2026.json"
OUTPUT_CSV_PATH = "district_seed_projections_2026.csv"

REGULAR_SEASON_END = datetime.date(2026, 10, 24)
DEFAULT_SIMS = 10000
DEFAULT_SIGMA = 16.0
OT_MARGIN = 3
SHRINK_GAMES = 2.0
CHUNK = 1000          # sims processed per batch (memory control)

P = bdp.POINTS


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _parse_date(s):
    return datetime.datetime.strptime(bdp._normalize_date(s), "%Y-%m-%d").date()


def load_inputs():
    """
    Uses the same files/loaders as build_district_points.py. If
    classifications.json / out_of_state_*.json aren't next to this script,
    falls back to football_ratings_2026.json (class + district) and to the
    out-of-state class/record values already stamped into
    district_points_2026.json, so the simulation can still run.
    """
    try:
        team_class, team_district = bdp.load_classifications(bdp.CLASS_PATH)
    except FileNotFoundError:
        print(f"NOTE: {bdp.CLASS_PATH} not found -- using class/district from {RATINGS_PATH}.")
        team_class, team_district = {}, {}
        for t in json.load(open(RATINGS_PATH))["teams"]:
            team_class[t["school"]] = t["classification"]
            team_district[t["school"]] = t["district"]

    oos_class, oos_records = {}, {}
    try:
        oos_class = bdp.load_out_of_state_classifications(bdp.OOS_CLASS_PATH)
        oos_records = bdp.load_out_of_state_records(bdp.OOS_RECORDS_PATH)
    except FileNotFoundError:
        print(f"NOTE: {bdp.OOS_CLASS_PATH} not found -- using out-of-state class/records "
              f"recorded in {DISTRICT_POINTS_PATH}.")
        dp = json.load(open(DISTRICT_POINTS_PATH))
        for teams in dp.values():
            for t in teams:
                for g in t["games"]:
                    if g["opponent_classification_source"] == "in_state":
                        continue
                    if g["opponent_class"] is not None:
                        oos_class[g["opponent"]] = g["opponent_class"]
                    if g["opponent_record"]:
                        oos_records[g["opponent"]] = {
                            "wins": g["opponent_record"]["wins"],
                            "losses": g["opponent_record"]["losses"],
                        }

    games = bdp.load_games(bdp.GAMES_PATH)
    overrides = bdp.load_manual_overrides(bdp.MANUAL_OVERRIDES_PATH)
    bdp.apply_overtime_overrides(games, overrides)

    ratings_doc = json.load(open(RATINGS_PATH))
    ovr = {t["school"]: t["ovr_rating"] for t in ratings_doc["teams"]}
    return team_class, team_district, oos_class, oos_records, games, ovr, ratings_doc


def estimate_unrated(games, ovr):
    """Strength estimate for teams with no rating, from their played games vs rated teams."""
    sums, counts = defaultdict(float), defaultdict(int)
    for g in games:
        if g["score1"] is None or g["score2"] is None or g["forfeit"]:
            continue
        for me, opp, ms, os_ in ((g["team1"], g["team2"], g["score1"], g["score2"]),
                                 (g["team2"], g["team1"], g["score2"], g["score1"])):
            if me not in ovr and opp in ovr:
                margin = max(-35, min(35, ms - os_))   # blunt blowouts a bit
                sums[me] += ovr[opp] + margin
                counts[me] += 1
    return {t: sums[t] / (counts[t] + SHRINK_GAMES) for t in sums}


# --------------------------------------------------------------------------
# Season structure (built once)
# --------------------------------------------------------------------------

class Season:
    def __init__(self, team_class, team_district, oos_class, oos_records, games, ovr,
                 include_remaining=True):
        self.team_class = team_class
        played, remaining = [], []
        for g in games:
            if g["score1"] is not None and g["score2"] is not None:
                played.append(g)
            elif include_remaining and _parse_date(g["date"]) <= REGULAR_SEASON_END:
                remaining.append(g)
        self.played, self.remaining = played, remaining
        self.all_games = played + remaining
        n_p = len(played)

        names = sorted({g["team1"] for g in self.all_games} | {g["team2"] for g in self.all_games})
        self.names = names
        idx = {n: i for i, n in enumerate(names)}
        self.idx = idx
        N = len(names)

        # strength for every team (rated or estimated)
        est = estimate_unrated(games, ovr)
        self.strength = np.array([ovr.get(n, est.get(n, 0.0)) for n in names])
        unrated_in_use = [n for n in names if n not in ovr]
        self.n_unrated = len(unrated_in_use)

        # played-game constants
        self.p_margin = np.array([g["score1"] - g["score2"] for g in played], dtype=np.float64)
        self.p_ot = np.array([g["overtime"] for g in played], dtype=bool)
        forfeit_g = np.array([g["forfeit"] for g in played] + [False] * len(remaining), dtype=bool)

        # remaining-game predicted margins
        self.r_pred = np.array([self.strength[idx[g["team1"]]] - self.strength[idx[g["team2"]]]
                                for g in remaining], dtype=np.float64)

        # record bookkeeping: which team gets the win/loss for each game
        self.g_t1 = np.array([idx[g["team1"]] for g in self.all_games])
        self.g_t2 = np.array([idx[g["team2"]] for g in self.all_games])

        # Fixed records for out-of-state opponents (never simulated)
        self.in_state = np.array([n in team_class for n in names])
        self.oos_has_rec = np.array([(n not in team_class) and (n in oos_records) for n in names])
        self.oos_w = np.array([oos_records.get(n, {}).get("wins", 0) for n in names], dtype=np.float64)
        self.oos_l = np.array([oos_records.get(n, {}).get("losses", 0) for n in names], dtype=np.float64)

        # entries = one row per (in-state team, game) side, sorted by team
        ent = []
        for gi, g in enumerate(self.all_games):
            for me, opp, sign in ((g["team1"], g["team2"], 1.0), (g["team2"], g["team1"], -1.0)):
                if me in team_class:
                    ent.append((idx[me], idx[opp], sign, gi))
        ent.sort()
        self.e_team = np.array([e[0] for e in ent])
        self.e_opp = np.array([e[1] for e in ent])
        self.e_sign = np.array([e[2] for e in ent])
        self.e_game = np.array([e[3] for e in ent])
        self.e_forfeit = forfeit_g[self.e_game]

        def cls_of(n):
            if n in team_class:
                return team_class[n]
            return oos_class.get(n)

        u = []
        for t, o in zip(self.e_team, self.e_opp):
            mc, oc = cls_of(names[t]), cls_of(names[o])
            u.append((oc - mc) * P["class_step"] if (mc is not None and oc is not None and oc > mc) else 0)
        self.e_ubase = np.array(u, dtype=np.float64)
        self.e_has_rec = self.in_state[self.e_opp] | self.oos_has_rec[self.e_opp]

        # reduceat boundaries per team
        teams_sorted, starts = np.unique(self.e_team, return_index=True)
        self.out_teams = teams_sorted
        self.starts = starts
        self.n_total = np.diff(np.append(starts, len(self.e_team))).astype(np.float64)
        self.n_sos = np.add.reduceat(self.e_has_rec.astype(np.float64), starts)

        # districts
        self.districts = defaultdict(list)   # key -> positions into out_teams
        for pos, t in enumerate(teams_sorted):
            n = names[t]
            self.districts[(team_class[n], team_district[n])].append(pos)

    # ------------------------------------------------------------------
    def simulate_margins(self, rng, n, sigma):
        """Returns (margin[n, G], ot[n, G]) for all games, played ones constant."""
        R = len(self.remaining)
        raw = rng.normal(self.r_pred, sigma, size=(n, R))
        m = np.rint(raw)
        ot = m == 0
        m = np.where(ot, np.where(raw >= 0, OT_MARGIN, -OT_MARGIN), m)
        margin = np.concatenate([np.broadcast_to(self.p_margin, (n, len(self.played))), m], axis=1)
        ot_all = np.concatenate([np.broadcast_to(self.p_ot, (n, len(self.played))), ot], axis=1)
        return margin, ot_all

    def points(self, margin, ot):
        """District points for every in-state team in every sim -> [n, teams]."""
        n = margin.shape[0]
        N = len(self.names)
        t1_won = margin > 0
        ot_loss = ot  # in an OT game the loser gets the OT-loss value

        # final records (wins, losses, ot_losses) per team per sim
        wins = np.zeros((n, N)); losses = np.zeros((n, N)); otl = np.zeros((n, N))
        for won_side, lost_side, mask in ((self.g_t1, self.g_t2, t1_won), (self.g_t2, self.g_t1, ~t1_won)):
            mf = mask.astype(np.float64)
            for col_team_arr, target in ((won_side, wins), (lost_side, losses)):
                np.add.at(target.T, col_team_arr, mf.T)
            np.add.at(otl.T, lost_side, (mask & ot_loss).astype(np.float64).T)
        # out-of-state opponents keep scraped records
        oos = ~self.in_state
        wins[:, oos] = self.oos_w[oos]; losses[:, oos] = self.oos_l[oos]; otl[:, oos] = 0

        em = margin[:, self.e_game] * self.e_sign
        won = em > 0
        e_ot = ot[:, self.e_game]
        ff = self.e_forfeit

        T = np.where(ff, np.where(won, P["win"], P["forfeit"]),
                     np.where(won, P["win"], np.where(e_ot, P["ot_loss"], P["loss"])))
        U = np.where(ff & ~won, 0.0, self.e_ubase)
        X = np.clip(em, -P["diff_cap"], P["diff_cap"])

        ow, ol, oo = wins[:, self.e_opp], losses[:, self.e_opp], otl[:, self.e_opp]
        hr = self.e_has_rec
        V = np.where(hr, ow * P["opp_win"], 0.0)
        W = np.where(hr, oo * P["opp_ot_loss"] + (ol - oo) * P["opp_loss"], 0.0)
        Y = np.where(hr, np.where(won, P["loss"], P["win"]), 0.0)
        D = np.where(hr, ow + ol, 0.0)

        red = lambda a: np.add.reduceat(a, self.starts, axis=1)
        base = (red(T) + red(U) + red(X)) / self.n_total
        denom = red(D) - self.n_sos
        num = red(V) + red(W) - red(Y)
        sos = np.divide(num, denom, out=np.zeros_like(num), where=denom != 0)
        return base + sos


def break_ties(rng, pts):
    """Random tiebreak (placeholder for MSHSAA's tiebreaker order)."""
    return pts + rng.uniform(0, 1e-6, size=pts.shape)


# --------------------------------------------------------------------------

def validate(season):
    """Played games only: vectorized points must equal build_district_points output."""
    margin = season.p_margin[None, :]
    ot = season.p_ot[None, :]
    pts = season.points(margin, ot)[0]
    schedule, record = bdp.build_schedules_and_records(season.played)
    worst = 0.0
    for pos, t in enumerate(season.out_teams):
        n = season.names[t]
        ref = bdp.compute_team_points(n, schedule, record, season.team_class,
                                      VAL_OOS_CLASS, VAL_OOS_REC)
        worst = max(worst, abs(ref["total_points"] - pts[pos]))
    print(f"Validation: max |difference| vs build_district_points = {worst:.6f} "
          f"across {len(season.out_teams)} teams")


def residual_sd(season):
    pred = season.strength[season.g_t1[:len(season.played)]] - season.strength[season.g_t2[:len(season.played)]]
    return float(np.std(season.p_margin - pred))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    team_class, team_district, oos_class, oos_records, games, ovr, ratings_doc = load_inputs()
    global VAL_OOS_CLASS, VAL_OOS_REC
    VAL_OOS_CLASS, VAL_OOS_REC = oos_class, oos_records

    if args.validate:
        validate(Season(team_class, team_district, oos_class, oos_records, games, ovr,
                        include_remaining=False))
        return

    season = Season(team_class, team_district, oos_class, oos_records, games, ovr)
    print(f"Played games: {len(season.played)}   Remaining regular-season games simulated: "
          f"{len(season.remaining)}")
    print(f"Teams without a rating (strength estimated): {season.n_unrated}")
    print(f"In-season residual SD (lower bound for sigma): {residual_sd(season):.2f}   "
          f"using sigma = {args.sigma}")

    rng = np.random.default_rng(args.seed)
    n_out = len(season.out_teams)
    max_size = max(len(v) for v in season.districts.values())
    seed_counts = np.zeros((n_out, max_size), dtype=np.int64)
    pts_sum = np.zeros(n_out)

    done = 0
    while done < args.sims:
        n = min(CHUNK, args.sims - done)
        margin, ot = season.simulate_margins(rng, n, args.sigma)
        pts = break_ties(rng, season.points(margin, ot))
        pts_sum += pts.sum(axis=0)
        for positions in season.districts.values():
            pos = np.array(positions)
            order = np.argsort(-pts[:, pos], axis=1)          # [n, k] best first
            seeds = np.empty_like(order)
            np.put_along_axis(seeds, order, np.arange(len(pos))[None, :].repeat(n, 0), axis=1)
            for j, p in enumerate(pos):
                seed_counts[p, :len(pos)] += np.bincount(seeds[:, j], minlength=len(pos))
        done += n

    # current (played-only) points for context
    cur_season = Season(team_class, team_district, oos_class, oos_records, games, ovr,
                        include_remaining=False)
    cur_pts = cur_season.points(cur_season.p_margin[None, :], cur_season.p_ot[None, :])[0]
    cur_by_name = {cur_season.names[t]: cur_pts[i] for i, t in enumerate(cur_season.out_teams)}

    remaining_by_team = defaultdict(int)
    for g in season.remaining:
        remaining_by_team[g["team1"]] += 1
        remaining_by_team[g["team2"]] += 1
    rec = defaultdict(lambda: [0, 0])
    for g in season.played:
        w1 = g["score1"] > g["score2"]
        rec[g["team1"]][0 if w1 else 1] += 1
        rec[g["team2"]][1 if w1 else 0] += 1

    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "ratings_last_updated": ratings_doc.get("last_updated"),
        "simulations": args.sims,
        "sigma": args.sigma,
        "remaining_games_simulated": len(season.remaining),
        "districts": {},
    }
    rows = []
    for (cls, dist), positions in sorted(season.districts.items()):
        k = len(positions)
        teams = []
        for p in positions:
            name = season.names[season.out_teams[p]]
            probs = seed_counts[p, :k] / args.sims
            teams.append({
                "team": name,
                "record": f"{rec[name][0]}-{rec[name][1]}",
                "games_remaining": remaining_by_team[name],
                "current_points": round(float(cur_by_name.get(name, 0.0)), 4),
                "projected_points": round(float(pts_sum[p] / args.sims), 4),
                "avg_seed": round(float((probs * np.arange(1, k + 1)).sum()), 3),
                "seed_probs": [round(float(x), 4) for x in probs],
                "top2_prob": round(float(probs[:2].sum()), 4),
            })
        teams.sort(key=lambda t: t["avg_seed"])
        out["districts"][f"class{cls}_district{dist}"] = {"class": cls, "district": dist, "teams": teams}
        for t in teams:
            rows.append({"class": cls, "district": dist, **{k2: t[k2] for k2 in
                         ("team", "record", "games_remaining", "current_points",
                          "projected_points", "avg_seed", "top2_prob")},
                         **{f"seed{i+1}": t["seed_probs"][i] for i in range(len(t["seed_probs"]))}})

    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    fields = ["class", "district", "team", "record", "games_remaining", "current_points",
              "projected_points", "avg_seed", "top2_prob"] + [f"seed{i+1}" for i in range(max_size)]
    with open(OUTPUT_CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUTPUT_PATH} and {OUTPUT_CSV_PATH} ({len(out['districts'])} districts)")


VAL_OOS_CLASS, VAL_OOS_REC = {}, {}

if __name__ == "__main__":
    main()
