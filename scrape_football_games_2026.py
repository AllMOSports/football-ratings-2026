"""
MSHSAA Football GAMES Scraper - 2026 Season (schedule pull, no ratings)
=========================================================================
 
Adapted directly from football_ratings_2025.py, which is proven to work
correctly against the live MSHSAA scoreboard. Same request approach
(requests + BeautifulSoup, NOT Playwright -- the page is plain server-
rendered HTML, no JS execution needed), same session/retry logic, same
name-resolution via school ID in the team link href.
 
WHAT'S DIFFERENT FROM THE 2025 RATINGS SCRIPT
-----------------------------------------------
2025's scrape_date() only kept a game if the table's last row said
"final" AND both scores parsed as valid ints -- correct for building
ratings off completed games, but it would throw away every 2026 game
since none have been played yet.
 
This version:
  1. Drops the "final" requirement entirely.
  2. Detects team cells by scanning ALL cells in ALL rows for the
     MySchool/Schedule.aspx link (instead of assuming team names always
     live at rows[1]/rows[2], td index [1]) -- this is more robust to
     scheduled-game tables possibly having a different column layout
     than completed-game tables (e.g. a time/TBA column instead of a
     score column). It reuses the exact same is_mshsaa_team/resolve_name
     logic that already works, just applied more flexibly.
  3. Score is optional -- captured if present and parseable, otherwise
     stored as null. Games are never dropped for lacking a score.
  4. No date.today() cap -- 2025's script capped scraping at "today"
     since it was pulling a season already in progress. This script
     scrapes the full configured range regardless, since we're pulling
     a schedule that's mostly in the future. Dates with nothing posted
     yet just return 0 games, same as any other empty day.
 
DISTRICT POINTS CALCULATOR SUPPORT (new)
-----------------------------------------
The District Points Calculator page needs to know, per game, whether it
went to overtime and whether it was decided by forfeit -- both feed
directly into the scoring formula. Two changes support that:
  5. Forfeited games are no longer dropped. is_forfeit() still detects
     them the same way as before; the result is now stored as a
     "forfeit" boolean on the game instead of being used to discard it.
  6. A new is_overtime() check looks for "overtime"/"OT" in the game's
     row text and stores it as an "overtime" boolean.
  NOTE: is_overtime()'s text match is a first pass, unverified against
  a real OT game on the live scoreboard (this environment can't reach
  mshsaa.org to check). Also note neither flag says WHICH team forfeited
  or lost in OT -- that's derived downstream by comparing score1/score2
  once known. Spot-check the first few forfeit/OT games each week against
  the live scoreboard and tell me if the wording differs from what
  is_overtime() looks for, or if a forfeit's score pattern needs
  special-casing (e.g. a 1-0 placeholder score).
 
REQUIRES (same as your existing pipeline, must be in the same directory
or update the paths below):
  - classifications.json  (2026-27 projected classifications)
  - mshsaa_schools.csv     (school_id -> school_name lookup)
 
Usage:
    python3 scrape_football_games_2026.py
"""
 
import requests
from bs4 import BeautifulSoup
import json
import csv
import re
import pandas as pd
from datetime import date, timedelta
import time
 
# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
 
SEASON_YEAR   = 2026
SEASON_START  = date(2026, 8, 1)
SEASON_END    = date(2026, 12, 15)   # matches 2025 script's default; adjust if MSHSAA's championship date differs
BASE_URL      = "https://www.mshsaa.org/activities/scoreboard.aspx?alg=19&date={}"
MAX_POINTS    = 100
OUTPUT_JSON   = f"football_games_{SEASON_YEAR}.json"
OUTPUT_CSV    = f"football_games_{SEASON_YEAR}.csv"
OUTPUT_JSON_ALL = f"football_games_{SEASON_YEAR}_all.json"
OUTPUT_CSV_ALL  = f"football_games_{SEASON_YEAR}_all.csv"
CLASSIFICATIONS_PATH = "classifications.json"
SCHOOLS_CSV           = "mshsaa_schools.csv"
 
REQUEST_DELAY = 0.5  # seconds between requests, matches 2025 script
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.mshsaa.org/"
}
 
# ---------------------------------------------------------------------------
# HTTP SESSION (identical to football_ratings_2025.py)
# ---------------------------------------------------------------------------
 
def build_session():
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        from requests.packages.urllib3.util.retry import Retry
 
    session = requests.Session()
    retry = Retry(
        total=1,
        connect=1,
        read=1,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
 
 
# ---------------------------------------------------------------------------
# CLASSIFICATIONS / NAME RESOLUTION (identical to football_ratings_2025.py)
# ---------------------------------------------------------------------------
 
def load_classifications(path=CLASSIFICATIONS_PATH):
    with open(path) as f:
        data = json.load(f)
    team_to_class    = {}
    team_to_district = {}
    for entry in data["teams"]:
        school = entry["school"]
        team_to_class[school]    = entry["classification"]
        team_to_district[school] = entry["district"]
    return team_to_class, team_to_district
 
 
def build_id_to_classname(team_to_class, schools_csv=SCHOOLS_CSV):
    """
    Build { school_id_str : classification_name }. NOTE: the MANUAL_OVERRIDES
    dict below is copied from football_ratings_2025.py as of the last known
    good run. If your 2026 classifications.json renames/adds/removes any
    co-op programs, this list may need updating -- cross-check against
    whatever produced football_ratings_2025's overrides originally.
    """
    MANUAL_OVERRIDES = {
        "271": "Clopton with Elsberry",
        "331": "King City with Pattonsburg",
        "126": "Lockwood with Golden City",
        "421": "Princeton with Mercer",
        "424": "Rich Hill with Hume",
        "431": "Salisbury",
        "435": "Scott City",
        "443": "Skyline",
        "193": "Slater",
        "194": "Smith-Cotton",
        "197": "South Callaway",
        "549": "St. Mary's South Side",
        "463": "Stockton",
        "207": "Sullivan",
        "208": "Sumner",
        "469": "Sweet Springs with Malta Bend",
        "198": "Truman",
        "479": "University Academy Charter",
        "204": "Van Horn",
        "206": "Vashon",
        "20": "Appleton City with Montrose",
        "275": "Drexel with Miami (Amoret)",
        "575": "Renaissance Academy Charter",
        "172": "St. James",
        "35": "DeSoto with Kingston",
        "917": "Father Tolton with Calvary Lutheran",
        "342": "Liberal with Bronaugh",
        "776": "Transportation and Law with Beaumont",
        "483": "Van-Far with Community",
    }
 
    df = pd.read_csv(schools_csv)
    known_class_names = set(team_to_class.keys())
 
    id_to_classname = {}
    for _, row in df.iterrows():
        full_name = row["school_name"]
        sid       = str(row["school_id"])
        stripped  = full_name.replace(" High School", "").strip()
 
        if stripped in known_class_names:
            id_to_classname[sid] = stripped
        elif full_name in known_class_names:
            id_to_classname[sid] = full_name
 
    id_to_classname.update(MANUAL_OVERRIDES)
 
    print(f"  [name-resolve] {len(id_to_classname)} schools mapped by ID "
          f"({len(MANUAL_OVERRIDES)} via manual overrides)")
    return id_to_classname
 
 
def resolve_name(cell, id_to_classname, known_teams):
    """Identical logic to football_ratings_2025.py."""
    a = cell.find("a", href=lambda h: h and "/MySchool/Schedule.aspx" in h)
    if not a:
        return None
 
    href  = a.get("href", "")
    match = re.search(r"[?&]s=(\d+)", href, re.IGNORECASE)
    if match:
        sid = match.group(1)
        if sid in id_to_classname:
            return id_to_classname[sid]
 
    display_text = a.get_text(strip=True)
    if display_text in known_teams:
        return display_text
 
    return None
 
 
# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------
 
def resolve_name_or_raw(cell, id_to_classname, known_teams):
    """
    Like resolve_name(), but never returns None for a cell that has an
    MSHSAA schedule link -- if the school ID/display text doesn't match
    classifications.json (an unclassified or missing school), this falls
    back to whatever display text the scoreboard link shows, and reports
    that the team is unclassified via the second return value.
    Returns (name, classified: bool).
    """
    name = resolve_name(cell, id_to_classname, known_teams)
    if name is not None:
        return name, True
 
    a = cell.find("a", href=lambda h: h and "/MySchool/Schedule.aspx" in h)
    raw = a.get_text(strip=True) if a else None
    return raw, False
 
 
def is_mshsaa_team(cell):
    return cell.find(
        "a", href=lambda h: h and "/MySchool/Schedule.aspx" in h
    ) is not None
 
 
def parse_score(text):
    text = text.strip()
    if not text:
        return None
    try:
        score = int(text)
    except ValueError:
        return None
    return score if 0 <= score <= MAX_POINTS else None
 
 
def is_forfeit(row1, row2):
    return "forfeit" in (row1.get_text() + row2.get_text()).lower()
 
 
def is_overtime(row1, row2):
    """
    First-pass OT detection: looks for "overtime" or a standalone "OT"
    token in the game's row text (e.g. a "Final/OT" status flag some
    scoreboards use). UNVERIFIED against a real MSHSAA OT game -- confirm
    the actual wording once a live OT game shows up and adjust the regex
    if needed.
    """
    text = row1.get_text() + " " + row2.get_text()
    return bool(re.search(r"overtime|\bOT\b", text, re.IGNORECASE))
 
 
def scrape_date(target_date, id_to_classname, known_teams, session):
    """
    Generalized version of football_ratings_2025.py's scrape_date():
    scans every row in every table for a cell containing an MSHSAA team
    link (rather than assuming team names always sit at a fixed row/column
    index), so it works whether the table has a score column (completed
    games) or not (scheduled games). Pairs up tables with exactly 2
    team-rows as a single game. Score is captured if present, else None.
    """
    url = BASE_URL.format(target_date.strftime("%m%d%Y"))
    try:
        resp = session.get(url, timeout=(10, 25), headers=HEADERS)
        resp.raise_for_status()
    except requests.exceptions.Timeout as e:
        print(f"  TIMEOUT {target_date}: {e}")
        return [], "timeout"
    except requests.RequestException as e:
        print(f"  Failed {target_date}: {e}")
        return [], "error"
 
    soup  = BeautifulSoup(resp.text, "html.parser")
    games = []
 
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
 
        team_rows = []  # list of (name, classified, score, row) for rows with a team link
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            team_cell = next((c for c in cells if is_mshsaa_team(c)), None)
            if team_cell is None:
                continue
 
            name, classified = resolve_name_or_raw(team_cell, id_to_classname, known_teams)
            if name is None:
                # Has the schedule link but somehow no display text either --
                # too broken to use, skip just this row.
                continue
 
            score = None
            for c in cells:
                s = parse_score(c.get_text())
                if s is not None:
                    score = s
                    break
 
            team_rows.append((name, classified, score, row))
 
        if len(team_rows) != 2:
            continue  # not a clean 2-team game table -- skip
 
        (name1, classified1, s1, row1), (name2, classified2, s2, row2) = team_rows
        if name1 == name2:
            continue
 
        games.append({
            "date": target_date.strftime("%Y-%m-%d"),
            "team1": name1,
            "team1_classified": classified1,
            "score1": s1,
            "team2": name2,
            "team2_classified": classified2,
            "score2": s2,
            "forfeit": is_forfeit(row1, row2),
            "overtime": is_overtime(row1, row2),
        })
 
    return games, None
 
 
def scrape_full_season(id_to_classname, known_teams):
    all_games   = []
    current     = SEASON_START
    scrape_t0   = time.perf_counter()
    slow_days   = []
    failed_days = []
    session     = build_session()
 
    while current <= SEASON_END:
        day_t0 = time.perf_counter()
        print(f"  Scraping {current}...", end=" ", flush=True)
        day_games, fail_reason = scrape_date(current, id_to_classname, known_teams, session)
        all_games.extend(day_games)
        day_elapsed = time.perf_counter() - day_t0
        print(f"{len(day_games)} games ({day_elapsed:.1f}s)")
        if day_elapsed > 3.0:
            slow_days.append((current, day_elapsed))
        if fail_reason is not None:
            failed_days.append((current, fail_reason))
        current += timedelta(days=1)
        time.sleep(REQUEST_DELAY)
 
    scrape_elapsed = time.perf_counter() - scrape_t0
    print(f"\n  [TIMING] Scraping took {scrape_elapsed:.1f}s total "
          f"for {len(all_games)} games.")
    if slow_days:
        print(f"  [TIMING] {len(slow_days)} slow day(s) (>3s each):")
        for d, secs in slow_days:
            print(f"    {d}: {secs:.1f}s")
    if failed_days:
        print(f"\n  *** {len(failed_days)} date(s) NEVER returned data, "
              f"even after retry: ***")
        for d, reason in failed_days:
            print(f"    {d} ({reason})")
    else:
        print("  All dates returned successfully -- no known data gaps "
              "from scraping failures.")
    return all_games
 
 
def deduplicate_games(all_games):
    """Same score-independent dedup key as football_ratings_2025.py."""
    seen = set()
    unique_games = []
    duplicates = 0
    for g in all_games:
        key = (g["date"], frozenset([g["team1"], g["team2"]]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique_games.append(g)
 
    if duplicates:
        print(f"  Removed {duplicates} duplicate game(s). "
              f"{len(unique_games)} unique games remain.")
    else:
        print(f"  No duplicates found. {len(unique_games)} games.")
    return unique_games
 
 
def strict_games_from_all(all_games):
    """
    Filters the full (>=1 classified team) game list down to games where
    BOTH teams are in classifications.json, and reshapes each record back
    to the original schema (no *_classified fields) so this stays a
    drop-in replacement for whatever already consumes football_games_2026.json.
    """
    strict = []
    for g in all_games:
        if not (g["team1_classified"] and g["team2_classified"]):
            continue
        strict.append({
            "date": g["date"],
            "team1": g["team1"],
            "score1": g["score1"],
            "team2": g["team2"],
            "score2": g["score2"],
            "forfeit": g["forfeit"],
            "overtime": g["overtime"],
        })
    return strict
 
 
def save_json(all_games, path=OUTPUT_JSON):
    with open(path, "w") as f:
        json.dump(all_games, f, indent=2)
    print(f"Saved {len(all_games)} games to {path}")
 
 
def save_csv(all_games, path=OUTPUT_CSV):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "score1", "team2", "score2", "forfeit", "overtime"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["score1"], g["team2"], g["score2"],
                              g["forfeit"], g["overtime"]])
    print(f"Saved {len(all_games)} games to {path}")
 
 
def save_csv_all(all_games, path=OUTPUT_CSV_ALL):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "team1_classified", "score1",
                          "team2", "team2_classified", "score2",
                          "forfeit", "overtime"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["team1_classified"], g["score1"],
                              g["team2"], g["team2_classified"], g["score2"],
                              g["forfeit"], g["overtime"]])
    print(f"Saved {len(all_games)} games to {path}")
 
 
# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    print(f"=== MSHSAA Football Games Pull {SEASON_YEAR} (schedule, no ratings) ===")
 
    print("\nLoading classifications...")
    team_to_class, team_to_district = load_classifications()
    known_teams = set(team_to_class.keys())
    print(f"  Loaded {len(team_to_class)} teams from {CLASSIFICATIONS_PATH}")
 
    print("\nBuilding school ID -> classification name lookup...")
    id_to_classname = build_id_to_classname(team_to_class, SCHOOLS_CSV)
 
    print(f"\nScraping {SEASON_START} to {SEASON_END}...")
    all_games = scrape_full_season(id_to_classname, known_teams)
    print(f"\nTotal games found (before dedup, >=1 classified team): {len(all_games)}")
 
    if not all_games:
        print("No games found. This is expected if the 2026 schedule "
              "hasn't been posted to MSHSAA yet -- try again closer to "
              "the season, or check a known date manually in a browser.")
 
    print("\nDeduplicating...")
    all_games = deduplicate_games(all_games)
 
    strict_games = strict_games_from_all(all_games)
    print(f"Of those, {len(strict_games)} have both teams classified "
          f"({len(all_games) - len(strict_games)} have exactly one classified side).")
 
    print("\nSaving output...")
    save_json(strict_games, OUTPUT_JSON)
    save_csv(strict_games, OUTPUT_CSV)
    save_json(all_games, OUTPUT_JSON_ALL)
    save_csv_all(all_games, OUTPUT_CSV_ALL)
 
    print("\n=== Done ===")
