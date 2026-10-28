#!/usr/bin/env python3
"""Promotion strength as a function of (promotion, date, sport). Rules: docs/promotion_strength.md

  STRENGTH = f(promotion_id, sport, t)   in [0, 1], 0.5 = the era's average promotion

Three changes from v1, all enabled by the current dataset:

  * PER SPORT. v1 was MMA-only and said so (its section 8). A promotion running MMA,
    kickboxing and Muay Thai is three different operations of three different qualities --
    ONE is a top-3 MMA promotion and the largest Muay Thai promotion on earth, and one
    number cannot say both. Every signal is computed inside a sport.
  * PER DATE, CAUSALLY. Quarterly grid; the window for quarter Q ends at Q's first day,
    so no bout can ever contribute to the strength that weights it.
  * MORE SIGNALS. v1 had four (intake, consistency, matchmaking, roster) because that was
    all the CSVs held. The crawl since then adds belts and reigns, title stakes, weigh-ins,
    broadcast, cancellations, referees, nationalities and card structure.

Records are still used (intake, matchmaking) -- what stays banned is Elo, or anything
derived from it, because this feeds Elo (v1 section 1.1).

Outputs
  data/promotion_strength.jsonl   one row per (promotion_id, sport, quarter)
  logs/promotion_strength.log     field sizes, coverage, and the top table per sport

  python3 scrapers/derive/promotion_strength.py [--sport MMA] [--rebuild-cache]
"""
import argparse, bisect, json, collections, math, pickle, re, sys, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA, STATE = ROOT / "data", ROOT / "logs"
CACHE = Path("/private/tmp/claude-501/-Users-yousefhammoudeh-Desktop-MMA-Data/"
             "5232c7f8-9841-4c55-998a-e3eaa03a2978/scratchpad/ps_cache.pkl")

WIN_DAYS   = 1095        # 3-year trailing window (v1, unchanged)
K_EXP      = 10          # fights at which `experience` reaches 0.5
ALPHA      = 0.60        # intake: win-rate vs experience
CHAMP_BONUS = 0.50       # elite: full credit for a champion with five title bouts behind them
M_INTAKE   = 25          # empirical-Bayes pseudo-counts
M_MATCH    = 50
MIN_REC    = 2           # fights on both records before a bout can measure matchmaking
Z_CLIP     = 5.0         # no single signal may swing the composite further than this
M_CELL     = 14          # bouts at that level before a cell is trusted (fitted)
M_TENURE   = 7           # months a promotion must have existed before the same (fitted)
FIELD_THIN = 30          # below this many field members, the normalisation is not meaningful
EXPORT_ELITE = [0.85]    # "reached the top": derived at run time as the 99th percentile of
                         # professional norms, which is where the hand-picked 0.85 sat
                         # anyway (p98.85 of 152,155 cells). Listed so it stays a
                         # measurement rather than a constant that drifts out of date.
M_EXPORT   = 50          # fighters before an export share is taken at face value
EXPORT_REF = [0.0, 1.0]  # [rate, count] refs, set at run time; rate 0 = feature off
SPORT_KIN = {"Judo": "Grappling", "Sambo": "Grappling", "Taekwondo": "Kickboxing",
             "Vale Tudo": "MMA", "Ice Fighting": "Boxing (Bare Knuckle)"}
M_TITLE    = 8           # title bouts before title-integrity is taken at face value
MINF       = 8           # fighters in window to enter the field mean
K_SCALE    = 0.30        # monopoly dial (v1, unchanged)
A_NORM     = 1.0         # logistic steepness, applied to the composite z
NO_DATA    = 0.30        # no bouts in window
EPOCH      = dt.date(1900, 1, 1).toordinal()

# Quality weights. Availability-gated signals (title, ops, dist) drop out and have their
# weight redistributed over the rest when a promotion has no data for them -- absence of a
# broadcast listing is a fact about Tapology's US-facing coverage, not about the promotion.
# Fitted 2026-08-28 against data/rankings/ -- Tapology's Major-Org label and Fight Matrix's
# four orderings -- by scripts/tune_promotion_strength.py. NOT the argmax, which with 9
# labelled majors and 13 ranked promotions lands in a corner that zeroes `intake` (the signal
# that on its own separates majors best, AUC 0.9915). These are the mean of the top 1% of
# solutions, which is the part of the fit that reproduces across both sources: more on `card`
# and `match`, less on `intake` and `ops`. The individual weights are still poorly identified
# -- across those solutions `match` ranges 0.03-0.44 and `title` 0.00-0.45 -- so read them as
# a direction, not a measurement. Weights and the scale/shrinkage parameters below have to
# move together: applying the weights alone raised Major-Org AUC and LOWERED Fight Matrix
# agreement (0.662 -> 0.632).
WEIGHTS = {"intake": 0.099, "elite": 0.120, "title": 0.152, "consistency": 0.107,
           "card": 0.201, "match": 0.214, "dist": 0.081, "ops": 0.027}
AM_WEIGHTS = {"intake": 0.16, "elite": 0.08, "grad": 0.20, "consistency": 0.13,
              "card": 0.11, "match": 0.11, "title": 0.11, "dist": 0.05, "ops": 0.05}
AM_FLOOR, AM_CEIL = 0.15, 0.45   # the amateur band, see docs: 0.30 at the amateur median
AM_CEIL_MAX = 0.90       # --export-ceiling: how high an amateur programme may reach when its
                         # alumni demonstrably populate the top professional promotions
GATED   = {"title", "ops", "dist"}   # absent -> neutral, weight redistributed
K_SCALE_Z  = 0.42        # how far scale may move the composite, in field sd (fitted)
FLOOR      = 1e-3

# Broadcast tiers. Keyword match, most specific first.
BCAST = [(r"pay ?per ?view|ppv", 0.95), (r"espn\b|espn\+|fox sports|showtime|dazn|spike|axs|"
         r"cbs|nbc|abc|bt sport|sky sports|tnt|hbo", 1.00),
         (r"ufc fight pass|fite|trillertv|flo|gfl\.tv|combat sports now|dailymotion", 0.60),
         (r"youtube|facebook|twitch|internet stream|internet ppv|free", 0.30)]

def bcast_tier(s):
    s = (s or "").lower()
    for pat, v in BCAST:
        if re.search(pat, s): return v
    return 0.50

def ordi(d):   # "YYYY-MM-DD" -> day ordinal, or None
    try: return dt.date(int(d[:4]), int(d[5:7]), int(d[8:10])).toordinal()
    except Exception: return None

def quarters(lo, hi):
    y, q = dt.date.fromordinal(lo).year, (dt.date.fromordinal(lo).month - 1) // 3
    out = []
    while True:
        d = dt.date(y, q * 3 + 1, 1)
        if d.toordinal() > hi: break
        out.append(d.toordinal())
        q += 1
        if q == 4: y, q = y + 1, 0
    return out

ROUNDS = re.compile(r"^\s*(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*$")

# ---------------------------------------------------------------- cache

def build_cache():
    print("reading event_details.jsonl ...", flush=True)
    freetext = {}
    for line in open(DATA / "event_promoter_freetext.jsonl"):
        o = json.loads(line); freetext[o["event_slug"]] = o["promoter_id"]
    ev, n_ft = {}, 0
    for line in open(DATA / "event_details.jsonl", errors="replace"):
        o = json.loads(line)
        p = o.get("promotion") or {}
        pid = p.get("promotion_id") if isinstance(p, dict) else None
        if pid is None: pid = o.get("index_promotion_id")   # page block first, index second
        if pid is None:
            pid = freetext.get(o["event_slug"])              # then the bare-text promoter
            if pid is not None: n_ft += 1
        prod = o.get("production") or {}
        d = ordi(o.get("event_date_index") or "")
        ev[o["event_id"]] = (d, pid, bcast_tier(prod.get("u_s_broadcast")),
                             bool(prod.get("u_s_broadcast")), len(o.get("cancelled_bouts") or []),
                             bool(o.get("awards")), bool(o.get("weigh_ins")),
                             bool(o.get("referees")), o.get("region_slug"))
    print(f"  {len(ev):,} events ({n_ft:,} attributed to a free-text promoter)", flush=True)

    print("reading bouts.jsonl ...", flush=True)
    B = []                                   # bout rows, filled below
    for line in open(DATA / "bouts.jsonl", errors="replace"):
        o = json.loads(line)
        e = ev.get(o.get("event_id"))
        if not e or e[0] is None or e[1] is None: continue
        d, pid = e[0], e[1]
        sport = sys.intern(o.get("sport_base") or "?")
        m = ROUNDS.match(o.get("round_format") or "")
        smin = float(m.group(1)) * float(m.group(2)) if m else None
        fs = o.get("fighters") or []
        if len(fs) != 2: continue
        cmpn = o.get("comparison") or {}
        B.append([pid, sport, d, o.get("level") == "Professional", o.get("title_kind"),
                  smin, o.get("billing"), bool(cmpn.get("betting_odds")),
                  o.get("event_id"), sys.intern(o.get("bout_slug") or ""),
                  [(sys.intern(f.get("fighter_slug") or ""), f.get("result"),
                    f.get("record_after"), f.get("nationality_code")) for f in fs],
                  o.get("level")])
    print(f"  {len(B):,} bouts on dated, attributed events", flush=True)

    # Records entering the bout. record_after exists for MMA only (96%); every other sport
    # gets a running count reconstructed from our own card data, which is what makes the
    # intake signal portable across sports at all.
    B.sort(key=lambda r: r[2])
    run, first_seen, first_pro, src = {}, {}, {}, collections.Counter()
    for r in B:
        pid, sport, d, level = r[0], r[1], r[2], r[11]
        pre = []
        for slug, res, ra, nat in r[10]:
            key = (slug, sport, level)
            w, l, dr = run.get(key, (0, 0, 0))
            if ra:                                   # authoritative: strip this result off
                parts = [int(x) for x in ra.split("-")]
                aw, al = parts[0], parts[1]
                ad = parts[2] if len(parts) > 2 else 0
                w2, l2, d2 = aw - (res == "W"), al - (res == "L"), ad - (res == "D")
                if min(w2, l2, d2) >= 0: w, l, dr = w2, l2, d2; src["record_after"] += 1
                else: src["record_after_bad"] += 1
            else: src["reconstructed"] += 1
            n = w + l + dr
            pre.append((n, (w + 0.5 * dr + 1) / (n + 2)))     # (fights, Laplace win-rate)
            run[key] = (w + (res == "W"), l + (res == "L"), dr + (res == "D"))
            fk = (slug, pid, sport)
            if fk not in first_seen: first_seen[fk] = d
            if level == "Professional":
                pk = (slug, sport)
                if pk not in first_pro: first_pro[pk] = d
        r.append(pre)
    print(f"  record source: {dict(src)}", flush=True)

    # When each fighter first won a belt, for the top-end signal. Only belts with >= 5 bouts
    # in their lineage: a one-card promotion inventing a title for its main event should not
    # mint champions. Keyed by (slug, sport) because a champion in one sport is not one in
    # another, and dated so the signal stays causal.
    belt_ok, belt_sport = set(), {}
    for line in open(DATA / "belts.jsonl"):
        o = json.loads(line)
        belt_sport[o["belt_id"]] = o["sport"]
        if (o.get("bouts") or 0) >= 5: belt_ok.add(o["belt_id"])
    reigns = collections.defaultdict(list)
    for line in open(DATA / "belt_reigns.jsonl"):
        o = json.loads(line)
        if o["belt_id"] not in belt_ok or not o.get("won_at_date"): continue
        d = ordi(o["won_at_date"]); slug = o.get("champion_slug")
        if not d or not slug: continue
        reigns[(sys.intern(slug), belt_sport[o["belt_id"]])].append((d, o.get("defenses") or 0))
    champ_from = {}
    for k, xs in reigns.items():
        xs.sort(); run, tot = [], 0
        for d, dfn in xs:
            tot += 1 + dfn                    # the win itself, plus each defence
            run.append((d, tot))
        champ_from[k] = run
    print(f"  {len(champ_from):,} fighter-sport champions from {len(belt_ok):,} "
          f"established belts", flush=True)

    # side tables keyed by bout_slug
    wi = {}
    for line in open(DATA / "bout_weigh_ins.jsonl"):
        o = json.loads(line)
        over = o.get("over_lb")
        wi[o["bout_slug"]] = bool(over) and any(x for x in (over if isinstance(over, list) else [over]))
    stakes = {}
    for line in open(DATA / "bout_title_stakes.jsonl"):
        o = json.loads(line)
        cur = stakes.get(o["bout_slug"])
        row = (o.get("stake"), o.get("champion_defenses_before"), o.get("champion_reign_days"),
               o.get("belt_id"))
        if cur is None or row[0] == "defence": stakes[o["bout_slug"]] = row
    canc = collections.Counter()
    for line in open(DATA / "cancelled_title_bouts.jsonl"):
        o = json.loads(line)
        d = ordi(o.get("event_date") or "")
        if d: canc[(o.get("promotion_id"), o.get("sport"), d // 91)] += 1

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump({"B": B, "ev": ev, "first_seen": first_seen, "first_pro": first_pro,
                     "wi": wi, "stakes": stakes, "canc": canc,
                     "champ_from": champ_from}, f, protocol=5)
    print(f"cached -> {CACHE}", flush=True)
    return {"B": B, "ev": ev, "first_seen": first_seen, "first_pro": first_pro, "wi": wi,
            "stakes": stakes, "canc": canc, "champ_from": champ_from}


# ---------------------------------------------------------------- signals

def median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0

def cell_signals(rows, q, level, first_seen, first_pro, wi, stakes, canc, evd,
                 champ_len, champ_from):
    """Raw signals for one (promotion, sport) over the window ending at quarter start `q`."""
    lo = q - WIN_DAYS
    W = [r for r in rows if lo <= r[2] < q]
    if not W: return None
    sel = [r for r in W if (r[3] if level == "pro" else r[11] == "Amateur")]
    if not sel: return None
    pid, sport = W[0][0], W[0][1]

    # -- intake: what the fighters debuting here already were
    seen, isc = set(), []
    for r in sel:
        for i, (slug, res, ra, nat) in enumerate(r[10]):
            if slug and slug not in seen and first_seen.get((slug, pid, sport)) == r[2]:
                seen.add(slug)
                n, wr = r[12][i]
                isc.append(ALPHA * wr + (1 - ALPHA) * n / (n + K_EXP))
    # -- matchmaking: how far apart the two records were. Only bouts where both men have a
    # record to compare: outside MMA the records are reconstructed from our own cards, so a
    # card of debutants scores a mismatch of zero -- 0-0 against 0-0 is a perfect match --
    # and the signal inverts, ranking the least-known promotions as the best matchmakers.
    mis = [abs(r[12][0][1] - r[12][1][1]) for r in sel
           if r[12][0][0] >= MIN_REC and r[12][1][0] >= MIN_REC]
    # -- elite: how good are this promotion's BEST fighters. Every other signal here is a
    # mean or a count, so a small roster of elite fighters is invisible: RIZIN sits 14th for
    # us and 2nd on Fight Matrix's average fighter rank, which is v1's limitation 3. This
    # reads the top decile of the roster instead of its average, and adds a bonus for having
    # held an established belt anywhere in the sport, so a promotion whose greatness is a few
    # transcendent fighters is not averaged away by the undercard.
    best = {}
    for r in sel:
        for i, (slug, res, ra, nat) in enumerate(r[10]):
            if not slug: continue
            n_, wr = r[12][i]
            qv = ALPHA * wr + (1 - ALPHA) * n_ / (n_ + K_EXP)
            # Champion credit, scaled by how much of a champion: one title win counts, five
            # title bouts count fully. Uncapped on purpose -- with a cap at 1.0 the top decile
            # of every credible promotion saturated and the signal separated majors from the
            # mass (AUC 0.996) while telling RIZIN, the UFC and DWCS apart to two decimals.
            hs = champ_from.get((slug, sport))
            if hs:
                i = bisect.bisect_left(hs, (q,)) - 1
                if i >= 0: qv += CHAMP_BONUS * min(1.0, math.log1p(hs[i][1]) / math.log1p(5))
            if qv > best.get(slug, -1.0): best[slug] = qv
    bv = sorted(best.values(), reverse=True)
    top = bv[:max(5, len(bv) // 10)]
    elite = sum(top) / len(top) if top else None

    # -- roster / breadth
    fset, nats = set(), set()
    for r in sel:
        for (slug, res, ra, nat) in r[10]:
            if slug: fset.add(slug)
            if nat: nats.add(nat)
    # -- consistency: how PREDICTABLY a promotion runs, times log volume. The old density
    # form (months run / months spanned) conflated frequent with continuously operating and
    # punished a deliberate low cadence: ADCC runs biennially like clockwork and scored as
    # dormant. Gaps include the trailing gap to the window end, so a promotion that ran three
    # cards and vanished has one huge final gap and is correctly irregular.
    evs = {r[8] for r in W}
    months = {dt.date.fromordinal(r[2]).year * 12 + dt.date.fromordinal(r[2]).month for r in W}
    span = max(1.0, (max(r[2] for r in W) - min(r[2] for r in W)) / 30.44)
    edates = sorted({r[2] for r in W})
    gaps = [b - a for a, b in zip(edates, edates[1:])] + [q - edates[-1]]
    if len(edates) >= 3:
        mg = sum(gaps) / len(gaps)
        cv = ((sum((x - mg) ** 2 for x in gaps) / len(gaps)) ** 0.5 / mg) if mg else 0.0
        regularity = 1.0 / (1.0 + cv)
    else:
        regularity = min(1.0, len(months) / span)
    cons = regularity * math.log1p(len(evs))
    # -- card architecture
    n_all = len(W)
    pro_share  = sum(1 for r in W if r[3]) / n_all
    champ      = sum(1 for r in W if r[5] and r[5] >= champ_len) / n_all
    prelim_ev  = len({r[8] for r in W if r[6] in ("Prelim", "Postlim")}) / len(evs)
    size       = min(1.0, math.log1p(n_all / len(evs)) / math.log1p(12))
    card = (0.35 * pro_share + 0.25 * champ + 0.20 * prelim_ev + 0.20 * size if level == "pro"
            else (0.25 * champ + 0.20 * prelim_ev + 0.20 * size) / 0.65)
    # -- title integrity. Unlike broadcast or weigh-in data, a belt is printed on the card:
    # a promotion with events and no titles has no championship structure, which is a real
    # deficit, not a coverage gap. So this is gated only when the promotion is too small to
    # judge (< 4 events); above that, no titles scores low rather than dropping out. Without
    # this, Dana White's Contender Series -- no belts by design -- had its title weight
    # redistributed onto intake, where signing undefeated prospects makes it look elite.
    #
    # A tournament final is a title-level bout with no incumbent. 2,032 of the 2,080 pro MMA
    # tournament bouts carry no belt at all -- `belts.py` cannot key the generic string
    # "Tournament Championship" (2,040 of them) to a division -- so they were invisible here,
    # and the PFL, whose season format puts 66 of its 81 title-level bouts in that bucket,
    # was scored on the 15 that remained. They count toward density, and where a promotion's
    # title bouts are mostly tournaments the defence share is dropped rather than scored:
    # a final has nobody to defend against, so a low share measures the format, not churn.
    tb = [stakes[r[9]] for r in sel if r[9] in stakes]
    tourn = [r for r in sel if r[4] == "tournament" and r[9] not in stakes]
    n_title, n_level = len(tb), len(tb) + len(tourn)
    seasonal = n_level and len(tourn) / n_level > 0.5
    title, tfmt = None, ("seasonal" if seasonal else "lineal" if n_level else None)
    if n_level:
        parts, wts = [], []
        if tb and not seasonal:
            parts.append(sum(1 for s_ in tb if s_[0] == "defence") / len(tb)); wts.append(0.30)
        if tb:
            dfs = [s_[1] for s_ in tb if s_[1] is not None]
            rds = [s_[2] for s_ in tb if s_[2] is not None]
            parts.append(min(1.0, (sum(dfs) / len(dfs)) / 3.0) if dfs else 0.0); wts.append(0.20)
            parts.append(min(1.0, median(rds) / 730.0) if rds else 0.0); wts.append(0.15)
            parts.append(1.0 - min(1.0, len({s_[3] for s_ in tb}) / len(evs))); wts.append(0.15)
        parts.append(min(1.0, n_level / len(evs))); wts.append(0.20)   # titles actually staged
        title = sum(p_ * w for p_, w in zip(parts, wts)) / sum(wts)
        if len(tourn) and not tb: tfmt = "seasonal"
    elif len(evs) >= 4:
        title = 0.0
    # -- discipline: did the fighters make weight. The only operations signal here that is
    # not really a coverage proxy -- cancelled-bout counts and named referees say Tapology
    # tracks the promotion closely (the UFC "cancels" 525 bouts to Shooto's 49), so those
    # sit in `dist` with the rest of the attention signals.
    weighed = [wi[r[9]] for r in W if r[9] in wi]
    ops = (1.0 - sum(weighed) / len(weighed)) if len(weighed) >= 10 else None
    # -- distribution / market attention
    bt = [evd[e][2] for e in evs if e in evd and evd[e][3]]
    aw = sum(1 for e in evs if e in evd and evd[e][5]) / len(evs)
    od = sum(1 for r in W if r[7]) / n_all
    rf = sum(1 for e in evs if e in evd and evd[e][7]) / len(evs)
    wp = sum(1 for e in evs if e in evd and evd[e][6]) / len(evs)
    dist = None
    if bt or aw or od or rf or wp:
        dist = (0.40 * (sum(bt) / len(bt) if bt else 0.0) + 0.15 * aw
                + 0.15 * od + 0.15 * rf + 0.15 * wp)
    # -- graduation (amateur track only): did this promotion's amateurs go on to fight pro.
    # An amateur programme's job is to produce professionals, and this is the one signal that
    # measures the output rather than the input.
    grad = None
    if level == "am":
        gk = [(slug, r[2]) for r in sel for (slug, res, ra, nat) in r[10] if slug]
        if len(gk) >= 10:
            grad = sum(1 for slug, d in {(a, b) for a, b in gk}
                       if first_pro.get((slug, sport), 1 << 30) > d) / len({a for a, _ in gk})
    return dict(pid=pid, sport=sport, q=q, level=level, n_bouts=n_all, n_sel=len(sel),
                fset=(fset if level == "am" else None), export=None,
                since_last=q - max(r[2] for r in W),
                ev_365=len({r[8] for r in W if r[2] >= q - 365}),
                tenure_m=(q - rows[0][2]) / 30.44,
                n_pro=sum(1 for r in W if r[3]), n_events=len(evs), grad=grad,
                intake=(sum(isc) / len(isc) if isc else None), n_intake=len(isc),
                mismatch=(sum(mis) / len(mis) if len(mis) >= 10 else None), n_match=len(mis),
                consistency=cons, roster=len(fset), breadth=len(nats),
                elite=elite, card=card, title=title, n_title=n_level, title_format=tfmt,
                ops=ops, dist=dist)


def shrink_cell(c, g):
    """Empirical-Bayes shrinkage toward the sport's global mean. Applied to every cell the
    same way, whether it came off the quarterly grid or an exact date."""
    s = c["sport"]
    c["intake_s"] = shrink(c["intake"], c["n_intake"], M_INTAKE, g[(s, "intake")])
    c["mismatch_s"] = shrink(c["mismatch"], c["n_match"], M_MATCH, g[(s, "mismatch")])
    c["title_s"] = shrink(c["title"], c["n_title"] or c["n_events"], M_TITLE, g[(s, "title")])
    c["grad_s"] = c["grad"]
    return c


def shrink(x, n, m, g):
    if x is None: return None
    return (n * x + m * g) / (n + m)


SIG = ["intake", "elite", "match", "consistency", "card", "title", "ops", "dist", "grad"]


def quarter_of(d):
    """Day ordinal -> the ordinal of the quarter it falls in. Field statistics stay on the
    quarterly grid; a promotion's own window does not."""
    x = dt.date.fromordinal(d)
    return dt.date(x.year, (x.month - 1) // 3 * 3 + 1, 1).toordinal()


def load_overrides():
    """Asserted strengths for competitions the signals cannot see. Every signal in this model
    is a volume signal, and volume is exactly what an Olympic sport does not give us: we hold
    24 Olympic judo bouts and 69 Olympic wrestling bouts, present only because MMA fighters
    competed in them. Scoring those 0.32 does not mean the Olympics is weak, it means we have
    no calendar to measure. For a sport whose terminal competition IS the Games -- no
    professional tier exists above it -- that is a fact about the sport, not about the
    promotion, and it is asserted here rather than mined.

    Deliberately NOT covered: Olympic boxing, because professional boxing sits above it and
    the export ceiling already ranks it first among amateur boxing on evidence; and the 2008
    Beijing sanda bouts, which were a demonstration event, not a medal sport.
    """
    f = DATA / "overrides" / "promotion_strength.jsonl"
    if not f.exists(): return {}
    out = {}
    for line in open(f):
        o = json.loads(line)
        out[(o["promotion_id"], o["sport"], o["level"])] = (ordi(o["from"]), o["norm"], o["why"])
    return out


OVERRIDE = {}


def field_for(field, sport, level, q):
    """The field to normalise against, and the sport whose scaling goes with it."""
    f = field.get((sport, level, q))
    if f is not None: return f, sport
    kin = SPORT_KIN.get(sport)
    if kin: return field.get((kin, level, q)), kin
    return None, sport


def ratios(c, f):
    return {
        "intake": (c["intake_s"] / f["intake"]) if c["intake_s"] and f["intake"] else None,
        "match": (f["mismatch"] / c["mismatch_s"]) if c["mismatch_s"] and f["mismatch"] else None,
        "consistency": c["consistency"] / f["consistency"] if f["consistency"] else None,
        "card": c["card"] / f["card"] if f["card"] else None,
        "elite": (c["elite"] / f["elite"]) if c["elite"] and f.get("elite") else None,
        "title": (c["title_s"] / f["title"]) if c["title_s"] and f["title"] else None,
        "ops": (c["ops"] / f["ops"]) if c["ops"] is not None and f["ops"] else None,
        "dist": (c["dist"] / f["dist"]) if c["dist"] and f["dist"] else None,
        "grad": (c["grad_s"] / f["grad"]) if c["grad_s"] and f.get("grad") else None,
        "roster": c["roster"] / max(1.0, f["roster"]),
        "breadth": max(c["breadth"], 1) / max(1.0, f["breadth"])}


def _zs(c, scal, keys):
    z = {}
    for k in keys:
        r = c["ratio"].get(k)
        if r is None: z[k] = None; continue
        mu, sd = scal[(c.get("nsport", c["sport"]), c["level"], k)]
        z[k] = max(-Z_CLIP, min(Z_CLIP, (math.log(max(r, FLOOR)) - mu) / sd))
    return z


def _composite(c, scal, W, SIG, ks):
    W = AM_WEIGHTS if c["level"] == "am" else W
    z = _zs(c, scal, [k for k in SIG if k in W])
    den = sum(W[k] for k, v in z.items() if v is not None)
    if not den: return None
    zs = _zs(c, scal, ["roster", "breadth"])
    scale = 0.7 * (zs["roster"] or 0.0) + 0.3 * (zs["breadth"] or 0.0)
    comp = sum(W[k] * v for k, v in z.items() if v is not None) / den + ks * scale
    # A promotion seen over eight bouts is not evidence of a top promotion. Shrink the whole
    # composite toward the field median, which is also the right prior: what we do not know
    # about a promotion should read as average, not as excellent. Two dimensions, because 77
    # bouts across nine cards in a single year are not 77 independent observations -- UFC BJJ
    # was shrunk by 0.89 on bout count alone and came top of grappling in its first year.
    # Tenure rather than event count, so a promotion that runs rarely on purpose (fix 3) is
    # not punished twice for the same cadence.
    return (comp * c["n_sel"] / (c["n_sel"] + M_CELL)
            * c["tenure_m"] / (c["tenure_m"] + M_TENURE))


def at(pid, sport, date, level="pro"):
    """strength(promotion, sport, EXACT date, level), computed on demand: the window ends on
    `date` itself (exclusive), against the persisted field statistics for its quarter."""
    C = pickle.load(open(CACHE, "rb"))
    OVERRIDE.update(load_overrides())
    ov = OVERRIDE.get((pid, sport, level))
    if ov and ordi(date) >= ov[0]:
        # An Olympic final has no bouts in its own trailing window -- the previous Games were
        # four years earlier -- so the override has to answer before the signals are asked.
        return {"promotion_id": pid, "sport": sport, "level": level, "date": date,
                "norm": ov[1], "source": "override", "why": ov[2]}
    F = json.load(open(DATA / "promotion_strength_field.json"))
    field = {(k.split("|")[0], k.split("|")[1], ordi(k.split("|")[2])): v
             for k, v in F["field"].items()}
    scal = {tuple(k.split("|")): tuple(v) for k, v in F["scal"].items()}
    A = {tuple(k.split("|")): v for k, v in F["slope"].items()}
    g = {tuple(k.split("|")): v for k, v in F["global"].items()}
    EXPORT_REF[0] = F.get("export_ref", 0.0); EXPORT_REF[1] = F.get("export_ref_n", 1.0)
    EXPORT_ELITE[0] = F.get("export_elite", 0.85)
    rows = sorted((r for r in C["B"] if r[0] == pid and r[1] == sport), key=lambda r: r[2])
    if not rows: return {"promotion_id": pid, "sport": sport, "level": level, "date": date,
                         "norm": NO_DATA, "reason": "promotion has no bouts in this sport"}
    by_sport_min = [r[5] for r in C["B"] if r[1] == sport and r[5]]
    cl = sorted(by_sport_min)[int(0.90 * len(by_sport_min))] if by_sport_min else 1e9
    c = cell_signals(rows, ordi(date), level, C["first_seen"], C["first_pro"], C["wi"],
                     C["stakes"], C["canc"], C["ev"], cl, C["champ_from"])
    if c and level == "am" and EXPORT_REF[0]:
        pro_norm = {(o["promotion_id"], o["sport"], ordi(o["date"])): o["norm"]
                    for o in (json.loads(l) for l in open(DATA / "promotion_strength.jsonl"))
                    if o["level"] == "pro"}
        hn = cell_export(c, build_probest(C["B"], pro_norm))
        c["export"] = ((hn[0] + M_EXPORT * F.get("export_gmean", 0.0)) / (hn[1] + M_EXPORT)) if hn else None
        c["export_n"] = hn[0] if hn else 0
    r = score(shrink_cell(c, g), field, scal, A) if c else None
    return r or {"promotion_id": pid, "sport": sport, "level": level, "date": date,
                 "norm": NO_DATA, "reason": "no bouts in window"}


def query(pid, sport, date, level="pro"):
    """strength(promotion, date, sport): the last quarter computed at or before `date`."""
    best = None
    for line in open(DATA / "promotion_strength.jsonl"):
        o = json.loads(line)
        if (o["promotion_id"] != pid or o["sport"] != sport or o["level"] != level
                or o["date"] > date): continue
        if best is None or o["date"] > best["date"]: best = o
    return best or {"promotion_id": pid, "sport": sport, "level": level,
                    "date": None, "norm": NO_DATA,
                    "reason": "no bouts in window"}


def build_probest(B, pro_norm):
    """fighter -> [(date, best professional norm reached on or before that date)], so an
    amateur cell can ask what its fighters went on to do without looking into its own future."""
    per = collections.defaultdict(list)
    for r in B:
        if not r[3]: continue
        v = pro_norm.get((r[0], r[1], quarter_of(r[2])))
        if v is None: continue
        for (slug, res, ra, nat) in r[10]:
            if slug: per[slug].append((r[2], v))
    out = {}
    for slug, xs in per.items():
        xs.sort()
        run, best = [], 0.0
        for d, v in xs:
            best = max(best, v); run.append((d, best))
        out[slug] = run
    return out


def cell_export(c, probest):
    """Share of this cell's fighters who had reached an ELITE professional promotion strictly
    before the cell's date. Causal: nothing after `q` is consulted.

    The mean best-later promotion, which this used to be, asks "did our fighters turn pro",
    and that is the wrong question: it ranked ten-fighter regional amateur cards (0.46, most
    of them went pro somewhere ordinary) above Olympic boxing (0.19, most Olympians never
    fight professionally at all). What separates an elite amateur programme is not how many
    graduate but whether the graduates reach the top."""
    if c["level"] != "am" or not c["fset"]: return None
    hit = 0
    for slug in c["fset"]:
        xs = probest.get(slug)
        if not xs: continue
        i = bisect.bisect_left(xs, (c["q"],)) - 1
        if i >= 0 and xs[i][1] >= EXPORT_ELITE[0]: hit += 1
    return (hit, len(c["fset"]))


def score(c, field, scal, A):
    """Cell -> output row. The only path from signals to `norm`, so the quarterly table, the
    per-event table and an arbitrary-date query cannot drift apart."""
    f, c["nsport"] = field_for(field, c["sport"], c["level"], quarter_of(c["q"]))
    if not f: return None
    c["ratio"] = ratios(c, f)
    composite = _composite(c, scal, WEIGHTS, SIG, K_SCALE_Z)
    if composite is None: return None
    z = _zs(c, scal, SIG)
    Wt = AM_WEIGHTS if c["level"] == "am" else WEIGHTS
    den = sum(Wt[k] for k, v in z.items() if v is not None and k in Wt)
    quality = sum(Wt[k] * v for k, v in z.items() if v is not None and k in Wt) / den
    zs = _zs(c, scal, ["roster", "breadth"])
    L = 1.0 / (1.0 + math.exp(-A[(c["nsport"], c["level"])] * composite))
    # An amateur card is a lower-quality product than a professional one whatever its
    # promoter does well, so the amateur track is scored against amateur promotions and then
    # mapped into a band that stops below the professional neutral point: the median amateur
    # programme lands on 0.30 -- the same constant the consumer already uses for "no data,
    # assume below average" -- and the best on 0.45.
    ceil = AM_CEIL
    if c.get("export") and EXPORT_REF[0]:
        lift = (min(1.0, c["export"] / EXPORT_REF[0])
                * min(1.0, math.log1p(c.get("export_n", 0)) / math.log1p(EXPORT_REF[1])))
        # An amateur card is normally a lower-quality product, which is what the band encodes.
        # The exception is measurable: a programme whose fighters go on to populate the top
        # professional promotions is not developmental, it IS the elite tier of its
        # discipline -- Olympic boxing is amateur by definition, ranks first of 98 in its
        # field, and was pinned at 0.435 by a flat ceiling. Fenced to the amateur track: the
        # reason export cannot be a general quality term is that it inverts on terminal
        # promotions (DWCS exports at 77%, the UFC at 25%), and an amateur programme is never
        # terminal.
        ceil = AM_CEIL + (AM_CEIL_MAX - AM_CEIL) * lift
    norm = L if c["level"] == "pro" else AM_FLOOR + (ceil - AM_FLOOR) * L
    ov = OVERRIDE.get((c["pid"], c["sport"], c["level"]))
    src = None
    if ov and c["q"] >= ov[0]:
        norm, src = ov[1], "override"
    return {"promotion_id": c["pid"], "sport": c["sport"], "level": c["level"],
            **({"source": src, "computed_norm": round(L if c["level"] == "pro"
                else AM_FLOOR + (ceil - AM_FLOOR) * L, 4)} if src else {}),
            "date": dt.date.fromordinal(c["q"]).isoformat(),
            "norm": round(norm, 4), "composite": round(composite, 3),
            "quality": round(quality, 3),
            "scale": round(0.7 * zs["roster"] + 0.3 * zs["breadth"], 3),
            "z": {k: (round(v, 2) if v is not None else None) for k, v in z.items()},
            "ratios": {k: (round(v, 3) if v is not None else None) for k, v in c["ratio"].items()},
            "raw": {"bouts": c["n_bouts"], "level_bouts": c["n_sel"], "pro": c["n_pro"],
                    "events": c["n_events"], "roster": c["roster"], "breadth": c["breadth"],
                    "title_bouts": c["n_title"], "debutants": c["n_intake"]},
            **({"export": round(c["export"], 3), "export_elite_produced": c.get("export_n", 0)}
               if c.get("export") else {}),
            **({"title_format": c["title_format"]} if c.get("title_format") else {}),
            "activity": {"days_since_last_event": c["since_last"],
                         "events_last_365": c["ev_365"],
                         "tenure_months": round(c["tenure_m"], 1)},
            "field_n": f["n"], "field_thin": f["n"] < FIELD_THIN,
            **({"normalised_against": c["nsport"]} if c["nsport"] != c["sport"] else {})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="PROMOTION_ID,SPORT,YYYY-MM-DD[,pro|am] (quarterly table)")
    ap.add_argument("--at", help="PROMOTION_ID,SPORT,YYYY-MM-DD[,pro|am] (exact date)")
    ap.add_argument("--rebuild-cache", action="store_true")
    ap.add_argument("--sport", action="append", help="limit to these sports")
    ap.add_argument("--min-bouts", type=int, default=4, help="bouts in window to emit a row")
    ap.add_argument("--export-ceiling", action="store_true",
                    help="let an amateur programme exceed the 0.45 ceiling in proportion to "
                         "the professional promotions its fighters later reach")
    ap.add_argument("--min-pro", type=int, default=4,
                    help="bouts at that level in the window to emit a row")
    a = ap.parse_args()
    if a.at:
        p_ = a.at.split(",")
        print(json.dumps(at(int(p_[0]), p_[1], p_[2], p_[3] if len(p_) > 3 else "pro"),
                         indent=1)); return 0
    if a.query:
        parts = a.query.split(",")
        print(json.dumps(query(int(parts[0]), parts[1], parts[2],
                               parts[3] if len(parts) > 3 else "pro"), indent=1)); return 0

    OVERRIDE.update(load_overrides())
    C = build_cache() if (a.rebuild_cache or not CACHE.exists()) else pickle.load(open(CACHE, "rb"))
    B, evd, first_seen, first_pro, wi, stakes, canc, champ_from = (
        C["B"], C["ev"], C["first_seen"], C["first_pro"], C["wi"], C["stakes"], C["canc"],
        C["champ_from"])
    sports = set(a.sport) if a.sport else {r[1] for r in B}

    # championship-length threshold per sport: the 90th percentile of scheduled minutes
    by_sport_min = collections.defaultdict(list)
    for r in B:
        if r[5]: by_sport_min[r[1]].append(r[5])
    champ_len = {s: sorted(v)[int(0.90 * len(v))] for s, v in by_sport_min.items() if v}

    buckets = collections.defaultdict(list)
    for r in B:
        if r[1] in sports: buckets[(r[0], r[1])].append(r)

    print(f"computing cells for {len(buckets):,} (promotion, sport) pairs ...", flush=True)
    cells = []
    for (pid, sport), rows in buckets.items():
        lo, hi = rows[0][2], rows[-1][2] + WIN_DAYS
        for q in quarters(lo, hi):
          for level in ("pro", "am"):
            c = cell_signals(rows, q, level, first_seen, first_pro, wi, stakes, canc, evd,
                             champ_len.get(sport, 1e9), champ_from)
            # An all-amateur promotion has no professional strength to report. Without this
            # the sample-size shrink multiplies a zero pro-bout count and every one of them
            # lands on exactly 0.50 -- the neutral value -- instead of falling through to
            # the no-data default. (The IOC's amateur boxing was scoring 0.50.)
            # An all-amateur promotion has no professional strength to report. Without this
            # the sample-size shrink multiplies a zero count and every one of them lands on
            # exactly 0.50 -- the neutral value -- instead of falling through to the no-data
            # default. (The IOC's amateur boxing was scoring 0.50.)
            if c and c["n_bouts"] >= a.min_bouts and c["n_sel"] >= a.min_pro: cells.append(c)
    print(f"  {len(cells):,} cells", flush=True)

    # global means for shrinkage, per sport
    g = {}
    for s in sports:
        cs = [c for c in cells if c["sport"] == s]
        for k in ("intake", "mismatch", "title", "grad"):
            v = [c[k] for c in cs if c[k]]
            g[(s, k)] = sum(v) / len(v) if v else 0.5
    for c in cells: shrink_cell(c, g)

    # field statistics per (sport, quarter); carried forward when a quarter is too thin
    bykey = collections.defaultdict(list)
    for c in cells: bykey[(c["sport"], c["level"], c["q"])].append(c)
    field, last = {}, {}
    for (s, lv, q) in sorted(bykey, key=lambda k: (k[0], k[1], k[2])):
        F = [c for c in bykey[(s, lv, q)] if c["roster"] >= MINF]
        if len(F) >= 3:
            f = {"intake": median([c["intake_s"] for c in F if c["intake_s"]]),
                 "mismatch": median([c["mismatch_s"] for c in F if c["mismatch_s"]]),
                 "consistency": median([c["consistency"] for c in F]),
                 "card": median([c["card"] for c in F]),
                 "elite": median([c["elite"] for c in F if c["elite"] is not None]),
                 "title": median([c["title_s"] for c in F if c["title_s"]]),
                 "ops": median([c["ops"] for c in F if c["ops"] is not None]),
                 "dist": median([c["dist"] for c in F if c["dist"]]),
                 "grad": median([c["grad"] for c in F if c["grad"] is not None]),
                 "roster": median([c["roster"] for c in F]),
                 "breadth": median([c["breadth"] for c in F]), "n": len(F)}
            field[(s, lv, q)] = last[(s, lv)] = f
        elif (s, lv) in last: field[(s, lv, q)] = last[(s, lv)]
    print(f"  field stats for {len(field):,} (sport, level, quarter) cells", flush=True)

    # Ratios against the field MEDIAN (the mean is dominated by a tail of one-card shows),
    # then a z-score of ln(ratio) against that sport's ratio distribution POOLED OVER ALL
    # QUARTERS. Two reasons: the seven signals have wildly different dynamic ranges -- an
    # unstandardised weighted sum silently hands `consistency` (ratio 1..7) four times the
    # say of `intake` (0.8..1.7) whatever the weights claim -- and pooling the scaling
    # parameters over time, while the ratio itself is era-relative, is what keeps v1's
    # cross-era stance: a 2005 major scores below a 2025 major because it really was
    # further down its own era's field.
    for c in cells:
        f, c["nsport"] = field_for(field, c["sport"], c["level"], c["q"])
        if not f: continue
        c["ratio"] = ratios(c, f)

    scal = {}
    for s_ in sports:
      for lv in ("pro", "am"):
        for k in SIG + ["roster", "breadth"]:
            v = [math.log(max(c["ratio"][k], FLOOR)) for c in cells
                 if c["sport"] == s_ and c["level"] == lv and c.get("ratio") and c["ratio"].get(k)]
            if len(v) < 30: scal[(s_, lv, k)] = (0.0, 1.0); continue
            mu = sum(v) / len(v)
            sd = (sum((x - mu) ** 2 for x in v) / len(v)) ** 0.5 or 1.0
            scal[(s_, lv, k)] = (mu, sd)

    # Slope per sport: the field median sits at composite 0 -> 0.5 by construction, and the
    # slope is set so the 99.9th-percentile promotion-quarter reaches 0.95. Calibrated on
    # cells pooled over ALL quarters, so it is fixed across eras (v1's cross-era stance):
    # a 2005 major scores below a 2025 major when it sat lower in its own era's field.
    comp = collections.defaultdict(list)
    for c in cells:
        if not c.get("ratio"): continue
        c["_c"] = _composite(c, scal, WEIGHTS, SIG, K_SCALE_Z)
        if c["_c"] is not None: comp[(c.get("nsport", c["sport"]), c["level"])].append(c["_c"])
    A, big = {}, [k for k, v in comp.items() if len(v) >= 2000]
    default = {}
    for lv in ("pro", "am"):
        hs = sorted(sorted(comp[k])[int(0.999 * len(comp[k]))] for k in big if k[1] == lv)
        default[lv] = math.log(19.0) / hs[len(hs) // 2] if hs else 1.0
    for k, v in sorted(comp.items()):
        v.sort()
        hi = v[min(len(v) - 1, int(0.999 * len(v)))] or 1.0
        A[k] = (math.log(19.0) / max(hi, 0.5)) if len(v) >= 2000 else default[k[1]]
        print(f"  {k[0]:<22}{k[1]:>4} cells {len(v):>7,}  p50 {v[len(v)//2]:>6.2f}"
              f"  p99 {v[int(.99*len(v))]:>6.2f}  p99.9 {hi:>6.2f}  slope {A[k]:.3f}", flush=True)

    probest, gmean = {}, 0.0
    if a.export_ceiling:
        pro_norm = {}
        for c in cells:
            if c["level"] != "pro": continue
            r = score(c, field, scal, A)
            if r: pro_norm[(c["pid"], c["sport"], c["q"])] = r["norm"]
        pv = sorted(pro_norm.values())
        EXPORT_ELITE[0] = pv[int(0.99 * len(pv))] if pv else 0.85
        probest = build_probest(B, pro_norm)
        raw = [(c, cell_export(c, probest)) for c in cells]
        gm = ([h / n for c, hn in raw if hn for h, n in [hn]])
        gmean = sum(gm) / len(gm) if gm else 0.0
        for c, hn in raw:
            c["export"] = ((hn[0] + M_EXPORT * gmean) / (hn[1] + M_EXPORT)) if hn else None
            c["export_n"] = hn[0] if hn else 0
        ex = sorted(c["export"] for c in cells if c["export"])
        hs = sorted(c["export_n"] for c in cells if c.get("export_n"))
        EXPORT_REF[0] = ex[int(0.999 * len(ex))] if ex else 0.0
        EXPORT_REF[1] = hs[int(0.999 * len(hs))] if hs else 1.0
        print(f"  elite threshold (p99 of professional norms): {EXPORT_ELITE[0]:.3f}", flush=True)
        print(f"  export evidence on {len(ex):,} amateur cells: rate p50 {ex[len(ex)//2]:.3f} "
              f"p99 {ex[int(.99*len(ex))]:.3f} ref {EXPORT_REF[0]:.3f} | elite produced "
              f"p99 {hs[int(.99*len(hs))]} ref {EXPORT_REF[1]}", flush=True)

    STATE.mkdir(exist_ok=True)
    n_out = 0
    with open(DATA / "promotion_strength.jsonl", "w") as out:
        for c in cells:
            r = score(c, field, scal, A)
            if r: out.write(json.dumps(r) + "\n"); n_out += 1
    print(f"wrote {n_out:,} rows -> data/promotion_strength.jsonl")

    # Per-event pass: the same signals with the window ending on the event's own date rather
    # than on its quarter's first day. The quarterly grid is only ever needed for the FIELD
    # statistics -- population aggregates over a 3-year window, which do not move inside a
    # quarter -- so a promotion's own trailing window can end exactly where it should. It is
    # also strictly more causal: a card on 20 December was being scored on data through
    # 1 October, throwing away 80 days of the promotion's own recent history.
    dates = collections.defaultdict(set)
    for r in B:
        if r[1] in sports: dates[(r[0], r[1])].add(r[2])
    n_ev, deltas = 0, []
    qtab = {(r["promotion_id"], r["sport"], r["level"], r["date"]): r["norm"]
            for r in (json.loads(l) for l in open(DATA / "promotion_strength.jsonl"))}
    with open(DATA / "promotion_strength_events.jsonl", "w") as out:
        for (pid, sport), rows in buckets.items():
            for d in sorted(dates[(pid, sport)]):
                for level in ("pro", "am"):
                    c = cell_signals(rows, d, level, first_seen, first_pro, wi, stakes, canc,
                                     evd, champ_len.get(sport, 1e9), champ_from)
                    if not c or c["n_bouts"] < a.min_bouts or c["n_sel"] < a.min_pro: continue
                    if probest:
                        hn = cell_export(c, probest)
                        c["export"] = ((hn[0] + M_EXPORT * gmean) / (hn[1] + M_EXPORT)) if hn else None
                        c["export_n"] = hn[0] if hn else 0
                    r = score(shrink_cell(c, g), field, scal, A)
                    if not r: continue
                    out.write(json.dumps(r) + "\n"); n_ev += 1
                    q = qtab.get((pid, sport, level,
                                  dt.date.fromordinal(quarter_of(d)).isoformat()))
                    if q is not None: deltas.append(abs(r["norm"] - q))
    print(f"wrote {n_ev:,} rows -> data/promotion_strength_events.jsonl")
    if deltas:
        deltas.sort(); n = len(deltas)
        print(f"  exact date vs its quarter: mean |delta| {sum(deltas)/n:.4f}  "
              f"p50 {deltas[n//2]:.4f}  p90 {deltas[9*n//10]:.4f}  p99 {deltas[int(.99*n)]:.4f}  "
              f"max {deltas[-1]:.4f}  (>0.05 on {100*sum(1 for x in deltas if x > .05)/n:.1f}%)")

    n_ovr = 0
    if OVERRIDE:
        seen_q = {(o["promotion_id"], o["sport"], o["level"], o["date"])
                  for o in (json.loads(l) for l in open(DATA / "promotion_strength.jsonl"))}
        seen_e = {(o["promotion_id"], o["sport"], o["level"], o["date"])
                  for o in (json.loads(l) for l in open(DATA / "promotion_strength_events.jsonl"))}
        qf = open(DATA / "promotion_strength.jsonl", "a")
        ef = open(DATA / "promotion_strength_events.jsonl", "a")
        for (pid, sport, level), (frm, val, why) in OVERRIDE.items():
            rs = [r for r in buckets.get((pid, sport), [])
                  if r[2] >= frm and (r[3] if level == "pro" else r[11] == "Amateur")]
            for d, fh, seen in ((quarter_of, qf, seen_q), (lambda x: x, ef, seen_e)):
                for x in sorted({d(r[2]) for r in rs}):
                    key = (pid, sport, level, dt.date.fromordinal(x).isoformat())
                    if key in seen: continue
                    seen.add(key); n_ovr += 1
                    fh.write(json.dumps({"promotion_id": pid, "sport": sport, "level": level,
                                         "date": key[3], "norm": val, "source": "override",
                                         "why": why}) + "\n")
        qf.close(); ef.close()
        print(f"overrides: {len(OVERRIDE)} rules, {n_ovr:,} rows added where nothing was computed")

    # Field statistics, scaling and slopes, so an arbitrary date can be scored later without
    # recomputing the grid.
    with open(DATA / "promotion_strength_field.json", "w") as fh:
        json.dump({"field": {"|".join((k[0], k[1], dt.date.fromordinal(k[2]).isoformat())): v
                             for k, v in field.items()},
                   "scal": {"|".join(k): v for k, v in scal.items()},
                   "slope": {"|".join(k): v for k, v in A.items()},
                   "global": {"|".join(k): v for k, v in g.items()},
                   "export_elite": EXPORT_ELITE[0],
                   "export_ref": EXPORT_REF[0], "export_ref_n": EXPORT_REF[1],
                   "export_gmean": gmean}, fh)
    print("wrote data/promotion_strength_field.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
