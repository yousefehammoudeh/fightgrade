#!/usr/bin/env python3
"""Reconstruct a belt registry from title bouts. Rules: docs/belts.md

  BELT = (owner, division, gender, era, sport)

`sport` is part of the key because a title contested under different rules is a different
championship. ONE Championship awards a Bantamweight title in MMA, another in Muay Thai,
another in kickboxing and another in submission grappling -- all four are printed
"ONE Bantamweight Championship", and without the sport they collapse into one belt with
four concurrent champions. 669 belts spanned more than one sport before this.

An interim title is the same belt, contested while the undisputed champion is unavailable,
so it is NOT a separate key -- it rides on the link as `interim` and the lineage tracks the
two states concurrently (see belt_lineage.py).

`era` splits a belt whose weight shifts and STAYS shifted. Promotions redefine divisions
and keep the name: the UFC's "Middleweight" was 199 lb until 2001 and 185 lb after, and its
"Lightweight" was 170 lb before 2001. Without this the two are one belt and the lineage is
nonsense. Detected from weight_badge, never hardcoded.

Outputs
  data/belts.jsonl        one row per belt, with bout count and date span
  data/bout_belts.jsonl   bout -> belt links (a unification bout yields several)
  logs/belt_worklist.txt  bout slugs on a belt with >= --min-bouts bouts (stage 3b queue)

  python3 derive/belts.py [--min-bouts N]
"""
import argparse, json, collections, re, sys, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA, STATE = ROOT / "data", ROOT / "logs"

TYPO = {"straweight": "strawweight", "crusierweight": "cruiserweight",
        "cruieserweight": "cruiserweight", "bantanweight": "bantamweight",
        "middlweight": "middleweight", "fetherweight": "featherweight",
        "superflyweight": "super flyweight"}
DIVWORD = re.compile(r"\b((?:light|super|junior)\s+)?([a-z]+weight)\b", re.I)
# `63,5kg` is a comma decimal (Brazilian/European cards). Without the comma in the number
# the `\b` lands after it and the match is `5kg` -- a 63.5 kg title read as 5 kg.
NUMW    = re.compile(r"\b(\d+(?:[.,]\d+)?\s*\+?)\s*(lbs?|kg|kgs)\b", re.I)
MEDAL   = re.compile(r"Medal Match|Gold Medal|Bronze Medal", re.I)
STATE_W = re.compile(r"\b(Vacant|Inaugural)\b", re.I)
INTERIM = re.compile(r"\bInterim\b", re.I)
GENDW   = re.compile(r"\b(Women'?s?|Woman|Female|Feamale|Ladies|Men'?s?|Male)\b", re.I)
FEMW    = re.compile(r"\b(Women'?s?|Woman|Female|Feamale|Ladies)\b", re.I)
# expand a truncated division only right before Title/Championship/end -- never touch a
# bare "Light", which is the compound "Light Heavyweight"/"Light Flyweight" 98% of the time
TRUNC   = re.compile(r"\b(Welter|Middle|Feather|Bantam|Fly|Cruiser|Heavy|Straw|Atom)\b"
                     r"(?!\s*weight)(?=\s+(?:Title|Championship|$))", re.I)
CATCH   = re.compile(r"\bcatchweight\b", re.I)
# Kickboxing and Muay Thai have sanctioning bodies exactly like boxing: an ISKA world
# title is one belt whether 31 different promoters stage it. Promotion-scoping mints a
# copy per promoter. Deliberately excludes K-1/GLORY/Enfusion/RISE -- those are
# promotions awarding their own titles, so they stay promotion-scoped.
SANCTIONING = re.compile(r"\b(ISKA|WAKO|WKN|WKA|WPMF|IFMA|WMC|IKF|WKF|WMF|WBC Muay ?Thai)\b", re.I)

# ONE Championship realigned every MEN'S weight limit upward on 2018-01-01 while keeping
# the division names. Tapology names a division by its numeric limit, so from 2018 the same
# ONE belt appears one conventional class HIGHER -- Aung La N Sang's ONE Middleweight title
# is "ONE Middleweight Championship" in 2017 and "ONE Light Heavyweight Championship" in
# 2018. Keyed on the name, one belt splits in two and two belts collide in one key. Shift
# the post-realignment name back down so the key is ONE's own division throughout.
# Heavyweight is deliberately absent: ONE Light Heavyweight (225 lb) and ONE Heavyweight
# (265 lb) both land on conventional "heavyweight", so the two cannot be separated here.
# The women's ladder was never realigned -- ONE's women's Atomweight has been 115 lb
# throughout -- but Tapology names it by that limit too, so ONE Atomweight reads
# "strawweight" and ONE women's Strawweight reads "flyweight" from the very first bout.
# Same shift, no date gate.
ONE_REALIGN_DATE = "2018-01-01"
ONE_SHIFT_DOWN = {"flyweight": "strawweight", "bantamweight": "flyweight",
                  "featherweight": "bantamweight", "lightweight": "featherweight",
                  "welterweight": "lightweight", "middleweight": "welterweight",
                  "light heavyweight": "middleweight"}
ONE_SHIFT_DOWN_F = {"strawweight": "atomweight", "flyweight": "strawweight",
                    "bantamweight": "flyweight", "featherweight": "bantamweight"}


def canon_weight(d):
    """`121`, `121 lbs`, `65 kg`, `65kg` must not be four different divisions.
    A bare number comes from weight_badge, which is always pounds."""
    if d is None:
        return None
    d = str(d).replace(",", ".")            # 63,5kg -> 63.5kg
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(\+?)\s*(lbs?|kgs?)?\s*$", str(d), re.I)
    if not m:
        return str(d)
    unit = (m.group(3) or "lb").lower().rstrip("s")
    unit = "kg" if unit.startswith("kg") else "lb"
    return f"{m.group(1)}{m.group(2)}{unit}"


def normalise(s):
    s = s.replace("*Numeric Weight*", " ")
    s = re.sub(r"\bMuaythai\b", "Muay Thai", s, flags=re.I)
    s = TRUNC.sub(lambda m: m.group(1) + "weight", s)
    for a, b in TYPO.items():
        s = re.sub(r"\b" + a + r"\b", b, s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,&-")


def _clean(x):
    x = re.sub(r"\b(Title|Titles|Championship|Championships|Belt)\b", " ", x, flags=re.I)
    return re.sub(r"[\s,&]+", " ", x).strip(" ,&-").lower()


# "Undisputed X Title" names no sanctioning body -- it is a STATE, like Vacant and Interim,
# meaning the holder has every major belt at once. Keyed literally it becomes a phantom
# `body:undisputed` that says nothing about who sanctions the belt, and it splits a real
# lineage: Shields vs Hammer 2019 sat on `body:undisputed` while the Ring/IBF/WBO belts it
# actually unified sat elsewhere. Expand it to the majors that EXISTED that year -- the WBO
# was founded in 1988 and the IBF in 1983, so a 1977 "undisputed" title is WBC and WBA only.
UNDISPUTED = re.compile(r"^undisputed$", re.I)
MAJORS = (("1988-01-01", ["wbc world", "wba world", "ibf world", "wbo world"]),
          ("1983-01-01", ["wbc world", "wba world", "ibf world"]),
          ("0000-00-00", ["wbc world", "wba world"]))


def majors_for(date):
    for since, bodies in MAJORS:
        if (date or "9999") >= since:
            return bodies
    return MAJORS[-1][1]


def authorities(s):
    """Strip the shared axes, then split a unification string into component belts.
    The division suffix is shared across the parts, so it is removed first."""
    x = STATE_W.sub(" ", s); x = INTERIM.sub(" ", x); x = GENDW.sub(" ", x)
    x = DIVWORD.sub(" ", x); x = NUMW.sub(" ", x)
    parts = [_clean(p) for p in re.split(r"\s*(?:,|&|\band\b)\s*", x)]
    return [p for p in parts if p] or [""]


WEIGHT_TOL = 8.0      # catchweight and weigh-in variance stay inside one division
ERA_CONFIRM = 2       # a shift needs this many further bouts before it counts as an era


def _num(w):
    """Parse the canonicalised weight (`199lb`, `65kg`) to (value, unit). Comparing a kg
    figure against a lb one would invent era cuts, so the unit travels with the value."""
    m = re.match(r"^(\d+(?:\.\d+)?)\+?(lb|kg)$", str(w or ""))
    return (float(m.group(1)), m.group(2)) if m else None


def assign_eras(staged, weights):
    """A promotion that redefines a division while keeping its name produces one belt key
    covering two different divisions. Split on a weight shift that persists."""
    groups = collections.defaultdict(list)
    for i, r in enumerate(staged):
        groups[(r["owner"], r["div"], r["g"], r["sport"])].append(i)
    cuts = 0
    for key, idxs in groups.items():
        idxs.sort(key=lambda i: (staged[i]["date"] or "9999"))
        seq = [(i, weights.get(staged[i]["slug"])) for i in idxs]
        known = [(i, w) for i, w in seq if w]
        for i, _ in seq:
            staged[i]["era"] = 0
        if len(known) < ERA_CONFIRM + 2:
            continue
        era = 0
        base, unit = known[0][1]
        for pos in range(1, len(known)):
            i, (w, u) = known[pos]
            if u != unit:                      # different unit: not comparable, carry on
                staged[i]["era"] = era
                continue
            if abs(w - base) > WEIGHT_TOL:
                nxt = [x for _, (x, xu) in known[pos:pos + ERA_CONFIRM + 1] if xu == u]
                if len(nxt) >= ERA_CONFIRM + 1 and all(abs(x - w) <= WEIGHT_TOL for x in nxt):
                    era += 1; cuts += 1; base = w
            else:
                base = (base * 3 + w) / 4
            staged[i]["era"] = era
        # bouts with no weight inherit the era of the nearest dated neighbour
        last = 0
        for i, w in seq:
            if w: last = staged[i]["era"]
            else: staged[i]["era"] = last
    return cuts


def admit_tournament_finals(links, belts, ev):
    """Early promotions crowned their first champions in a tournament, not a title bout.
    `title_kind == "tournament"` rightly excludes bracket rounds, but it also drops the
    final that founded the lineage -- UFC 16 crowned Pat Miletich, Bellator founded five
    divisions this way, as did DREAM, Sengoku, DEEP, Pancrase and Road FC.

    Admit a tournament final only on Tapology's own evidence: its winner must be the
    recorded holder at that belt's FIRST title bout, same promotion and weight. That is a
    statement from the source, not an inference about what a tournament meant.
    """
    status = {}
    det_path = DATA / "bout_details.jsonl"
    if not det_path.exists():
        return 0
    for line in det_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("belt_status_kind") == "held" and r.get("belt_holder"):
                status[r["bout_slug"]] = r["belt_holder"]

    tour = collections.defaultdict(list)      # (promotion, weight) -> tournament finals
    first_of = {}                             # belt -> its earliest link
    for bid, rows in links_by_belt(links).items():
        rows.sort(key=lambda r: (r["event_date"] or "9999"))
        first_of[bid] = rows[0]
    for line in (DATA / "bouts.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        if b.get("title_kind") != "tournament" or not b.get("bout_slug"):
            continue
        pid, date = ev.get(b["event_slug"], (None, None))
        w = _num(canon_weight(b.get("weight_badge")))
        if not (pid and date and w):
            continue
        win = next((f for f in (b.get("fighters") or []) if f.get("result") == "W"), None)
        if win and win.get("name"):
            tour[(pid, round(w[0]))].append((date, win, b))

    added = 0
    # One tournament crowned ONE champion, so its final may found only one belt. Pat
    # Miletich's UFC 16 win matched both the 170 lb "Lightweight" belt it actually founded
    # and the "Welterweight" belt that title was renamed to in 2001 -- he is the recorded
    # holder at the first bout of each. Give it to the belt that starts soonest after the
    # tournament; the rename is already carried by `continued_from: division_renamed`.
    claimed = set()
    for bid, first in sorted(first_of.items(), key=lambda kv: kv[1]["event_date"] or "9999"):
        holder = status.get(first["bout_slug"])
        if not holder:
            continue
        pid, _ = ev.get(first["event_slug"], (None, None))
        if not pid:
            continue
        w = first.get("_weight")
        if w is None:
            continue
        hl = holder.lower().strip()
        for date, win, b in tour.get((pid, round(w)), []):
            if date >= (first["event_date"] or "0000") or b["bout_slug"] in claimed:
                continue
            n = (win.get("name") or "").lower()
            if n and (hl in n or n.split()[-1] == hl.split()[-1]):
                claimed.add(b["bout_slug"])
                links.append({"bout_slug": b["bout_slug"], "belt_id": bid,
                              "event_slug": b["event_slug"], "event_date": date,
                              "sport": b.get("sport_base"), "title_bout": b.get("title_bout"),
                              "interim": False, "admitted": "tournament_final"})
                belts[bid].append((None, b["bout_slug"], date, b.get("sport_base"), False))
                added += 1
                break
    return added


def links_by_belt(links):
    out = collections.defaultdict(list)
    for r in links:
        out[r["belt_id"]].append(r)
    return out


def belt_id(key):
    return "b" + hashlib.sha1("|".join(str(k) for k in key).encode()).hexdigest()[:14]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tournament-finals", action="store_true",
                    help="skip admitting tournament finals that crowned a champion")
    ap.add_argument("--min-bouts", type=int, default=2,
                    help="worklist keeps bouts on a belt with at least this many bouts")
    args = ap.parse_args()

    gender, gsource = {}, {}
    for line in (DATA / "fighter_gender.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            gender[r["fighter_slug"]] = r["gender"]
            gsource[r["fighter_slug"]] = r.get("gender_source")
    ev = {}
    for line in (DATA / "event_details.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            ev[d["event_slug"]] = ((d.get("promotion") or {}).get("promotion_id"),
                                   d.get("event_date_index"))

    links, belts, rej = [], collections.defaultdict(list), collections.Counter()
    staged = []
    for line in (DATA / "bouts.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        if b.get("title_kind") != "title" or not b.get("title_bout"):
            continue
        raw, sport, slug = b["title_bout"], b.get("sport_base"), b.get("bout_slug")
        if not slug:                                   rej["no_bout_slug"] += 1; continue
        if MEDAL.search(raw):                          rej["medal_match"] += 1; continue
        if re.search(r"Championships\b", raw) and re.search(r"\b(19|20)\d{2}\b", raw):
            rej["championship_event"] += 1; continue
        # a catchweight is a negotiated one-off, not a division -- see docs/belts.md
        if CATCH.search(raw):                          rej["catchweight"] += 1; continue
        pid, date = ev.get(b["event_slug"], (None, None))
        s = normalise(raw)
        fs = (b.get("fighters") or [])
        gs = [gender.get(f.get("fighter_slug")) for f in fs]
        # One F used to be enough to make the belt women's. A single mis-propagated gender
        # then captures a men's belt: `72396-dejdamrong-sor-amnuaysirichoke` is tagged F by
        # graph propagation, which moved ONE's men's Strawweight title onto the women's
        # key. Require corroboration -- the title string, or an F that was actually
        # observed rather than propagated -- before a men's opponent is overruled.
        hard_f = any(gender.get(f.get("fighter_slug")) == "F"
                     and gsource.get(f.get("fighter_slug")) != "propagated" for f in fs)
        if "F" in gs and ("M" not in gs or hard_f or FEMW.search(raw)):
            g = "F"
        else:
            g = "M" if "M" in gs else ("F" if FEMW.search(raw) else None)
        m = DIVWORD.search(s)
        named = re.sub(r"\s+", " ", m.group(0)).strip().lower() if m else None
        if not named:
            m2 = NUMW.search(s)
            named = canon_weight(m2.group(0)) if m2 else None
        badge = canon_weight(b.get("weight_badge"))
        # division NAME first: keying on the weight fragments a belt across weigh-in
        # variance. Fall back to the badge only when the string names no division --
        # and prefer the name this owner uses at that weight, so "UFC Championship" at
        # 155 lb joins "UFC Lightweight Championship" instead of forming its own belt.
        # A title that names no division and carries no weigh-in badge is an OPENWEIGHT
        # title, not a parse failure: the UFC Superfight Championship is the whole of Ken
        # Shamrock's reign. Dropping it lost five bouts and left UFC 12 -- the one bout
        # that happened to carry a 265 badge -- stranded on a "265lb" key of its own.
        div = named or badge or "openweight"
        interim = bool(INTERIM.search(raw))
        # Tapology marks a fighter who missed weight "Ineligible to Win Belt" in the
        # per-side title row. They can win the FIGHT but not the BELT, so the lineage must
        # not hand it to them -- this is Yoel Romero at UFC 221 and Ilia Topuria at Cage
        # Warriors 94. The signal lives only in `comparison`, so carry it on the link.
        tcmp = (b.get("comparison") or {}).get("title") or {}
        inelig = []
        for side, side_key in ((0, "left"), (1, "right")):
            # "Ineligible to WIN" and "Ineligible to LOSE" are opposites: the second is the
            # champion of an opponent who missed weight, and he keeps the belt either way.
            if "ineligible to win" in str(tcmp.get(side_key) or "").lower():
                fs = (b.get("fighters") or [])
                if side < len(fs) and fs[side].get("fighter_slug"):
                    inelig.append(fs[side]["fighter_slug"])
        is_box = "Boxing" in (sport or "")
        auth = authorities(s)
        if is_box and len(auth) == 1 and UNDISPUTED.match(auth[0]):
            auth = majors_for(date)
            rej["undisputed expanded to the majors of its era"] += 1
        for a in auth:
            # Test EACH authority fragment, not the whole string: in a unification like
            # "QP, WKN 60kg Titles" only the WKN half is a sanctioning body. Prefixing
            # both makes the promoter's own belt promotion-agnostic and merges unrelated
            # promotions that share an acronym.
            if is_box or SANCTIONING.search(a):
                owner = "body:" + a          # authority from the string; promoter ignored
            else:
                owner = f"pid:{pid}|{a}" if a else f"pid:{pid}"
            staged.append({"owner": owner, "named": named, "badge": badge, "div": div,
                           "g": g, "interim": interim, "slug": slug, "pid": pid,
                           "event_slug": b["event_slug"], "date": date,
                           "sport": sport, "raw": raw, "ineligible": inelig,
                           "fs": [f.get("fighter_slug") for f in fs
                                  if f.get("fighter_slug")]})

    weights = {r["slug"]: _num(r["badge"]) for r in staged}
    # pass 2: within an owner, a weight that also appears with a division NAME adopts it
    name_at = collections.defaultdict(collections.Counter)
    for r in staged:
        if r["named"] and not re.match(r"^\d", r["named"]) and r["badge"]:
            name_at[(r["owner"], r["badge"])][r["named"]] += 1
    merged = 0
    for r in staged:
        if not r["named"] and r["badge"]:
            cands = name_at.get((r["owner"], r["badge"]))
            if cands:
                r["div"] = cands.most_common(1)[0][0]
                merged += 1
    print(f"numeric divisions merged into a named one: {merged:,}")

    # pass 2b: an owner that never names a division anywhere is an undivided title. Any
    # weigh-in badge on one of its bouts is a weigh-in, not a division, and keying on it
    # strands that bout on a numeric key of its own -- so fold the whole owner to
    # openweight. Gated on the owner actually HAVING an unbadged bout, so a body that
    # merely omits the division from its title string is left alone.
    by_owner = collections.defaultdict(list)
    for r in staged:
        by_owner[r["owner"]].append(r)
    ow = 0
    for owner, rs in by_owner.items():
        if any(r["named"] for r in rs) or not any(r["div"] == "openweight" for r in rs):
            continue
        for r in rs:
            if r["div"] != "openweight":
                r["div"] = "openweight"; ow += 1
    print(f"numeric divisions folded into an undivided (openweight) title: {ow:,}")

    # pass 2b2: gender from the BELT, where the opponent graph had nothing to say.
    # `fighter_population.py` propagates gender along the OPPONENT graph and, by its own
    # account, every fighter it leaves unlabelled sits in a component with no seed at all --
    # so no amount of further propagation on that graph helps. A title bout also sits on a
    # BELT, which is a graph that method never walked, and the two reinforce each other:
    # a belt does not change gender, and a fighter who appears on men's belts is a man.
    # So alternate the two until neither moves.
    #
    #   belt  -> bout : an unlabelled bout at an (owner, division, sport) whose labelled
    #                   bouts are all one gender is that gender
    #   bout  -> fighter : a fighter whose labelled bouts are unanimous takes that gender
    #   fighter -> bout : re-run the normal any-F-else-M rule with the new labels
    #
    # Both directions are guarded to UNANIMITY, so a promotion running a men's and a
    # women's belt at the same division, or a fighter with contradictory evidence, is left
    # alone rather than guessed at.
    fg = dict(gender)
    gfill = ffill = 0
    for _ in range(10):
        moved = 0
        sib = collections.defaultdict(collections.Counter)
        for r in staged:
            if r["g"]:
                sib[(r["owner"], r["div"], r["sport"])][r["g"]] += 1
        for r in staged:
            if r["g"] is None:
                c = sib.get((r["owner"], r["div"], r["sport"]))
                if c and len(c) == 1:
                    r["g"] = next(iter(c)); gfill += 1; moved += 1
        byf = collections.defaultdict(collections.Counter)
        for r in staged:
            if r["g"]:
                for f in r["fs"]:
                    byf[f][r["g"]] += 1
        for f, c in byf.items():
            if not fg.get(f) and len(c) == 1:
                fg[f] = next(iter(c)); ffill += 1; moved += 1
        for r in staged:
            if r["g"] is None:
                gs2 = [fg.get(f) for f in r["fs"]]
                if "F" in gs2:
                    r["g"] = "F"; moved += 1
                elif "M" in gs2:
                    r["g"] = "M"; moved += 1
        if not moved:
            break
    print(f"gender recovered from the belt/fighter graph: {gfill:,} bouts, "
          f"{ffill:,} fighters")
    # Persist it. This used to live only in this function's memory, so every other consumer
    # -- the cancelled-bout path most visibly -- kept reading the stale fighter_gender.jsonl
    # and reported "unknown" for a fighter whose gender the belt graph had just settled.
    with (DATA / "fighter_gender_belt.jsonl").open("w", encoding="utf-8") as fh:
        for f, g in sorted(fg.items()):
            if g and not gender.get(f):
                fh.write(json.dumps({"fighter_slug": f, "gender": g,
                                     "gender_source": "belt"}, ensure_ascii=False) + "\n")

    # pass 2c: undo ONE Championship's 2018 men's weight realignment (see ONE_SHIFT_DOWN)
    one = 0
    for r in staged:
        if r["pid"] != 61:
            continue
        if r["g"] == "F":
            nd = ONE_SHIFT_DOWN_F.get(r["div"])
        elif (r["date"] or "") >= ONE_REALIGN_DATE:
            nd = ONE_SHIFT_DOWN.get(r["div"])
        else:
            nd = None
        if nd:
            r["div"] = nd; r["badge"] = None; one += 1
    print(f"ONE men's bouts re-keyed to ONE's own division (2018 realignment): {one:,}")

    # pass 3: divisions are final, so eras can be cut before the key is formed
    era_cuts = assign_eras(staged, {r["slug"]: _num(r["badge"]) for r in staged})
    print(f"belts split on a sustained weight shift (division redefined): {era_cuts:,}")

    for r in staged:
        key = (r["owner"], r["div"], r["g"], r["era"], r["sport"])
        bid = belt_id(key)
        wv = _num(r["badge"])
        links.append({"bout_slug": r["slug"], "belt_id": bid, "event_slug": r["event_slug"],
                      "event_date": r["date"], "sport": r["sport"], "title_bout": r["raw"],
                      "interim": r["interim"], "ineligible": r["ineligible"],
                      "_weight": round(wv[0]) if wv else None})
        belts[bid].append((key, r["slug"], r["date"], r["sport"], r["interim"]))

    # ---- manual overrides -------------------------------------------------------
    # Tapology names The Ring on only 9 of the 24 women's Ring title fights we hold: it
    # bills Shields vs Hammer and Shields vs Marshall as "Undisputed", which expands to the
    # four sanctioning bodies and carries no Ring component. The Ring is a separate merit
    # title, so there is nothing in the string to infer it from -- it needs ground truth.
    # Each row cites its source and is applied only to a bout we already hold.
    ovr = DATA / "overrides" / "title_belts.jsonl"
    n_ovr = 0
    if ovr.exists():
        by_slug = collections.defaultdict(list)
        for r in links:
            by_slug[r["bout_slug"]].append(r)
        for line in ovr.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            base = by_slug.get(o["bout_slug"])
            if not base:
                continue                       # never invent a bout we do not hold
            key = ("body:" + o["add_authority"], o["division"], o["gender"], 0,
                   base[0].get("sport"))
            bid = belt_id(key)
            if any(r["belt_id"] == bid for r in base):
                continue
            links.append({"bout_slug": o["bout_slug"], "belt_id": bid,
                          "event_slug": base[0]["event_slug"],
                          "event_date": base[0]["event_date"], "sport": base[0].get("sport"),
                          "title_bout": base[0]["title_bout"], "interim": False,
                          "ineligible": [], "override": o.get("source")})
            belts[bid].append((key, o["bout_slug"], base[0]["event_date"],
                               base[0].get("sport"), False))
            n_ovr += 1
    print(f"manual title-belt overrides applied: {n_ovr:,}")

    if not args.no_tournament_finals:
        n = admit_tournament_finals(links, belts, ev)
        print(f"tournament finals admitted as title bouts: {n:,}")

    with (DATA / "bout_belts.jsonl").open("w", encoding="utf-8") as f:
        for r in links:
            f.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                               ensure_ascii=False) + "\n")
    with (DATA / "belts.jsonl").open("w", encoding="utf-8") as f:
        for bid, rows in sorted(belts.items()):
            key = rows[0][0]
            dates = sorted(d for _, _, d, _, _ in rows if d)
            f.write(json.dumps({"belt_id": bid, "owner": key[0], "division": key[1],
                                "gender": key[2], "era": key[3], "sport": key[4],
                                "bouts": len(set(s for _, s, _, _, _ in rows)),
                                "interim_bouts": len({s for _, s, _, _, it in rows if it}),
                                "sports": sorted({sp for _, _, _, sp, _ in rows if sp}),
                                "first_bout": dates[0] if dates else None,
                                "last_bout": dates[-1] if dates else None},
                               ensure_ascii=False) + "\n")
    keep = {bid for bid, rows in belts.items()
            if len({s for _, s, _, _, _ in rows}) >= args.min_bouts}
    work = sorted({r["bout_slug"] for r in links if r["belt_id"] in keep})
    (STATE / "belt_worklist.txt").write_text("\n".join(work) + "\n")

    print(f"rejected: {dict(rej)}")
    print(f"belt-links {len(links):,} | belts {len(belts):,}")
    print(f"  belts with >= {args.min_bouts} bouts : {len(keep):,}")
    print(f"  worklist bouts                : {len(work):,}  -> logs/belt_worklist.txt")
    print(f"  est. crawl at 33.8 bouts/min  : {len(work)/33.8/60:.1f} h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
