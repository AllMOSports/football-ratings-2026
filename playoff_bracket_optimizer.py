"""
All MO Sports - travel-limited football playoff bracket optimizer.

For each class it:
  1. Geocodes every school (U.S. Census batch geocoder; ZIP-center fallback).
  2. Pulls a full drive-time table from OSRM in one request per class.
  3. Seeds teams by Overall Rating + head-to-head jump rule.
  4. Groups teams into 8 districts so that EVERY possible matchup meets the
     travel limits (option b), or, where that's impossible, breaks as few
     limits as possible and reports each one.
  5. Places teams on a 64-line bracket: top seeds get byes, the rest play
     best-vs-worst.

Rules
  * Rounds: District QF, District SF, District Championship, Quarterfinal,
    Semifinal, Championship.
  * Travel limits: any two teams in the same district <= 2.5 h; any two teams
    in districts that meet in the Quarterfinal <= 4 h; no limit after that.
  * Seeds: sort by ovr_rating. Then, working from the top seed down, a team
    within 5 spots below that beat a team head-to-head jumps directly above
    it. A team can jump at most once.
  * Byes: seeds 1..(64 - number of teams) get a first-round bye.

Inputs (repo root)
  football_ratings_2026.json, football_games_2026_all.json,
  football_school_addresses.csv  (team,class,district,street,city,state,zip,zip_lat,zip_lon)
  coord_overrides.json (optional) {"Team": [lat, lon]}

Outputs
  school_coords.json            geocode cache (delete a team's entry to redo it)
  drive_times/class_N.json      drive-time cache (seconds)
  playoff_brackets/class_N.json and class_N.md, playoff_brackets/README.md

Usage
  python playoff_bracket_optimizer.py --classes 1,2,3,4,5,6 --factor 1.0 --time-limit 60
  --factor scales OSRM drive times before checking limits (e.g. 0.85 if OSRM
  runs slow vs. real-world times). Set OSRM_URL to use your own OSRM server.
"""
import argparse, csv, io, itertools, json, math, os, sys, time

import requests
from ortools.sat.python import cp_model

DIST_LIM_H, QF_LIM_H = 2.5, 4.0
OSRM_URL = os.environ.get('OSRM_URL', 'https://router.project-osrm.org').rstrip('/')
CENSUS_BATCH = 'https://geocoding.geo.census.gov/geocoder/locations/addressbatch'
QF_PAIRS = [(0, 1), (2, 3), (4, 5), (6, 7)]
ROUND_NAMES = ['District Quarterfinal', 'District Semifinal', 'District Championship',
               'Quarterfinal', 'Semifinal', 'Championship']


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- data
def load_json(p, default=None):
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def save_json(p, obj):
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    with open(p, 'w') as f:
        json.dump(obj, f, indent=1)


def load_addresses(path):
    with open(path, newline='') as f:
        return {r['team']: r for r in csv.DictReader(f)}


# --------------------------------------------------------------------------- geocoding
def geocode(teams, addresses, cache_path='school_coords.json'):
    cache = load_json(cache_path, {})
    overrides = load_json('coord_overrides.json', {})
    for t, (lat, lon) in overrides.items():
        cache[t] = {'lat': lat, 'lon': lon, 'source': 'override'}
    need = [t for t in teams if t not in cache]
    if need:
        log(f'Geocoding {len(need)} schools with the Census batch geocoder...')
        buf = io.StringIO()
        w = csv.writer(buf)
        ids = {}
        for i, t in enumerate(need):
            a = addresses[t]
            ids[str(i)] = t
            w.writerow([i, a['street'], a['city'], a.get('state') or 'MO', a['zip']])
        res_text = ''
        for attempt in range(3):
            try:
                r = requests.post(CENSUS_BATCH, files={'addressFile': ('addr.csv', buf.getvalue(), 'text/csv')},
                                  data={'benchmark': 'Public_AR_Current'}, timeout=300)
                r.raise_for_status()
                res_text = r.text
                break
            except Exception as e:
                log(f'  Census geocoder attempt {attempt + 1} failed: {e}')
                time.sleep(10)
        for row in csv.reader(io.StringIO(res_text)):
            if len(row) >= 6 and row[0] in ids and row[2] == 'Match' and row[5]:
                lon, lat = map(float, row[5].split(','))
                cache[ids[row[0]]] = {'lat': lat, 'lon': lon, 'source': 'census', 'matched': row[4]}
        for t in need:
            if t not in cache:
                a = addresses[t]
                cache[t] = {'lat': float(a['zip_lat']), 'lon': float(a['zip_lon']), 'source': 'zip_center'}
                log(f'  {t}: no street match, using ZIP {a["zip"]} center')
        save_json(cache_path, cache)
    return {t: cache[t] for t in teams}


# --------------------------------------------------------------------------- drive times
def drive_matrix(cls, teams, coords):
    path = f'drive_times/class_{cls}.json'
    key = [[t, round(coords[t]['lat'], 5), round(coords[t]['lon'], 5)] for t in teams]
    cached = load_json(path)
    if cached and cached.get('key') == key:
        return cached['seconds']
    log(f'Class {cls}: requesting {len(teams)}x{len(teams)} drive-time table from OSRM...')
    pts = ';'.join(f"{coords[t]['lon']:.5f},{coords[t]['lat']:.5f}" for t in teams)
    url = f'{OSRM_URL}/table/v1/driving/{pts}?annotations=duration'
    for attempt in range(6):
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 429:
                raise RuntimeError('rate limited (429)')
            r.raise_for_status()
            js = r.json()
            if js.get('code') != 'Ok':
                raise RuntimeError(js.get('message', js.get('code')))
            dur = js['durations']
            break
        except Exception as e:
            wait = 15 * (attempt + 1)
            log(f'  OSRM attempt {attempt + 1} failed ({e}); retrying in {wait}s')
            time.sleep(wait)
    else:
        sys.exit(f'Could not get drive times for class {cls} from {OSRM_URL}')
    sec = {}
    for i, a in enumerate(teams):
        for j, b in enumerate(teams):
            if i != j:
                if dur[i][j] is None:
                    sys.exit(f'OSRM found no route between {a} and {b}; check coordinates')
                sec[a + '|' + b] = dur[i][j]
    save_json(path, {'key': key, 'seconds': sec})
    time.sleep(2)  # be polite to the public server
    return sec


# --------------------------------------------------------------------------- seeding
def make_seeds(cls, ratings, games):
    teams = sorted([t for t in ratings if t['classification'] == cls], key=lambda t: -t['ovr_rating'])
    order = [t['school'] for t in teams]
    wins, h2h = {}, {}
    for g in games:
        if g.get('score1') is None or g.get('score2') is None or g['score1'] == g['score2']:
            continue
        w, l = (g['team1'], g['team2']) if g['score1'] > g['score2'] else (g['team2'], g['team1'])
        wins[(w, l)] = wins.get((w, l), 0) + 1
        h2h.setdefault((w, l), []).append(
            f"{g['date']}: {g['team1']} {g['score1']}, {g['team2']} {g['score2']}" + (' (forfeit)' if g.get('forfeit') else ''))
    beat = lambda a, b: wins.get((a, b), 0) > wins.get((b, a), 0)
    seeds, moved, jumps, p = order[:], set(), [], 0
    while p < len(seeds):
        top = seeds[p]
        cand = [q for q in range(p + 1, min(p + 6, len(seeds))) if seeds[q] not in moved and beat(seeds[q], top)]
        if cand:
            q = cand[0]  # highest-rated eligible team jumps first
            j = seeds.pop(q)
            seeds.insert(p, j)
            moved.add(j)
            jumps.append({'team': j, 'jumped': top, 'from_seed': q + 1, 'to_seed': p + 1, 'result': h2h[(j, top)]})
            continue  # the jumped team (now one spot lower) can still be jumped by someone else
        p += 1
    rating = {t['school']: t['ovr_rating'] for t in teams}
    return seeds, order, rating, jumps


# --------------------------------------------------------------------------- bracket
def order64(n=64):
    o = [1]
    while len(o) < n:
        m = len(o) * 2 + 1
        o = [x for s in o for x in (s, m - s)]
    return o


SLOT = order64()  # SLOT[line] = ideal seed on that bracket line


def optimize_districts(seeds, tt, time_limit, bye_rule='statewide'):
    """Assign teams to 8 districts in two stages:
       Stage 1: fewest travel violations (each over-limit pair that could meet counts once).
       Stage 2: holding that count, keep each team near its ideal seed line.
    A district with s teams holds exactly 8-s bye teams, so byes always go to the top seeds."""
    N = len(seeds)
    byes = 64 - N
    seed = {t: i + 1 for i, t in enumerate(seeds)}
    wt = {t: 1 + (N - seed[t]) / N for t in seeds}

    def build():
        m = cp_model.CpModel()
        y = {(t, d): m.NewBoolVar(f'y{i}_{d}') for i, t in enumerate(seeds) for d in range(8)}
        for t in seeds:
            m.AddExactlyOne(y[t, d] for d in range(8))
        m.Add(y[seeds[0], 0] == 1)  # symmetry breaking: #1 seed in District 1
        for d in range(8):
            size = sum(y[t, d] for t in seeds)
            m.Add(size >= 4)
            m.Add(size <= 8)
            if bye_rule == 'statewide':  # byes = statewide top seeds
                m.Add(sum(y[t, d] for t in seeds if seed[t] <= byes) == 8 - size)
        viol = []
        for a, b in itertools.combinations(seeds, 2):
            h = tt(a, b)
            if h > DIST_LIM_H:
                v = m.NewBoolVar('')
                for d in range(8):
                    m.Add(y[a, d] + y[b, d] <= 1 + v)
                viol.append((v, a, b, 'District rounds', h))
            if h > QF_LIM_H:
                v = m.NewBoolVar('')
                for d1, d2 in QF_PAIRS:
                    m.Add(y[a, d1] + y[b, d2] <= 1 + v)
                    m.Add(y[b, d1] + y[a, d2] <= 1 + v)
                viol.append((v, a, b, 'Quarterfinal', h))
        proxy = sum(int(round(100 * wt[t])) * min(abs(seed[t] - SLOT[k]) for k in range(8 * d, 8 * d + 8)) * y[t, d]
                    for t in seeds for d in range(8))
        return m, y, viol, proxy

    def run(m, secs):
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = secs
        s.parameters.num_workers = 8
        s.parameters.random_seed = 1
        st = s.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            sys.exit('Solver found no bracket at all')
        return s, st

    # Stage 1
    m, y, viol, proxy = build()
    m.Minimize(sum(v for v, *_ in viol))
    s1, st1 = run(m, time_limit * 0.6)
    best = int(round(s1.ObjectiveValue()))
    proven = int(round(s1.BestObjectiveBound())) >= best
    hint = {k: s1.Value(v) for k, v in y.items()}
    # Stage 2
    m, y, viol, proxy = build()
    m.Add(sum(v for v, *_ in viol) <= best)
    for k, v in y.items():
        m.AddHint(v, hint[k])
    m.Minimize(proxy)
    s, st = run(m, time_limit * 0.4)
    districts = {d: sorted([t for t in seeds if s.Value(y[t, d])], key=lambda t: seed[t]) for d in range(8)}
    violations = [{'team_a': a, 'team_b': b, 'round': r, 'hours': round(h, 2)} for v, a, b, r, h in viol if s.Value(v)]
    info = {'solver_status': f'stage1 {s1.StatusName(st1)}, stage2 {s.StatusName(st)}',
            'violations': len(violations),
            'violations_proven_minimum': proven,
            'solve_seconds': round(s1.WallTime() + s.WallTime(), 1)}
    return districts, violations, info


def place_lines(districts, seed, byes, bye_rule='statewide'):
    """Within each district: bye teams take the best seed lines; remaining
    teams play best-vs-worst in the District Quarterfinal.
    statewide: byes go to seeds 1..byes.  district: a district with s teams gives
    byes to its top 8-s seeds."""
    line = {}
    for d, mem in districts.items():
        pairs = sorted([(k, k + 1) if SLOT[k] < SLOT[k + 1] else (k + 1, k) for k in range(8 * d, 8 * d + 8, 2)],
                       key=lambda p: SLOT[p[0]])
        if bye_rule == 'statewide':
            bye_t = [t for t in mem if seed[t] <= byes]
        else:
            bye_t = mem[:8 - len(mem)]
        play = [t for t in mem if t not in bye_t]
        for t, p in zip(bye_t, pairs):
            line[p[0]] = t
        for i, p in enumerate(pairs[len(bye_t):]):
            line[p[0]] = play[i]
            line[p[1]] = play[-1 - i]
    return line


# --------------------------------------------------------------------------- reporting
def fmt_h(h):
    mins = int(round(h * 60))
    return f'{mins // 60}h {mins % 60:02d}m'


def write_outputs(cls, seeds, order, rating, jumps, districts, line, violations, info, tt, factor, bye_rule):
    seed = {t: i + 1 for i, t in enumerate(seeds)}
    N = len(seeds)
    byes = 64 - N
    out = {'class': cls, 'teams': N, 'byes': byes, 'bye_rule': bye_rule, 'drive_time_factor': factor, 'limits_hours':
           {'district_rounds': DIST_LIM_H, 'quarterfinal': QF_LIM_H}, **info,
           'seeds': [{'seed': seed[t], 'team': t, 'ovr_rating': rating[t], 'rating_rank': order.index(t) + 1} for t in seeds],
           'jumps': jumps,
           'districts': [], 'quarterfinals': [], 'lines': [], 'violations': violations}
    md = [f'# Class {cls} travel-limited bracket', '',
          f'{N} teams, {byes} first-round byes ({bye_rule} bye rule). Drive-time factor {factor}. '
          f'Limits: district rounds {DIST_LIM_H} h, quarterfinal {QF_LIM_H} h (every possible matchup).', '',
          f'**Travel-limit violations: {len(violations)}**' +
          (' (proven minimum)' if info['violations_proven_minimum'] else ' (best found in time limit)'), '']
    if jumps:
        md += ['## Head-to-head jumps', '']
        md += [f"- **{j['team']}** jumped {j['jumped']} (seed {j['from_seed']} to {j['to_seed']}): {'; '.join(j['result'])}" for j in jumps]
        md += ['']
    for d in range(8):
        mem = districts[d]
        mx = max((tt(a, b) for a, b in itertools.combinations(mem, 2)), default=0)
        games, byelist = [], []
        for k in range(8 * d, 8 * d + 8, 2):
            a, b = line.get(k), line.get(k + 1)
            if a and b:
                hi, lo = sorted((a, b), key=lambda t: seed[t])
                games.append({'home': hi, 'away': lo, 'drive_h': round(tt(a, b), 2)})
            elif a or b:
                byelist.append(a or b)
        out['districts'].append({'district': d + 1, 'teams': [{'seed': seed[t], 'team': t} for t in mem],
                                 'byes': byelist, 'dqf_games': games, 'max_drive_h': round(mx, 2)})
        md += [f'## District {d + 1}  (longest possible trip {fmt_h(mx)})', '',
               '| Line | Seed | Team | First round |', '|---|---|---|---|']
        for k in range(8 * d, 8 * d + 8):
            t = line.get(k)
            if not t:
                continue
            opp = line.get(k ^ 1)
            fr = 'Bye' if not opp else (f'vs #{seed[opp]} {opp} ({fmt_h(tt(t, opp))})')
            md.append(f'| {k + 1} | {seed[t]} | {t} | {fr} |')
        md.append('')
    md += ['## Quarterfinal pairings', '']
    for d1, d2 in QF_PAIRS:
        mx = max(tt(a, b) for a in districts[d1] for b in districts[d2])
        out['quarterfinals'].append({'districts': [d1 + 1, d2 + 1], 'max_drive_h': round(mx, 2)})
        md.append(f'- District {d1 + 1} champion vs District {d2 + 1} champion (longest possible trip {fmt_h(mx)})')
    md += ['- Semifinals: QF winners (D1/D2 vs D3/D4) and (D5/D6 vs D7/D8); no travel limit', '']
    if violations:
        md += ['## Possible matchups over the limit', '', '| Round | Team | Team | Drive |', '|---|---|---|---|']
        md += [f"| {v['round']} | {v['team_a']} | {v['team_b']} | {fmt_h(v['hours'])} |"
               for v in sorted(violations, key=lambda v: -v['hours'])]
        md.append('')
    md += ['## Seeds', '', '| Seed | Team | OVR | Rating rank |', '|---|---|---|---|']
    md += [f'| {seed[t]} | {t} | {rating[t]} | {order.index(t) + 1} |' for t in seeds]
    out['lines'] = [{'line': k + 1, 'ideal_seed': SLOT[k], 'team': line.get(k),
                     'seed': seed.get(line.get(k))} for k in range(64)]
    save_json(f'playoff_brackets/class_{cls}.json', out)
    with open(f'playoff_brackets/class_{cls}.md', 'w') as f:
        f.write('\n'.join(md) + '\n')
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--classes', default='1,2,3,4,5,6')
    ap.add_argument('--factor', type=float, default=1.0)
    ap.add_argument('--time-limit', type=float, default=60)
    ap.add_argument('--addresses', default='football_school_addresses.csv')
    ap.add_argument('--bye-rule', choices=['statewide', 'district'], default='statewide',
                    help="statewide: seeds 1..(64-N) get byes. district: each district's top seeds get its byes "
                         "(far fewer travel violations)")
    args = ap.parse_args()

    ratings = load_json('football_ratings_2026.json')['teams']
    games = load_json('football_games_2026_all.json')
    addresses = load_addresses(args.addresses)
    classes = [int(c) for c in args.classes.split(',') if c.strip()]

    summary = ['# Travel-limited playoff brackets', '',
               f'Bye rule: {args.bye_rule}. Drive-time factor {args.factor}; limits: district rounds {DIST_LIM_H} h, quarterfinal {QF_LIM_H} h.', '',
               '| Class | Teams | Byes | Violations | Proven minimum? |', '|---|---|---|---|---|']
    for cls in classes:
        seeds, order, rating, jumps = make_seeds(cls, ratings, games)
        missing = [t for t in seeds if t not in addresses]
        if missing:
            sys.exit(f'Class {cls}: no address for {missing}; add them to {args.addresses}')
        coords = geocode(seeds, addresses)
        sec = drive_matrix(cls, seeds, coords)
        tt = lambda a, b: max(sec[a + '|' + b], sec[b + '|' + a]) * args.factor / 3600  # slower direction, hours
        log(f'Class {cls}: optimizing districts ({len(seeds)} teams, up to {args.time_limit:.0f}s)...')
        districts, violations, info = optimize_districts(seeds, tt, args.time_limit, args.bye_rule)
        seed = {t: i + 1 for i, t in enumerate(seeds)}
        line = place_lines(districts, seed, 64 - len(seeds), args.bye_rule)
        write_outputs(cls, seeds, order, rating, jumps, districts, line, violations, info, tt, args.factor, args.bye_rule)
        log(f"Class {cls}: {len(violations)} violations ({info['solver_status']}, {info['solve_seconds']}s)")
        summary.append(f"| {cls} | {len(seeds)} | {64 - len(seeds)} | {len(violations)} | "
                       f"{'Yes' if info['violations_proven_minimum'] else 'No'} |")
    with open('playoff_brackets/README.md', 'w') as f:
        f.write('\n'.join(summary) + '\n')
    log('Done. See playoff_brackets/.')


if __name__ == '__main__':
    main()
