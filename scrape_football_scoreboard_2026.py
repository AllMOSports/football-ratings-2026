"""
MSHSAA Football Scoreboard Scraper - 2026 Season
===================================================
 
Scrapes https://www.mshsaa.org/Activities/Scoreboard.aspx for both
11-man (alg=19) and 8-man (alg=21) football across every Thursday,
Friday, and Saturday of the 2026 season (Aug 1 - Dec 31, 2026).
 
IMPORTANT - READ BEFORE RUNNING
--------------------------------
I was not able to fetch the live MSHSAA scoreboard page to inspect its
actual rendered HTML (their robots.txt blocks automated fetching from
my sandbox, and the sandbox can't reach mshsaa.org or download Playwright
browser binaries anyway). The page is an ASP.NET UpdatePanel that shows
"Processing... Still working" during an async postback, so it requires
a real headless browser (Playwright) rather than plain requests+bs4 -
this matches the approach your existing nightly rankings.json scraper
already uses.
 
Because I couldn't verify the exact CSS selectors/classes MSHSAA uses
for each game row, this script ships with an --inspect mode. Run that
FIRST against one known date to dump the rendered HTML, find the real
selectors in ~30 seconds, then update the SELECTORS block below.
 
    python3 scrape_football_scoreboard_2026.py --inspect --date 09042026 --alg 19
 
That will save the fully-rendered HTML to ./debug_html/ and also print
a best-guess list of candidate game containers it found, ranked by how
"table-row-like" they look, to speed up finding the right selector.
 
Usage
-----
    # one-time setup
    pip install playwright beautifulsoup4 --break-system-packages
    playwright install chromium --with-deps
 
    # inspect real markup once, then edit SELECTORS below
    python3 scrape_football_scoreboard_2026.py --inspect --date 09042026 --alg 19
 
    # full season scrape after selectors are confirmed
    python3 scrape_football_scoreboard_2026.py --season 2026
 
Output
------
    football_scoreboard_2026.json  - list of game dicts
    debug_html/<alg>_<date>.html   - raw rendered HTML (--inspect or --keep-html)
"""
 
import argparse
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path
 
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
 
# ---------------------------------------------------------------------------
# CONFIG - adjust ALG codes / selectors here after running --inspect
# ---------------------------------------------------------------------------
 
ALGS = {
    "11man": 19,
    "8man": 21,
}
 
BASE_URL = "https://www.mshsaa.org/Activities/Scoreboard.aspx"
 
# NOTE: These are best-guess placeholders based on typical ASP.NET
# scoreboard markup patterns (and your ActivityHistory.aspx scraper's
# use of icon-class parsing). Confirm/replace with real selectors from
# --inspect output before trusting the full scrape.
SELECTORS = {
    # Container that wraps a single game (guess: adjust after --inspect)
    "game_container": ".scoreboard-game, .sbGame, tr.game-row, div[id*='Game']",
    # Team name elements within a game container
    "team_name": ".team-name, .teamName, td.team, span[id*='Team']",
    # Score elements within a game container
    "team_score": ".team-score, .score, td.score, span[id*='Score']",
    # Optional: game status/time (Final, postponed, etc.)
    "status": ".game-status, .status",
}
 
# Selector to wait for before considering the async postback "done".
# The page shows a "Processing... Still working" indicator during the
# postback - adjust this to whatever element actually holds results.
RESULTS_READY_SELECTOR = "#ctl00_ContentPlaceHolder1_pnlResults, .scoreboard-container, table"
 
NAV_TIMEOUT_MS = 30_000
POSTBACK_WAIT_MS = 4_000  # extra settle time after networkidle, ASP.NET postbacks are sneaky
 
DEBUG_DIR = Path("debug_html")
 
 
# ---------------------------------------------------------------------------
# Date generation
# ---------------------------------------------------------------------------
 
def season_thu_fri_sat(year: int, start_month=8, start_day=1, end_month=12, end_day=31):
    """Yield every Thursday/Friday/Saturday between the given range (inclusive)."""
    d = date(year, start_month, start_day)
    end = date(year, end_month, end_day)
    while d <= end:
        if d.weekday() in (3, 4, 5):  # Mon=0 ... Thu=3, Fri=4, Sat=5
            yield d
        d += timedelta(days=1)
 
 
def fmt_date(d: date) -> str:
    """MSHSAA date param format: MMDDYYYY"""
    return d.strftime("%m%d%Y")
 
 
# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
 
def fetch_rendered_html(page, alg: int, date_str: str) -> str:
    url = f"{BASE_URL}?alg={alg}&date={date_str}"
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass  # some pages keep long-polling; don't hard-fail on this
    # ASP.NET UpdatePanel postbacks can finish network-wise before the DOM
    # actually swaps in results, so give it a beat.
    page.wait_for_timeout(POSTBACK_WAIT_MS)
    try:
        page.wait_for_selector(RESULTS_READY_SELECTOR, timeout=5_000)
    except Exception:
        pass
    return page.content()
 
 
def parse_games(html: str, sport_key: str, game_date: date) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    games = []
 
    containers = soup.select(SELECTORS["game_container"])
    for c in containers:
        teams = [t.get_text(strip=True) for t in c.select(SELECTORS["team_name"])]
        scores = [s.get_text(strip=True) for s in c.select(SELECTORS["team_score"])]
        status_el = c.select_one(SELECTORS["status"])
        status = status_el.get_text(strip=True) if status_el else None
 
        if len(teams) < 2:
            continue  # container didn't match a real game - selector likely needs fixing
 
        game = {
            "date": game_date.isoformat(),
            "sport": sport_key,
            "away_team": teams[0] if len(teams) > 0 else None,
            "home_team": teams[1] if len(teams) > 1 else None,
            "away_score": scores[0] if len(scores) > 0 else None,
            "home_score": scores[1] if len(scores) > 1 else None,
            "status": status,
        }
        games.append(game)
 
    return games
 
 
def inspect_page(html: str, alg: int, date_str: str):
    """Dump HTML and print candidate game-row structures to help find real selectors."""
    DEBUG_DIR.mkdir(exist_ok=True)
    out_path = DEBUG_DIR / f"alg{alg}_{date_str}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Saved rendered HTML -> {out_path}")
 
    soup = BeautifulSoup(html, "html.parser")
 
    # Heuristic: find elements whose class/id mentions game/team/score,
    # and any <table> elements, since ASP.NET scoreboards are often tables.
    print("\n--- Candidate elements (class/id contains game|team|score) ---")
    pattern = re.compile(r"(game|team|score|matchup)", re.I)
    seen = set()
    count = 0
    for el in soup.find_all(True):
        attrs = " ".join(el.get("class", []) or []) + " " + (el.get("id") or "")
        if pattern.search(attrs):
            key = (el.name, attrs.strip())
            if key in seen or not attrs.strip():
                continue
            seen.add(key)
            print(f"  <{el.name}> class/id='{attrs.strip()}'")
            count += 1
            if count >= 40:
                print("  ... (truncated, see saved HTML for full page)")
                break
 
    print("\n--- <table> elements found ---")
    tables = soup.find_all("table")
    print(f"  {len(tables)} table(s) found")
    for i, t in enumerate(tables[:5]):
        rows = t.find_all("tr")
        print(f"  table[{i}]: id='{t.get('id')}' class='{t.get('class')}' rows={len(rows)}")
 
    if count == 0 and not tables:
        print("\n  No obvious candidates found. The page may render results into")
        print("  an iframe, or need a longer wait, or a different URL/query param.")
        print("  Open the saved HTML file in a browser or text editor to inspect manually.")
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def run_season_scrape(season_year: int, algs: dict, out_path: Path, keep_html: bool, delay_s: float):
    all_games = []
    dates = list(season_thu_fri_sat(season_year))
    print(f"Scanning {len(dates)} Thu/Fri/Sat dates in {season_year} x {len(algs)} scoreboard(s) "
          f"= {len(dates) * len(algs)} page fetches.")
 
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
 
        for sport_key, alg in algs.items():
            for d in dates:
                date_str = fmt_date(d)
                try:
                    html = fetch_rendered_html(page, alg, date_str)
                except Exception as e:
                    print(f"  [{sport_key} {date_str}] FAILED to load: {e}")
                    continue
 
                if keep_html:
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / f"alg{alg}_{date_str}.html").write_text(html, encoding="utf-8")
 
                games = parse_games(html, sport_key, d)
                if games:
                    print(f"  [{sport_key} {date_str}] found {len(games)} game(s)")
                    all_games.extend(games)
 
                time.sleep(delay_s)  # be polite to MSHSAA's servers
 
        browser.close()
 
    out_path.write_text(json.dumps(all_games, indent=2), encoding="utf-8")
    print(f"\nDone. {len(all_games)} total games saved -> {out_path}")
    if not all_games:
        print("\nWARNING: zero games parsed across the whole run. Almost certainly means")
        print("SELECTORS at the top of this script don't match the real markup yet.")
        print("Run with --inspect --date <MMDDYYYY> --alg 19 on a known game date to fix them.")
 
 
def main():
    ap = argparse.ArgumentParser(description="Scrape MSHSAA football scoreboard.")
    ap.add_argument("--season", type=int, default=2026, help="Season year (default 2026)")
    ap.add_argument("--out", default="football_scoreboard_2026.json", help="Output JSON path")
    ap.add_argument("--keep-html", action="store_true", help="Save raw HTML for every date fetched")
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds to sleep between requests")
    ap.add_argument("--inspect", action="store_true",
                     help="Fetch a single date and dump HTML + candidate selectors, then exit")
    ap.add_argument("--date", help="MMDDYYYY - required with --inspect, or to scrape a single date")
    ap.add_argument("--alg", type=int, default=19, help="alg code: 19=11-man, 21=8-man (default 19)")
    args = ap.parse_args()
 
    if args.inspect:
        if not args.date:
            ap.error("--inspect requires --date MMDDYYYY")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            html = fetch_rendered_html(page, args.alg, args.date)
            browser.close()
        inspect_page(html, args.alg, args.date)
        return
 
    if args.date:
        # single-date scrape, useful for testing after fixing selectors
        d = date(int(args.date[4:8]), int(args.date[0:2]), int(args.date[2:4]))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            html = fetch_rendered_html(page, args.alg, args.date)
            browser.close()
        sport_key = next((k for k, v in ALGS.items() if v == args.alg), str(args.alg))
        games = parse_games(html, sport_key, d)
        print(json.dumps(games, indent=2))
        return
 
    run_season_scrape(args.season, ALGS, Path(args.out), args.keep_html, args.delay)
 
 
if __name__ == "__main__":
    main()
