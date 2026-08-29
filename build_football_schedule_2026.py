#!/usr/bin/env python3
"""
Build_Football_History_json.py
 
Aggregates historical AllMOSports football ratings (2010-2024) AND
season records (record/PPG/PAPG/win%/point diff) from two per-year
GitHub sources into a single consolidated JSON file that the Sport
Detail page's year dropdown can fetch once and slice client-side.
 
Sources per year:
  1. Ratings:  https://raw.githubusercontent.com/AllMOSports/football-ratings-{year}/main/football_ratings_{year}.json
  2. Records:  https://raw.githubusercontent.com/AllMOSports/All_MO_Sports-Data/main/output/mshsaa_historical_records/football/football_{year}.json
 
Output shape (football_history.json):
{
  "sport": "football",
  "years": [2010, 2011, ..., 2024],
  "teams": {
    "<slug>": {
      "school": "Lafayette (Wildwood)",
      "history": {
        "2010": {
          "ovr_rank": 87, "classification": 6, "district": 3,
          "ovr_rating": 16.65, "off_rating": 8.31, "off_rank": 91,
          "def_rating": 8.33, "def_rank": 92,
          "wins": 12, "losses": 1, "win_pct": 92.31, "games_played": 13,
          "ppg": 38.38, "papg": 5.54, "points_for": 499, "points_against": 72,
          "point_diff": 427, "mov": 32.85
        },
        "2011": {...},
        ...
      }
    },
    ...
  }
}
 
Usage:
  python Build_Football_History_json.py
"""
 
import json
import re
import unicodedata
import urllib.request
from pathlib import Path
 
# ---------- CONFIG ----------
ORG = "AllMOSports"
YEARS = range(2010, 2026)  # 2010-2025 inclusive
 
# CONFIRM/EDIT: repo containing football-ratings-2010.json actually also
# holds 2011 and 2019 files. If other years live in that SAME repo rather
# than their own "football-ratings-YYYY" repo, update this mapping.
YEAR_REPO_OVERRIDES = {
    # 2011: "football-ratings-2010",
    # 2019: "football-ratings-2010",
}
 
def repo_and_file_for_year(year: int):
    repo = YEAR_REPO_OVERRIDES.get(year, f"football-ratings-{year}")
    fname = f"football_ratings_{year}.json"
    return repo, fname
 
RAW_URL_TEMPLATE = "https://raw.githubusercontent.com/{org}/{repo}/main/{fname}"
 
# Second source: season records (record/PPG/PAPG/win%/point diff).
# Same DATA_REPO for every year -- only the filename changes.
DATA_REPO = "All_MO_Sports-Data"
RECORDS_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/{org}/{repo}/main/"
    "output/mshsaa_historical_records/football/football_{year}.json"
)
 
# Source of truth for slugs (your live schools.json feed) and the
# co-op / renamed-school alias table already used by add_slugs.py.
SCHOOLS_JSON_PATH = "output/schools.json"   # local path or raw.githubusercontent.com URL
ALIASES_JSON_PATH = "Aliases.json"          # local path or URL; optional
OUTPUT_PATH = "football_history.json"
 
 
# ---------- SLUG HELPERS ----------
def make_slug(name: str) -> str:
    """Fallback slug generator, only used when a school name can't be
    matched against schools.json. Mirrors the fixed make_slug() pattern
    (strips punctuation including parentheses) so it's at least internally
    consistent even if it doesn't line up with the canonical slug."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "-", name.strip())
    return name
 
 
def load_json(path_or_url: str):
    if path_or_url.startswith("http"):
        with urllib.request.urlopen(path_or_url) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(path_or_url, "r", encoding="utf-8") as f:
        return json.load(f)
 
 
def build_name_to_slug_map(schools_json_path: str, aliases_json_path: str):
    """Build normalized-name -> slug lookup from schools.json.
 
    Handles either shape:
      A) {"schools": [{"name": "...", "slug": "..."}, ...]}  (list of records)
      B) {"<slug>": {"name": "...", ...}, ...}               (dict keyed by slug)
    """
    schools = load_json(schools_json_path)
    name_to_slug = {}
 
    if isinstance(schools, dict) and isinstance(schools.get("schools"), list):
        # Shape A
        for entry in schools["schools"]:
            nm = entry.get("name") or entry.get("school")
            slug = entry.get("slug")
            if nm and slug:
                name_to_slug[nm.strip().lower()] = slug
    elif isinstance(schools, dict):
        # Shape B: dict keyed by slug
        for slug, entry in schools.items():
            if not isinstance(entry, dict):
                continue
            nm = entry.get("name") or entry.get("school")
            if nm:
                name_to_slug[nm.strip().lower()] = slug
    elif isinstance(schools, list):
        # Bare list of records, no "schools" wrapper
        for entry in schools:
            nm = entry.get("name") or entry.get("school")
            slug = entry.get("slug")
            if nm and slug:
                name_to_slug[nm.strip().lower()] = slug
 
    aliases = {}
    if aliases_json_path and Path(aliases_json_path).exists():
        aliases = load_json(aliases_json_path)  # {"Old/Co-op Name": "Canonical Name"}
 
    return name_to_slug, aliases
 
 
def resolve_slug(school_name: str, name_to_slug: dict, aliases: dict):
    key = school_name.strip().lower()
    if key in name_to_slug:
        return name_to_slug[key]
 
    canonical = aliases.get(school_name) or aliases.get(school_name.strip())
    if canonical:
        canon_key = canonical.strip().lower()
        if canon_key in name_to_slug:
            return name_to_slug[canon_key]
 
    return None  # caller falls back to a derived slug
 
 
# ---------- MAIN AGGREGATION ----------
def build_records_lookup(records_data: dict):
    """Index one year's records file by lowercased school name for merging."""
    lookup = {}
    for team in records_data.get("teams", []):
        nm = team.get("school")
        if nm:
            lookup[nm.strip().lower()] = team
    return lookup
 
 
def main():
    name_to_slug, aliases = build_name_to_slug_map(SCHOOLS_JSON_PATH, ALIASES_JSON_PATH)
 
    history = {}
    unmatched = set()
    unmatched_records = set()
    fetched_years = []
 
    for year in YEARS:
        repo, fname = repo_and_file_for_year(year)
        ratings_url = RAW_URL_TEMPLATE.format(org=ORG, repo=repo, fname=fname)
        records_url = RECORDS_URL_TEMPLATE.format(org=ORG, repo=DATA_REPO, year=year)
 
        print(f"Fetching {year} ratings: {ratings_url}")
        try:
            ratings_data = load_json(ratings_url)
        except Exception as e:
            print(f"  !! Skipping {year} (ratings fetch failed): {e}")
            continue
 
        print(f"Fetching {year} records: {records_url}")
        records_lookup = {}
        try:
            records_data = load_json(records_url)
            records_lookup = build_records_lookup(records_data)
        except Exception as e:
            print(f"  !! No records data for {year} (continuing with ratings only): {e}")
 
        fetched_years.append(year)
        for team in ratings_data.get("teams", []):
            school_name = team.get("school")
            if not school_name:
                continue
 
            slug = resolve_slug(school_name, name_to_slug, aliases)
            if not slug:
                unmatched.add(school_name)
                slug = make_slug(school_name)
 
            record = {
                "ovr_rank": team.get("ovr_rank"),
                "classification": team.get("classification"),
                "district": team.get("district"),
                "ovr_rating": team.get("ovr_rating"),
                "off_rating": team.get("off_rating"),
                "off_rank": team.get("off_rank"),
                "def_rating": team.get("def_rating"),
                "def_rank": team.get("def_rank"),
            }
 
            rec_match = records_lookup.get(school_name.strip().lower())
            if rec_match:
                points_for = rec_match.get("points_for")
                points_against = rec_match.get("points_against")
                point_diff = None
                if points_for is not None and points_against is not None:
                    point_diff = points_for - points_against
 
                # win_pct is computed from wins/losses rather than trusted
                # from the source, since MSHSAA's 2010-2011 pages return a
                # literal 0 in that column for ~95% of teams (confirmed bug
                # in the scraped data -- wins/losses/points parse correctly
                # for those years, only win_pct is broken). Deriving it
                # ourselves sidesteps that inconsistency for every year,
                # not just the two known-bad ones.
                wins = rec_match.get("wins")
                losses = rec_match.get("losses")
                win_pct = None
                if wins is not None and losses is not None and (wins + losses) > 0:
                    win_pct = round((wins / (wins + losses)) * 100, 2)
 
                record.update({
                    "wins": wins,
                    "losses": losses,
                    "win_pct": win_pct,
                    "games_played": rec_match.get("games_played"),
                    "ppg": rec_match.get("ppg"),
                    "papg": rec_match.get("oppg"),   # oppg = opponent PPG = PAPG
                    "points_for": points_for,
                    "points_against": points_against,
                    "point_diff": point_diff,
                    "mov": rec_match.get("mov"),
                })
            else:
                unmatched_records.add(f"{school_name} ({year})")
 
            bucket = history.setdefault(slug, {"school": school_name, "history": {}})
            bucket["history"][str(year)] = record
 
    output = {
        "sport": "football",
        "years": fetched_years,
        "teams": history,
    }
 
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
 
    print(f"\nWrote {OUTPUT_PATH}: {len(history)} teams across years {fetched_years}")
    if unmatched:
        print(f"\n{len(unmatched)} school names had no slug match in schools.json "
              f"(fell back to a derived slug — check these against Aliases.json):")
        for n in sorted(unmatched):
            print(f"  - {n}")
    if unmatched_records:
        print(f"\n{len(unmatched_records)} team-years had ratings but no matching "
              f"records entry (record/PPG/PAPG will be missing for these — usually "
              f"a name-spelling mismatch between the two sources):")
        for n in sorted(unmatched_records):
            print(f"  - {n}")
 
 
if __name__ == "__main__":
    main()
