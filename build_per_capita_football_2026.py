"""
All MO Sports - Football "Best Teams Per Capita" (2026 season only)
 
Method: Rating Over Expected
  1. Fit an expected-rating curve on 2026 teams:  OVR = a + b * ln(enrollment)
     (fit on boundaried/public schools only by default, so private schools
      that recruit beyond their enrollment don't drag the curve upward)
  2. Points Over Expected (POE) = actual OVR - expected OVR
  3. Teams are ranked by POE. A size-adjusted z-score (POE / local spread,
     with the spread modeled against ln(enrollment)) is kept as a reference
     column only, since the spread is nearly flat across school sizes
  4. Private (non-charter, non-boundaried) schools are scored against the
     expected rating for PRIVATE_FACTOR x their enrollment, since they draw
     from a wider area. Charters stay on raw enrollment.
  5. "Plays like a school of N": inverts the curve to find the enrollment
     whose expected OVR equals the team's actual OVR (clamped to the
     observed enrollment range)
 
Inputs (local file paths tried first, then GitHub raw URLs):
  - football_ratings_2026.json           (football-ratings-2026 repo)
  - mshsaa_enrollments_2026-2027.json  (MSHSAA 2026-27 enrollments, same repo)
 
Outputs:
  - per_capita_football_2026.json
  - per_capita_football_2026.csv
"""
 
import csv
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
 
import numpy as np
 
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEASON = 2026
 
RATINGS_SOURCES = [
    "football_ratings_2026.json",
    "https://raw.githubusercontent.com/AllMOSports/football-ratings-2026/main/football_ratings_2026.json",
]
ENROLLMENT_SOURCES = [
    "mshsaa_enrollments_2026-2027.json",
    "https://raw.githubusercontent.com/AllMOSports/football-ratings-2026/main/mshsaa_enrollments_2026-2027.json",
]
 
# Co-ops ("Tipton with Bunceton", "Pleasant Hope with Halfway, Marion C. Early"):
# True = sum every member school's enrollment; False = primary school only.
COOP_COMBINE = True
 
OUT_JSON = "per_capita_football_2026.json"
OUT_CSV = "per_capita_football_2026.csv"
 
# Fit the expected curve on boundaried (public) schools only.
FIT_PUBLIC_ONLY = True
 
# Non-boundaried schools (private + charter). Flagged in output and excluded
# from the curve fit. Edit as needed.
NON_BOUNDARIED = {
    "Cardinal Ritter", "Chaminade College Prep", "Christian Brothers College",
    "De Smet Jesuit", "Duchesne", "Father Tolton", "Helias Catholic",
    "John Burroughs", "Lutheran North", "Lutheran South", "Lutheran St. Charles",
    "MICDS", "Pembroke Hill", "Principia", "Priory", "Rockhurst", "SLUH",
    "Saxony Lutheran", "Springfield Catholic", "St. Dominic",
    "St. Francis Borgia", "St. Mary's South Side", "St. Michael the Archangel",
    "St. Pius X (Festus)", "St. Pius X (Kansas City)", "St. Vincent",
    "Summit Christian Academy", "Valle Catholic", "Veritas Christian Academy",
    "Vianney", "Westminster Christian Academy",
    # Charters
    "Confluence Prep Academy Charter", "Hogan Prep Academy Charter",
    "KIPP KC Legacy", "Lift for Life Academy Charter",
    "University Academy Charter with Ewing Marion Kauffman",
}
 
# Manual enrollment fills/fixes keyed by the ratings-file school name.
# Teams with no enrollment found are skipped (and listed in the console).
ENROLLMENT_OVERRIDES = {
    # "East (Kansas City)": 0,
}
 
# Round "plays like" enrollment to this step for display.
PLAYS_LIKE_ROUND = 10
 
# Minimum share of the typical spread used for the z-score denominator,
# so a near-zero modeled spread can't blow up z-scores.
SPREAD_FLOOR_FRAC = 0.5
 
# Private-school enrollment factor. Measured from 2026 data: with the curve fit
# on public schools, privates (charters excluded) average ~+18 points over
# expected, the same as playing like a school ~2.2x their enrollment.
# Set to 1.0 to turn the adjustment off.
PRIVATE_FACTOR = 2.2
 
# Charters are non-boundaried but, as a group, do not outperform their
# enrollment, so PRIVATE_FACTOR is not applied to them.
CHARTER_SCHOOLS = {
    "Confluence Prep Academy Charter", "Hogan Prep Academy Charter",
    "KIPP KC Legacy", "Lift for Life Academy Charter",
    "University Academy Charter with Ewing Marion Kauffman",
}
 
 
# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_json(sources):
    for src in sources:
        try:
            if src.startswith("http"):
                with urllib.request.urlopen(src, timeout=30) as resp:
                    print(f"  loaded {src}")
                    return json.loads(resp.read().decode("utf-8"))
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), src)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    print(f"  loaded {path}")
                    return json.load(f)
        except Exception as e:  # try next source
            print(f"  could not load {src}: {e}")
    sys.exit(f"ERROR: none of these sources could be loaded: {sources}")
 
 
def build_enrollment_lookup(class_data):
    """Return {school_name: enrollment} from an enrollment JSON ({"teams": [{"name", "enrollment"}]})."""
    if "sports" in class_data:
        teams = class_data["sports"]["Football"]["teams"]
    else:
        teams = class_data.get("teams", [])
    lookup = {}
    for t in teams:
        name = t.get("name") or t.get("school")
        enr = t.get("enrollment")
        if name and enr:
            lookup[name] = float(enr)
    return lookup
 
 
def find_enrollment(school, lookup):
    if school in ENROLLMENT_OVERRIDES:
        return float(ENROLLMENT_OVERRIDES[school])
    if school in lookup:
        return lookup[school]
    # Co-ops are listed as "Primary with Partner[, Partner]" in the ratings.
    if " with " in school:
        primary, partners = school.split(" with ", 1)
        members = [primary.strip()] + [p.strip() for p in partners.split(",")]
        if primary.strip() not in lookup:
            return None
        if not COOP_COMBINE:
            return lookup[primary.strip()]
        missing = [m for m in members if m not in lookup]
        if missing:
            print(f"  co-op {school}: no enrollment for {missing}, using known members only")
        return sum(lookup[m] for m in members if m in lookup)
    return None
 
 
# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def fit_line(x, y):
    """Ordinary least squares y = a + b*x. Returns (a, b, r2)."""
    b, a = np.polyfit(x, y, 1)
    pred = a + b * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(a), float(b), r2
 
 
def main():
    print("Loading data...")
    ratings = load_json(RATINGS_SOURCES)
    class_data = load_json(ENROLLMENT_SOURCES)
    enrollment = build_enrollment_lookup(class_data)
 
    teams, missing = [], []
    for t in ratings["teams"]:
        enr = find_enrollment(t["school"], enrollment)
        if not enr or enr <= 0:
            missing.append(t["school"])
            continue
        nb = t["school"] in NON_BOUNDARIED
        factor = PRIVATE_FACTOR if (nb and t["school"] not in CHARTER_SCHOOLS) else 1.0
        teams.append({**t, "enrollment": enr, "non_boundaried": nb,
                      "private_factor": factor,
                      "effective_enrollment": enr * factor})
 
    if missing:
        print(f"\nSkipped {len(missing)} team(s) with no enrollment "
              f"(add to ENROLLMENT_OVERRIDES): {', '.join(missing)}")
 
    # --- 1. Expected-rating curve -----------------------------------------
    fit_set = [t for t in teams if not (FIT_PUBLIC_ONLY and t["non_boundaried"])]
    x_fit = np.log(np.array([t["enrollment"] for t in fit_set]))
    y_fit = np.array([t["ovr_rating"] for t in fit_set])
    a, b, r2 = fit_line(x_fit, y_fit)
    if b <= 0:
        sys.exit("ERROR: fitted slope is not positive; check input data.")
 
    # --- 2. Spread model: |residual| vs ln(enrollment) ---------------------
    abs_res = np.abs(y_fit - (a + b * x_fit))
    sa, sb, _ = fit_line(x_fit, abs_res)
    # mean |e| = sigma * sqrt(2/pi) for a normal distribution
    k = math.sqrt(math.pi / 2)
    typical_sd = float(np.std(y_fit - (a + b * x_fit), ddof=2))
    sd_floor = SPREAD_FLOOR_FRAC * typical_sd
 
    enr_min = min(t["enrollment"] for t in teams)
    enr_max = max(t["enrollment"] for t in teams)
 
    def expected(enr):
        return a + b * math.log(enr)
 
    def local_sd(enr):
        return max(k * (sa + sb * math.log(enr)), sd_floor)
 
    def plays_like(ovr):
        raw = math.exp((ovr - a) / b)
        clamped = min(max(raw, enr_min), enr_max)
        flag = "max" if raw > enr_max else ("min" if raw < enr_min else None)
        rounded = int(round(clamped / PLAYS_LIKE_ROUND) * PLAYS_LIKE_ROUND)
        return rounded, flag
 
    # --- 3. Score every team ----------------------------------------------
    for t in teams:
        exp_ovr = expected(t["effective_enrollment"])
        poe = t["ovr_rating"] - exp_ovr
        sd = local_sd(t["effective_enrollment"])
        pl, pl_flag = plays_like(t["ovr_rating"])
        t.update({
            "expected_ovr": round(exp_ovr, 2),
            "points_over_expected": round(poe, 2),
            "z_score": round(poe / sd, 3),
            "local_sd": round(sd, 2),
            "plays_like_enrollment": pl,
            "plays_like_capped": pl_flag,       # "max"/"min" when clamped
            "size_multiple": round(pl / t["enrollment"], 2),
        })
 
    # --- 4. Ranks -----------------------------------------------------------
    # Primary ranking: points over expected (z-score breaks exact ties)
    teams.sort(key=lambda t: (t["points_over_expected"], t["z_score"]),
               reverse=True)
    for i, t in enumerate(teams, 1):
        t["per_capita_rank"] = i
    for i, t in enumerate(sorted(teams, key=lambda t: t["z_score"],
                                 reverse=True), 1):
        t["z_rank"] = i
    pub = [t for t in teams if not t["non_boundaried"]]
    for i, t in enumerate(pub, 1):
        t["public_rank"] = i
    for c in sorted({t["classification"] for t in teams}):
        for i, t in enumerate([t for t in teams if t["classification"] == c], 1):
            t["class_rank"] = i
 
    # --- 5. Write -----------------------------------------------------------
    fields = ["per_capita_rank", "public_rank", "class_rank", "z_rank",
              "school", "classification", "district", "non_boundaried",
              "enrollment", "private_factor", "effective_enrollment", "ovr_rating", "ovr_rank", "expected_ovr",
              "points_over_expected", "z_score", "local_sd",
              "plays_like_enrollment", "plays_like_capped", "size_multiple"]
    rows = [{f: (int(round(t[f])) if f in ("enrollment", "effective_enrollment")
                 else t.get(f)) for f in fields}
            for t in teams]
 
    out = {
        "season": SEASON,
        "sport": "football",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ratings_last_updated": ratings.get("last_updated"),
        "method": {
            "description": "Rating Over Expected: OVR minus the expected OVR "
                           "for a school of that enrollment, fit on 2026 teams.",
            "curve": "expected_ovr = a + b * ln(enrollment)",
            "a": round(a, 4),
            "b": round(b, 4),
            "r_squared": round(r2, 4),
            "fit_on": "boundaried (public) schools" if FIT_PUBLIC_ONLY else "all schools",
            "fit_n": len(fit_set),
            "spread_model": "local_sd = sqrt(pi/2) * (sa + sb * ln(enrollment))",
            "sa": round(sa, 4),
            "sb": round(sb, 4),
            "typical_sd": round(typical_sd, 2),
            "enrollment_range": [int(enr_min), int(enr_max)],
            "sort": "points_over_expected (descending)",
            "private_factor": PRIVATE_FACTOR,
            "private_factor_applies_to": "non-boundaried schools except charters",
        },
        "skipped_no_enrollment": missing,
        "teams": rows,
    }
 
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, OUT_JSON), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    with open(os.path.join(base, OUT_CSV), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
 
    # --- Console summary ----------------------------------------------------
    print(f"\nCurve: expected OVR = {a:.2f} + {b:.2f} * ln(enrollment)   "
          f"R^2 = {r2:.3f}  (n = {len(fit_set)})")
    print(f"Every doubling of enrollment = +{b * math.log(2):.1f} expected OVR points")
    print(f"\nTop 15 per capita ({len(teams)} teams scored):")
    print(f"{'#':>3}  {'School':<34}{'Enr':>6}{'OVR':>8}{'Exp':>8}{'POE':>8}"
          f"{'z':>7}  Plays like")
    for t in teams[:15]:
        tag = " *" if t["non_boundaried"] else ""
        cap = "+" if t["plays_like_capped"] == "max" else ""
        print(f"{t['per_capita_rank']:>3}  {(t['school'] + tag)[:33]:<34}"
              f"{int(t['enrollment']):>6}{t['ovr_rating']:>8.1f}"
              f"{t['expected_ovr']:>8.1f}{t['points_over_expected']:>+8.1f}"
              f"{t['z_score']:>7.2f}  {t['plays_like_enrollment']:,}{cap}")
    print("  * = non-boundaried (private/charter)")
    print(f"\nWrote {OUT_JSON} and {OUT_CSV}")
 
 
if __name__ == "__main__":
    main()
