"""
Matchup Recaps - data builder (v1)
==================================
Writes one small file per game date plus a manifest:
  matchup_recaps_2026/<YYYY-MM-DD>.json   {"date", "recaps": {id: recap}}
  matchup_recaps_2026/manifest.json       {"dates": {date: count}, ...}
The Matchup Recap page loads the one date file it needs; the Sport Detail
snippet loads only the manifest (a game gets a "Matchup Recap" button when its
date is listed). Files are rewritten only when their content changes, so a
run with nothing new makes no commit.

A RECAP IS WRITTEN ONCE AND THEN FROZEN. It describes the game the way it
looked that night: the projection fans saw before kickoff, both teams' ranks
and records right after it, and (playoff games) each team's odds before and
after. Later rating changes never rewrite it. The Matchup Recap page adds a
separate, live "Looking back" line computed from today's ratings.

WHERE EACH NUMBER COMES FROM (all from this repo's own git history, so no
extra snapshot step is needed and every 2026 game can be backfilled):
  pre-game projection  football_ratings_2026.json as committed BEFORE kickoff
                       (the last commit before 5:00 PM Central on game day),
                       run through the Matchup Predictor v8 calculation
                       (predictor_projection, imported from the previews
                       builder so the two can never drift apart).
                       None for a team without ratings (out-of-state teams,
                       and weeks 1-2 before the first ratings run).
  ranks after the game the FIRST ratings commit after game day ("post-game
                       ratings"). The recap waits for it - a game isn't
                       recapped until the ratings have been updated with its
                       result - unless WAIT_DAYS have passed, then it uses the
                       latest ratings.
  playoff odds         playoff_hub_2026.json: last commit before kickoff
                       (before) and first commit after game day (after).
                       Playoff games only, same WAIT_DAYS rule.
  records, streaks,    football_games_2026_all.json, games up to and including
  season highs         this one.

FREEZING: recaps already in matchup_recaps_2026.json are kept as they are.
Exceptions: --rebuild (everything), --rebuild-game <id>, and an automatic
rebuild when MSHSAA corrects the final score of a game already recapped.

HEADLINE: the first rule that fits, in this order -
  1. state championship winner      "[W] wins the Class N state championship."
  2. upset (winner < 35% pre-game)  "Upset: [W] knocks off [L] as a P% underdog."
  3. margin within 3 of projected   "Right on the number: [W] wins by N, exactly/almost exactly the projected margin."
  4. favorite won by 14+ less       "[W] survives: a S win that was closer than projected."
  5. favorite won by 14+ more       "[W] rolls past [L], N points better than projected."
  6. default                        "[W] beats [L], S."
STORY: up to four template sentences (what it means, the projection, how the
offenses/defenses did against it, streak / best win). No AI.

Needs full git history (actions/checkout fetch-depth: 0).

Usage:
    python3 build_matchup_recaps_2026.py
    python3 build_matchup_recaps_2026.py --rebuild
    python3 build_matchup_recaps_2026.py --rebuild-game 2026-11-20-christian-brothers-college-vs-jackson
    DATA_DIR=../All_MO_Sports-Data python3 build_matchup_recaps_2026.py
"""

import argparse
import datetime
import json
import os
import subprocess
from collections import defaultdict
from zoneinfo import ZoneInfo

import build_matchup_previews_2026 as mp
import simulate_district_seeds as sds

OUTPUT_DIR = "matchup_recaps_2026"
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "manifest.json")
RATINGS_PATH = mp.RATINGS_PATH            # football_ratings_2026.json
GAMES_PATH = mp.GAMES_PATH                # football_games_2026_all.json
HUB_PATH = mp.PLAYOFF_HUB_PATH            # playoff_hub_2026.json
SEASON = 2026
CENTRAL = ZoneInfo("America/Chicago")
KICKOFF_HOUR = 17          # ratings committed before 5:00 PM Central on game day count as pre-game
WAIT_DAYS = 7              # give up waiting for post-game ratings / hub after this many days
UPSET_PROB = 0.35          # winner's pre-game win probability below this = upset
ON_NUMBER_POINTS = 3       # actual margin within this of the projected margin
SWING_POINTS = 14          # "closer than projected" / "rolls past" threshold
PERF_POINTS = 5            # offense/defense sentence threshold (points vs projection)
MIN_STREAK = 3
MIN_PRIOR_GAMES = 3        # season-high notes need at least this many earlier games


# --------------------------------------------------------------------------
# git history
# --------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


class History:
    """Every committed version of one file, oldest first, loaded lazily."""

    def __init__(self, path):
        self.path = path
        self.versions = []           # [(datetime aware, sha)]
        for line in git("log", "--format=%H\t%cI", "--", path).splitlines():
            sha, stamp = line.split("\t")
            self.versions.append((datetime.datetime.fromisoformat(stamp), sha))
        self.versions.sort()
        self._cache = {}
        if not self.versions:
            raise SystemExit(f"No git history for {path} - run with full history (fetch-depth: 0).")

    def load(self, sha):
        if sha not in self._cache:
            self._cache[sha] = json.loads(git("show", f"{sha}:{self.path}"))
        return self._cache[sha]

    def before(self, when):
        """Last version committed before `when`, or None."""
        hit = None
        for t, sha in self.versions:
            if t < when:
                hit = (t, sha)
        return hit

    def after(self, when):
        """First version committed after `when`, or None."""
        for t, sha in self.versions:
            if t > when:
                return (t, sha)
        return None

    def latest(self):
        return self.versions[-1]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def iso(d):
    return sds.bdp._normalize_date(str(d))


def kickoff(date_str):
    d = datetime.date.fromisoformat(date_str)
    return datetime.datetime(d.year, d.month, d.day, KICKOFF_HOUR, tzinfo=CENTRAL)


def game_day_end(date_str):
    d = datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)
    return datetime.datetime(d.year, d.month, d.day, 0, tzinfo=CENTRAL)


def pct_text(p):
    """Same as the predictor's formatWinPct(): never a literal 100% or 0%."""
    if p >= 0.995:
        return ">99%"
    if p <= 0.005:
        return "<1%"
    return f"{round(p * 100)}%"


def a_an(pct_str):
    """'an 87%', 'an 8%', 'an 11%', 'an 18%', 'a 46%', 'a >99%'."""
    digits = pct_str.lstrip("<>")
    starts_vowel_sound = digits.startswith("8") or digits in ("11%", "18%") or digits.startswith("11") and len(digits) == 3
    return ("an " if starts_vowel_sound and pct_str[0].isdigit() else "a ") + pct_str


def ordinal_word(n):
    words = {2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth",
             9: "ninth", 10: "10th", 11: "11th", 12: "12th", 13: "13th", 14: "14th", 15: "15th"}
    return words.get(n, f"{n}th")


def playoff_round(date_str):
    """(label, index) for a playoff date, else (None, None)."""
    d = datetime.date.fromisoformat(date_str)
    if d <= sds.REGULAR_SEASON_END:
        return None, None
    best = None
    for i, (label, rdate, _week) in enumerate(mp.PLAYOFF_ROUNDS):
        if datetime.date.fromisoformat(rdate) <= d:
            best = (label, i)
    return best if best else (mp.PLAYOFF_ROUNDS[0][0], 0)


def ratings_maps(doc):
    """name -> team row, plus class rank / class size from that version."""
    teams = {t["school"]: t for t in doc.get("teams", [])}
    by_class = defaultdict(list)
    for t in doc.get("teams", []):
        if t.get("ovr_rating") is not None:
            by_class[t.get("classification")].append(t)
    class_rank, class_size = {}, {}
    for cls, ts in by_class.items():
        ts.sort(key=lambda t: -t["ovr_rating"])
        class_size[cls] = len(ts)
        for i, t in enumerate(ts, 1):
            class_rank[t["school"]] = i
    return teams, class_rank, class_size


def has_off_def(t):
    return t is not None and t.get("off_rating") is not None and t.get("def_rating") is not None


def hub_team(hub_doc, name):
    if not hub_doc:
        return None, None
    for c in hub_doc.get("classes", {}).values():
        for t in c.get("teams", []):
            if t.get("team") == name:
                return t, hub_doc.get("stages", [])
    return None, None


def odds_rows(before_doc, after_doc, name):
    """Stages that were still in doubt before the game: [{stage, before, after}]."""
    tb, stages_b = hub_team(before_doc, name)
    ta, stages_a = hub_team(after_doc, name)
    if not tb or not stages_b:
        return None
    after_by_stage = {}
    if ta and stages_a:
        after_by_stage = {s: ta["reach"][i] for i, s in enumerate(stages_a) if i < len(ta.get("reach", []))}
    rows = []
    for i, s in enumerate(stages_b):
        b = tb["reach"][i] if i < len(tb.get("reach", [])) else None
        if b is None or b >= 0.995 or b <= 0:
            continue
        a = after_by_stage.get(s, 0.0 if ta is None else None)
        rows.append({"stage": s, "before": round(b, 4), "after": None if a is None else round(a, 4)})
    return rows or None


# --------------------------------------------------------------------------
# per-team season context from the games file
# --------------------------------------------------------------------------

def team_logs(games):
    """name -> chronological list of played games (own perspective)."""
    out = defaultdict(list)
    for g in games:
        if g.get("score1") is None or g.get("score2") is None:
            continue
        d = iso(g["date"])
        for me, opp, ms, os_, cl in ((g["team1"], g["team2"], g["score1"], g["score2"], g.get("team1_classified", True)),
                                     (g["team2"], g["team1"], g["score2"], g["score1"], g.get("team2_classified", True))):
            out[me].append({"date": d, "opp": opp, "pf": ms, "pa": os_, "forfeit": g.get("forfeit", False),
                            "result": "W" if ms > os_ else ("L" if ms < os_ else "T"), "classified": cl})
    for v in out.values():
        v.sort(key=lambda x: x["date"])
    return out


def context_through(log, date_str, opp):
    """Record, streak and season highs for one team through this game."""
    upto = [x for x in log if x["date"] <= date_str]
    this = None
    for x in upto:
        if x["date"] == date_str and x["opp"] == opp:
            this = x
    prior = [x for x in upto if x is not this]
    w = sum(1 for x in upto if x["result"] == "W")
    l = sum(1 for x in upto if x["result"] == "L")
    t = sum(1 for x in upto if x["result"] == "T")
    streak_kind, streak = None, 0
    for x in reversed(upto):
        if streak_kind is None:
            streak_kind = x["result"]
        if x["result"] != streak_kind:
            break
        streak += 1
    return {"this": this, "prior": prior, "upto": upto, "wins": w, "losses": l, "ties": t,
            "record": f"{w}-{l}" + (f"-{t}" if t else ""), "streak_kind": streak_kind, "streak": streak}


def team_notes(name, ctx, post_teams, is_winner):
    """Frozen 'For the record' notes for one team."""
    notes = []
    this, prior = ctx["this"], ctx["prior"]
    if this is None:
        return notes
    if is_winner and ctx["streak_kind"] == "W" and ctx["streak"] >= MIN_STREAK:
        unbeaten = ctx["losses"] == 0 and ctx["ties"] == 0
        notes.append({"kind": "streak", "label": "Streak",
                      "text": f"{ordinal_word(ctx['streak']).capitalize()} straight win" + (" to open the season." if unbeaten else ".")})
    if (not is_winner) and ctx["streak_kind"] == "L" and ctx["streak"] >= MIN_STREAK:
        notes.append({"kind": "skid", "label": "Skid", "text": f"{ordinal_word(ctx['streak']).capitalize()} straight loss."})
    if (not is_winner) and this["result"] == "L" and ctx["losses"] == 1 and len(prior) >= MIN_PRIOR_GAMES:
        notes.append({"kind": "first_loss", "label": "First loss", "text": f"First loss of the season after a {len(prior)}-0 start."
                      if all(p["result"] == "W" for p in prior) else "First loss of the season."})
    if is_winner and this["result"] == "W":
        beaten = [x for x in ctx["upto"] if x["result"] == "W" and post_teams.get(x["opp"], {}).get("ovr_rating") is not None]
        if len(beaten) >= 2:
            best = max(beaten, key=lambda x: post_teams[x["opp"]]["ovr_rating"])
            if best is this:
                rank = post_teams[this["opp"]].get("ovr_rank")
                notes.append({"kind": "best_win", "label": "Best win",
                              "text": f"{this['opp']}" + (f" (#{rank} statewide)" if rank else "") +
                                      f" is the highest-rated team {name} has beaten this season so far."})
    if len(prior) >= MIN_PRIOR_GAMES and not this["forfeit"]:
        if this["pf"] > max(p["pf"] for p in prior):
            prev = max(prior, key=lambda p: p["pf"])
            notes.append({"kind": "most_scored", "label": "Season high",
                          "text": f"{this['pf']} points is {name}'s most this season so far (previous high: {prev['pf']} vs {prev['opp']})."})
        if this["pa"] > max(p["pa"] for p in prior):
            prev = max(prior, key=lambda p: p["pa"])
            notes.append({"kind": "most_allowed", "label": "Allowed",
                          "text": f"{this['pa']} points is the most {name} has allowed this season so far (previous high: {prev['pa']} vs {prev['opp']})."})
    return notes


# --------------------------------------------------------------------------
# headline + story (template sentences only)
# --------------------------------------------------------------------------

def write_headline(W, L, sw, sl, proj, rnd, cls):
    score = f"{sw}-{sl}"
    if rnd == "State championship":
        return f"{W} wins the Class {cls} state championship." if cls else f"{W} wins the state championship."
    if proj:
        wp_w, margin_proj = proj["win_prob_winner"], proj["score_winner"] - proj["score_loser"]
        actual = sw - sl
        if wp_w < UPSET_PROB:
            return f"Upset: {W} knocks off {L} as {a_an(pct_text(wp_w))} shot."
        if abs(actual - margin_proj) <= ON_NUMBER_POINTS:
            how = "exactly" if actual == margin_proj else "almost exactly"
            return f"Right on the number: {W} wins by {actual}, {how} the projected margin."
        if actual <= margin_proj - SWING_POINTS:
            return f"{W} survives: a {score} win that was closer than projected."
        if actual >= margin_proj + SWING_POINTS:
            return f"{W} rolls past {L}, {actual - margin_proj} points better than projected."
    return f"{W} beats {L}, {score}."


def next_round_phrase(round_index, cls):
    label = mp.PLAYOFF_ROUNDS[round_index][0]
    cls_txt = f"Class {cls} " if cls else ""
    if label == "District final":
        return f"won its {cls_txt}district title and moves on to the state quarterfinals"
    if label == "State championship":
        return None
    nxt = mp.PLAYOFF_ROUNDS[round_index + 1][0].lower()
    if nxt.startswith("state "):
        return f"moved into the {cls_txt}{nxt[6:]}s" if nxt.endswith("final") else f"moved on to the {cls_txt}{nxt}"
    return f"moved on to the {cls_txt}{nxt}"


def write_story(W, L, sw, sl, ctxW, ctxL, proj, rnd, rnd_i, cls, notesW, classified_L, classified_W=True):
    out = []
    score = f"{sw}-{sl}"
    if rnd and rnd_i is not None:
        phrase = next_round_phrase(rnd_i, cls)
        if phrase:
            out.append(f"{W} {phrase} with a {score} win over {L}.")
        else:
            out.append(f"{W} finished the season {ctxW['record']} with a {score} win over {L} in the state championship game.")
        if classified_L:
            out.append(f"{L}'s season ends at {ctxL['record']}.")
    else:
        lose_txt = f"; {L} falls to {ctxL['record']}" if classified_L else ""
        if classified_W:
            out.append(f"{W} improved to {ctxW['record']} with a {score} win over {L}{lose_txt}.")
        else:   # out-of-state winner: its record here would count Missouri games only
            out.append(f"{W} beat {L} {score}{lose_txt}.")
    if proj:
        fav_is_w = proj["score_winner"] >= proj["score_loser"]
        fav = W if fav_is_w else L
        a, b = (proj["score_winner"], proj["score_loser"]) if fav_is_w else (proj["score_loser"], proj["score_winner"])
        wp = proj["win_prob_winner"] if fav_is_w else 1 - proj["win_prob_winner"]
        out.append(f"The pre-game projection had {fav} by {a - b} ({a}-{b}) with {a_an(pct_text(wp))} chance to win.")
        offW, offL = sw - proj["raw_winner"], sl - proj["raw_loser"]
        if offW >= PERF_POINTS and offL >= PERF_POINTS:
            out.append("Both offenses beat their projections by a touchdown or more.")
        elif offW <= -PERF_POINTS and offL <= -PERF_POINTS:
            out.append("Both defenses held the other side well under its projection.")
        else:
            if offW >= PERF_POINTS:
                out.append(f"{W}'s offense scored {round(offW)} more points than projected.")
            if offL <= -PERF_POINTS:
                out.append(f"{W}'s defense allowed {round(-offL)} fewer points than projected.")
    bits = []
    for n in notesW:
        if n["kind"] == "streak":
            bits.append(f"It was {W}'s {ordinal_word(ctxW['streak'])} straight win")
        if n["kind"] == "best_win":
            bits.append("its best win of the season so far by opponent rating" if bits else
                        f"It was {W}'s best win of the season so far by opponent rating")
    if bits:
        out.append(" and ".join(bits) + ".")
    return " ".join(s for s in out if s)


# --------------------------------------------------------------------------
# one recap
# --------------------------------------------------------------------------

def build_recap(g, gid, logs, rh, hh, resolver, latest_ratings, now):
    date = iso(g["date"])
    ko, end = kickoff(date), game_day_end(date)
    waited = (now - end).days >= WAIT_DAYS

    post = rh.after(end)
    if post is None:
        if not waited:
            return None, "waiting for post-game ratings"
        post = rh.latest()
    post_teams, post_class_rank, post_class_size = ratings_maps(rh.load(post[1]))
    pre = rh.before(ko)
    pre_teams = ratings_maps(rh.load(pre[1]))[0] if pre else {}

    rnd, rnd_i = playoff_round(date)
    hub_before = hub_after = None
    hb = ha = None
    if rnd:
        hb = hh.before(ko)
        ha = hh.after(end)
        if ha is None:
            if not waited:
                return None, "waiting for post-game playoff hub"
            ha = hh.latest()
        hub_before = hh.load(hb[1]) if hb else None
        hub_after = hh.load(ha[1])

    s1, s2 = g["score1"], g["score2"]
    if s1 >= s2:
        W, L, sw, sl, cw, cl = g["team1"], g["team2"], s1, s2, g.get("team1_classified", True), g.get("team2_classified", True)
    else:
        W, L, sw, sl, cw, cl = g["team2"], g["team1"], s2, s1, g.get("team2_classified", True), g.get("team1_classified", True)
    tie = s1 == s2

    ctxW = context_through(logs[W], date, L)
    ctxL = context_through(logs[L], date, W)

    proj = None
    if has_off_def(pre_teams.get(W)) and has_off_def(pre_teams.get(L)):
        a, b = pre_teams[W], pre_teams[L]
        raw_w, raw_l, sc_w, sc_l, wp_w = mp.predictor_projection(a["off_rating"], a["def_rating"], b["off_rating"], b["def_rating"])
        proj = {"raw_winner": round(raw_w, 2), "raw_loser": round(raw_l, 2), "score_winner": sc_w, "score_loser": sc_l,
                "win_prob_winner": round(wp_w, 4),
                "ratings_updated": mp.central_time(rh.load(pre[1]).get("last_updated"))}

    cls = None
    for nm in (W, L):
        if post_teams.get(nm, {}).get("classification") is not None:
            cls = post_teams[nm]["classification"]
            break

    notesW = [] if tie else team_notes(W, ctxW, post_teams, True)
    notesL = [] if tie else team_notes(L, ctxL, post_teams, False)
    if proj and not tie and proj["win_prob_winner"] < UPSET_PROB:
        notesW.insert(0, {"kind": "upset", "label": "Upset", "text": f"Won with {a_an(pct_text(proj['win_prob_winner']))} pre-game chance to win."})

    def side(name, score, ctx, classified, notes, raw_for, raw_against):
        pt = post_teams.get(name)
        slug = resolver(name) if classified else None
        perf = None
        if raw_for is not None and not g.get("forfeit"):
            off = score - raw_for
            deff = raw_against - (ctx["this"]["pa"] if ctx["this"] else 0)
            perf = {"off": round(off, 1), "def": round(deff, 1), "ovr": round(off + deff, 1)}
        return {
            "name": name, "slug": slug, "logo": mp.LOGO_URL.format(slug=slug) if slug else None,
            "score": score, "classified": bool(classified),
            "record": ctx["record"] if classified else None,
            "ovr_rank": pt.get("ovr_rank") if pt else None,
            "class": pt.get("classification") if pt else None,
            "class_rank": post_class_rank.get(name),
            "class_size": post_class_size.get(pt.get("classification")) if pt else None,
            "ovr_rating": pt.get("ovr_rating") if pt else None,
            "perf": perf, "notes": notes,
            "odds": odds_rows(hub_before, hub_after, name) if rnd else None,
        }

    winner = side(W, sw, ctxW, cw, notesW,
                  proj["raw_winner"] if proj else None, proj["raw_loser"] if proj else None)
    loser = side(L, sl, ctxL, cl, notesL,
                 proj["raw_loser"] if proj else None, proj["raw_winner"] if proj else None)

    if tie:
        headline = f"{W} and {L} play to a {sw}-{sl} tie."
        story = ""
    elif g.get("forfeit"):
        headline = f"{W} wins by forfeit over {L}."
        story = ""
    else:
        headline = write_headline(W, L, sw, sl, proj, rnd, cls)
        story = write_story(W, L, sw, sl, ctxW, ctxL, proj, rnd, rnd_i, cls, notesW, cl, cw)

    return {
        "id": gid, "date": date, "week": g.get("week"), "round": rnd, "class": cls,
        "postseason": bool(rnd), "tie": tie, "forfeit": bool(g.get("forfeit")), "overtime": bool(g.get("overtime")),
        "sides": [winner, loser],            # winner first (game-file order on a tie)
        "projection": proj,
        "headline": headline, "story": story,
        "frozen_at": now.astimezone(CENTRAL).isoformat(timespec="minutes"),
        "post_ratings_updated": mp.central_time(rh.load(post[1]).get("last_updated")),
        "post_ratings_stamp": rh.load(post[1]).get("last_updated"),   # raw, compared by the page
        "sources": {"pre_ratings": pre[1][:10] if pre else None, "post_ratings": post[1][:10],
                    "hub_before": (hb[1][:10] if rnd and hb else None), "hub_after": (ha[1][:10] if rnd else None)},
    }, None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="recompute every recap (unfreeze all)")
    ap.add_argument("--rebuild-game", action="append", default=[], help="recompute one recap id (repeatable)")
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)

    games = mp.load_json(GAMES_PATH)
    sds.bdp.apply_forfeit_overrides(games)
    logs = team_logs(games)
    rh, hh = History(RATINGS_PATH), History(HUB_PATH)
    resolver = mp.SlugResolver(mp.load_data_file("output/schools.json")["schools"],
                               mp.load_data_file("school_aliases.json"))

    existing = {}
    if os.path.isdir(OUTPUT_DIR) and not args.rebuild:
        for fn in sorted(os.listdir(OUTPUT_DIR)):
            if fn.endswith(".json") and fn != "manifest.json":
                existing.update(mp.load_json(os.path.join(OUTPUT_DIR, fn)).get("recaps", {}))

    def sid(name):
        return resolver(name) or mp.make_slug(name)

    recaps, waiting, rebuilt_scores = {}, 0, []
    for g in games:
        if g.get("score1") is None or g.get("score2") is None:
            continue
        date = iso(g["date"])
        gid = f"{date}-{sid(g['team1'])}-vs-{sid(g['team2'])}"
        old = existing.get(gid)
        if old and gid not in args.rebuild_game:
            scores = sorted([s["score"] for s in old["sides"]])
            if scores == sorted([g["score1"], g["score2"]]):
                recaps[gid] = old                      # frozen
                continue
            rebuilt_scores.append(gid)                 # score corrected since -> rebuild
        rec, why = build_recap(g, gid, logs, rh, hh, resolver, rh.latest(), now)
        if rec is None:
            waiting += 1
            continue
        recaps[gid] = rec

    by_date = defaultdict(dict)
    for gid, r in recaps.items():
        by_date[r["date"]][gid] = r
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    changed = 0
    for date, items in sorted(by_date.items()):
        path = os.path.join(OUTPUT_DIR, f"{date}.json")
        text = json.dumps({"season": SEASON, "date": date, "recaps": dict(sorted(items.items()))}, separators=(",", ":"))
        old_text = open(path).read() if os.path.exists(path) else None
        if text != old_text:
            with open(path, "w") as f:
                f.write(text)
            changed += 1
    dates = {d: len(v) for d, v in sorted(by_date.items())}
    old_manifest = mp.load_json(MANIFEST_PATH) if os.path.exists(MANIFEST_PATH) else {}
    if changed or old_manifest.get("dates") != dates:
        with open(MANIFEST_PATH, "w") as f:
            json.dump({"season": SEASON, "generated": now.astimezone(CENTRAL).isoformat(timespec="minutes"),
                       "league_average": mp.LEAGUE_AVERAGE, "dates": dates}, f, indent=1)

    new = len([k for k in recaps if k not in existing])
    print(f"{len(recaps)} recaps ({new} new, {len(rebuilt_scores)} rebuilt for score corrections), "
          f"{waiting} final games still waiting for post-game ratings/hub, {changed} date files written.")
    for gid in rebuilt_scores:
        print(f"  score corrected -> rebuilt {gid}")


if __name__ == "__main__":
    main()
