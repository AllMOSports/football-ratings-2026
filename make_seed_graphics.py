"""
make_seed_graphics.py -- All MO Sports district seed projection graphics (PNG).

Usage:
    python make_seed_graphics.py class4_district7 class2_district6 class5_district6 \
        --logo-dir logos_main2 --brand-logo all_mo_sports_logo.png --out graphics

Reads district_seed_projections_2026.json (seed odds), football_ratings_2026.json and
the games CSV (remaining games + win probabilities). Team logos are looked up in
--logo-dir by a few slug variants; a team without a logo gets an initials badge.
Headlines/storylines live in STORIES below -- edit per post.
"""
import argparse, csv, datetime, json, math, os, re
from PIL import Image, ImageDraw, ImageFont

PROJ_PATH = "district_seed_projections_2026.json"
RATINGS_PATH = "football_ratings_2026.json"
GAMES_PATH = "football_games_2026_all_district_points.csv"
REGULAR_SEASON_END = datetime.date(2026, 10, 24)

NAVY, GOLD = (16, 30, 58), (235, 172, 48)
INK, MUTED, RULE = (26, 34, 51), (110, 118, 135), (225, 229, 237)
WHITE = (255, 255, 255)

FONT_DIR = "/usr/share/fonts/opentype/inter"
def font(weight, size):
    name = {"reg": "Inter-Regular", "med": "Inter-Medium", "semi": "Inter-SemiBold",
            "bold": "Inter-Bold", "xbold": "Inter-ExtraBold", "black": "Inter-Black"}[weight]
    for p in (f"{FONT_DIR}/{name}.otf", f"{name}.ttf", f"{name}.otf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.truetype("DejaVuSans-Bold.ttf" if weight != "reg" else "DejaVuSans.ttf", size)

SHORT = {
    "University Academy Charter with Ewing Marion Kauffman": "UA Charter / Kauffman",
    "Hogan Prep Academy Charter": "Hogan Prep",
    "Lone Jack with Kingsville": "Lone Jack / Kingsville",
    "Central (Kansas City)": "Central (KC)",
    "East (Kansas City)": "East (KC)",
    "Northeast (Kansas City)": "Northeast (KC)",
    "St. Michael the Archangel": "St. Michael",
    "Central (New Madrid County)": "Central (New Madrid Co.)",
}

# Per-district headline copy. Anything not listed gets a generic line.
STORIES = {
    "class4_district7": ("Four teams, no favorite",
                         "Nobody tops 39% for the No. 1 seed, and 3-4 Raytown South still "
                         "has a 1-in-4 shot at it."),
    "class2_district6": ("Points leader isn't the favorite",
                         "Hogan Prep leads the district in points today, but Butler's softer "
                         "finish makes it the most likely No. 1."),
    "class5_district6": ("Four 6-1 teams, two top seeds",
                         "Republic and Carthage meet Friday for the inside track to No. 1. "
                         "Neosho is third in points but most likely to finish fifth."),
}

STORIES.update({
    "class4_district1": ("Best record, most points, likely No. 3",
                         "Hillsboro is 6-1 and leads the district in points, but it's a big underdog "
                         "against Farmington on Friday. Festus and Sikeston are nearly a coin flip for No. 1."),
    "class6_district6": ("Four 5-2 teams, one clear favorite",
                         "Blue Springs is 72% for the No. 1 seed. Behind it, Friday's Hickman vs. "
                         "Rock Bridge toss-up goes a long way toward sorting out Nos. 3 and 4."),
})

# Optional hand-picked "games that swing it" (team1, team2) per district;
# districts not listed pick the closest remaining games involving contenders.
KEY_GAMES = {
    "class2_district6": [("Butler", "Versailles"),
                         ("Hogan Prep Academy Charter", "St. Michael the Archangel"),
                         ("KIPP KC Legacy", "Northeast (Kansas City)")],
    "class4_district1": [("Hillsboro", "Farmington"),
                         ("Festus", "Poplar Bluff"),
                         ("Central (New Madrid County)", "Sikeston")],
    "class6_district6": [("Hickman", "Rock Bridge"),
                         ("Lee's Summit", "Blue Springs"),
                         ("Liberty", "Lee's Summit North")],
}

# ---------------------------------------------------------------- helpers
def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))

def cell_colors(p):
    if p <= 0:
        return WHITE, (196, 201, 212)
    lo, mid = (238, 241, 247), (125, 139, 173)
    bg = mix(lo, mid, p / 0.5) if p < 0.5 else mix(mid, NAVY, (p - 0.5) / 0.5)
    return bg, (WHITE if p >= 0.4 else INK)

def pct(p):
    if p <= 0: return ""
    if p < 0.005: return "<1"
    if p >= 0.995: return ">99"
    return str(max(1, round(p * 100)))

def display(name):
    return SHORT.get(name, name)

def text_w(d, s, f):
    return d.textlength(s, font=f)

def fit(d, s, weight, size, max_w, min_size=24):
    while size > min_size and text_w(d, s, font(weight, size)) > max_w:
        size -= 1
    f = font(weight, size)
    if text_w(d, s, f) > max_w:
        while s and text_w(d, s + "…", f) > max_w:
            s = s[:-1]
        s = s.rstrip() + "…"
    return s, f

def wrap(d, s, f, max_w):
    lines, cur = [], ""
    for w in s.split():
        t = (cur + " " + w).strip()
        if text_w(d, t, f) <= max_w:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def slug_variants(name):
    base = name.lower().replace("&", "and")
    out = []
    for sep in ("-", "_", ""):
        out.append(re.sub(r"[^a-z0-9]+", sep, base).strip(sep))
    out += [name, name.replace(" ", "_")]
    return out

def load_logo(logo_dir, name, size):
    if logo_dir:
        for s in slug_variants(name):
            for ext in (".png", ".webp", ".jpg"):
                p = os.path.join(logo_dir, s + ext)
                if os.path.exists(p):
                    im = Image.open(p).convert("RGBA")
                    im.thumbnail((size, size), Image.LANCZOS)
                    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
                    return canvas
    # initials badge
    words = [w for w in re.sub(r"\(.*?\)", "", display(name)).replace("/", " ").split()
             if w[0].isalnum()]
    ini = "".join(w[0] for w in words[:3]).upper() or "?"
    scale = 4
    big = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    bd = ImageDraw.Draw(big)
    bd.ellipse((0, 0, size * scale - 1, size * scale - 1), fill=(234, 237, 244))
    f = font("bold", int(size * scale * (0.38 if len(ini) < 3 else 0.30)))
    bd.text((size * scale / 2, size * scale / 2), ini, font=f, fill=NAVY, anchor="mm")
    return big.resize((size, size), Image.LANCZOS)

def brand_mark(path, h):
    if path and os.path.exists(path):
        im = Image.open(path).convert("RGBA")
        im.thumbnail((int(h * 2.6), h), Image.LANCZOS)
        return im
    # wordmark fallback
    f1, f2 = font("black", int(h * 0.42)), font("black", int(h * 0.42))
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    w = int(max(tmp.textlength("ALL MO", font=f1), tmp.textlength("SPORTS", font=f2))) + 4
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((w, h * 0.04), "ALL MO", font=f1, fill=NAVY, anchor="ra")
    d.text((w, h * 0.50), "SPORTS", font=f2, fill=GOLD, anchor="ra")
    return im

# ---------------------------------------------------------------- data
def win_prob(r1, r2, sigma):
    return 0.5 * (1 + math.erf((r1 - r2) / (sigma * math.sqrt(2))))

def load_games():
    rows = []
    for r in csv.DictReader(open(GAMES_PATH, newline="", encoding="utf-8")):
        d = datetime.datetime.strptime(r["date"].strip(), "%m/%d/%Y").date()
        played = (r["score1"] or "").strip() != "" and (r["score2"] or "").strip() != ""
        rows.append((d, r["team1"].strip(), r["team2"].strip(), played))
    return rows

def key_games(dteams, games, ovr, sigma, limit=3, picks=None):
    if picks:
        order = {frozenset(p): i for i, p in enumerate(picks)}
        out = [(0, order[frozenset((a, b))], d, a, b, win_prob(ovr.get(a, 0.0), ovr.get(b, 0.0), sigma))
               for d, a, b, played in games
               if not played and frozenset((a, b)) in order]
        return sorted(out, key=lambda x: (x[2], x[1]))
        return sorted(out, key=lambda x: x[2])
    names = {t["team"] for t in dteams}
    contenders = {t["team"] for t in dteams if t["seed_probs"][0] + t["seed_probs"][1] >= 0.05}
    out = []
    for d, a, b, played in games:
        if played or d > REGULAR_SEASON_END or not ({a, b} & contenders):
            continue
        pa = win_prob(ovr.get(a, 0.0), ovr.get(b, 0.0), sigma)
        both = a in contenders and b in contenders
        out.append((not both, abs(pa - 0.5), d, a, b, pa))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return sorted(out[:limit], key=lambda x: (x[2], x[0], x[1]))

# ---------------------------------------------------------------- render
W, CANVAS_H = 1200, 1800
PAD = 60

def render(key, dist, proj, games, ovr, logo_dir, brand_path, through):
    H = CANVAS_H
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    teams = dist["teams"]
    n_seeds = len(teams[0]["seed_probs"])
    head, story = STORIES.get(key, ("Seed projections", "Chance of finishing in each district seed."))

    # brand mark top-right
    bm = brand_mark(brand_path, 104)
    img.paste(bm, (W - PAD - bm.width, PAD - 6), bm)

    # header
    y = PAD
    d.text((PAD, y), "DISTRICT SEED PROJECTIONS", font=font("bold", 24), fill=GOLD)
    y += 38
    d.text((PAD, y), f"Class {dist['class']}  ·  District {dist['district']}",
           font=font("black", 68), fill=NAVY)
    y += 92
    d.text((PAD, y), head, font=font("bold", 34), fill=INK)
    y += 48
    sf = font("reg", 26)
    for line in wrap(d, story, sf, W - 2 * PAD)[:2]:
        d.text((PAD, y), line, font=sf, fill=MUTED)
        y += 36
    y += 44

    # table geometry
    logo_sz, row_h = 60, 84
    cell_w = min(80, (W - 2 * PAD - 520) // n_seeds)
    seeds_x0 = W - PAD - cell_w * n_seeds
    pts_x = seeds_x0 - 28            # right edge of PTS column
    name_x = PAD + logo_sz + 18
    name_max = pts_x - 90 - name_x

    hf = font("bold", 19)
    d.text((name_x, y), "TEAM", font=hf, fill=MUTED)
    d.text((pts_x, y), "PTS", font=hf, fill=MUTED, anchor="ra")
    d.text((seeds_x0 + cell_w * n_seeds / 2, y - 28), "CHANCE OF EACH SEED (%)",
           font=font("bold", 17), fill=MUTED, anchor="ma")
    for s in range(n_seeds):
        d.text((seeds_x0 + cell_w * s + cell_w / 2, y), f"#{s+1}", font=hf, fill=NAVY, anchor="ma")
    y += 34
    d.line((PAD, y, W - PAD, y), fill=NAVY, width=3)
    y += 8

    for i, t in enumerate(teams):
        top = y + i * row_h
        cy = top + row_h / 2
        logo = load_logo(logo_dir, t["team"], logo_sz)
        img.paste(logo, (PAD, int(cy - logo_sz / 2)), logo)
        nm, nf = fit(d, display(t["team"]), "bold", 30, name_max)
        d.text((name_x, cy - 4), nm, font=nf, fill=INK, anchor="ls")
        d.text((name_x, cy + 26), t["record"], font=font("med", 22), fill=MUTED, anchor="ls")
        d.text((pts_x, cy), f"{t['current_points']:.1f}", font=font("semi", 26), fill=INK, anchor="rm")
        for s, p in enumerate(t["seed_probs"]):
            x0 = seeds_x0 + cell_w * s
            bg, fg = cell_colors(p)
            d.rectangle((x0 + 2, top + 6, x0 + cell_w - 2, top + row_h - 6), fill=bg)
            d.text((x0 + cell_w / 2, cy), pct(p), font=font("bold", 26), fill=fg, anchor="mm")
        if i < len(teams) - 1:
            d.line((PAD, top + row_h, seeds_x0 - 6, top + row_h), fill=RULE, width=1)
    y += len(teams) * row_h + 40

    # key games
    kg = key_games(teams, games, ovr, proj["sigma"], picks=KEY_GAMES.get(key))
    if kg:
        d.text((PAD, y), "GAMES THAT SWING IT", font=font("bold", 24), fill=GOLD)
        d.text((W - PAD, y + 4), "win probability (first team / second team)", font=font("med", 19), fill=MUTED, anchor="ra")
        y += 44
        bar_x0, bar_x1 = PAD + 640, W - PAD
        for _, _, gd, a, b, pa in kg:
            d.text((PAD, y + 4), gd.strftime("%b. %-d").upper(), font=font("bold", 20), fill=MUTED)
            matchup = f"{display(a)} vs. {display(b)}"
            mt, mf = fit(d, matchup, "semi", 26, bar_x0 - PAD - 110 - 20)
            d.text((PAD + 110, y), mt, font=mf, fill=INK)
            split = bar_x0 + (bar_x1 - bar_x0) * pa
            d.rectangle((bar_x0, y + 2, split, y + 34), fill=NAVY)
            d.rectangle((split, y + 2, bar_x1, y + 34), fill=GOLD)
            lf = font("bold", 21)
            if split - bar_x0 >= 64:
                d.text((bar_x0 + 10, y + 18), pct(pa) + "%", font=lf, fill=WHITE, anchor="lm")
            if bar_x1 - split >= 64:
                d.text((bar_x1 - 10, y + 18), pct(1 - pa) + "%", font=lf, fill=NAVY, anchor="rm")
            y += 54

    # footer -- crop canvas to content
    fh = 78
    H2 = int(y + 36 + fh + 6)
    img = img.crop((0, 0, W, H2)); d = ImageDraw.Draw(img); H = H2  # noqa
    d.rectangle((0, H - fh, W, H), fill=NAVY)
    d.text((PAD, H - fh / 2), "@All_MO_Sports  ·  allmosports.com", font=font("bold", 24),
           fill=WHITE, anchor="lm")
    d.text((W - PAD, H - fh / 2),
           f"{proj['simulations']:,} simulations  ·  games through {through}",
           font=font("med", 22), fill=(200, 207, 222), anchor="rm")
    d.rectangle((0, H - fh - 6, W, H - fh), fill=GOLD)
    return img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("districts", nargs="+", help="keys like class4_district7")
    ap.add_argument("--proj", default=PROJ_PATH)
    ap.add_argument("--logo-dir", default=None)
    ap.add_argument("--brand-logo", default=None)
    ap.add_argument("--out", default="graphics")
    a = ap.parse_args()

    proj = json.load(open(a.proj))
    ovr = {t["school"]: t["ovr_rating"] for t in json.load(open(RATINGS_PATH))["teams"]}
    games = load_games()
    last = max(g[0] for g in games if g[3])
    through = last.strftime("%b. %-d")
    os.makedirs(a.out, exist_ok=True)
    for key in a.districts:
        img = render(key, proj["districts"][key], proj, games, ovr, a.logo_dir, a.brand_logo, through)
        path = os.path.join(a.out, f"seed_projections_{key}.png")
        img.save(path, optimize=True)
        print("wrote", path)

if __name__ == "__main__":
    main()
