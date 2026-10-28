#!/usr/bin/env python3
"""Reconstruct belt lineages: who held what, when, and how each reign began and ended.

Two independent signals are combined, which is what makes this checkable:

  RECORDED  `Belt Status Before Fight` from the bout page (stage 3b) -- Tapology's own
            statement of who held the belt going in.
  DERIVED   the winner of the previous bout for the same belt.

Where both exist they should agree. Disagreements are reported, not silently resolved:
they mark a belt that changed hands outside the ring (stripped, vacated, promoted from
interim) or a gap in the data.

Outputs
  data/belt_reigns.jsonl     one row per reign
  logs/lineage_audit.log     agreement rate and the disagreements

  python3 scrapers/derive/belt_lineage.py
"""
import json, collections, sys, re, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA, STATE = ROOT / "data", ROOT / "logs"


def load():
    det = {}
    for line in (DATA / "bout_details.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); det[r["bout_slug"]] = r
    linked = set()
    for line in (DATA / "bout_belts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            linked.add(json.loads(line)["bout_slug"])
    bouts = {}
    for line in (DATA / "bouts.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        # keep every bout on a belt, crawled or not: the stage-3b worklist only queued
        # belts with >= 2 bouts, so a belt whose whole life is one title fight (every
        # PFL lineal belt, 10.5k belts overall) had no crawled bout and no reign at all
        if b.get("bout_slug") in det or b.get("bout_slug") in linked:
            bouts[b["bout_slug"]] = b
    belts = {}
    for line in (DATA / "belts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); belts[r["belt_id"]] = r
    links = collections.defaultdict(list)
    for line in (DATA / "bout_belts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); links[r["belt_id"]].append(r)
    return det, bouts, belts, links


def holder_slug(holder, bout):
    """`Held by Liston` is a surname. Resolve it against the two fighters in the bout --
    14,538 of 14,539 recorded holders are one of the two."""
    if not holder or not bout:
        return None, None
    h = holder.lower().strip()
    for f in (bout.get("fighters") or []):
        n = (f.get("name") or "").lower()
        if not n:
            continue
        if h in n or n.split()[-1] == h.split()[-1]:
            return f.get("fighter_slug"), f.get("name")
    return None, None


def gap_days(a, b):
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except Exception:
        return None


def gap_bucket(g):
    if g is None: return "unknown"
    return ("<90d" if g < 90 else "90-365d" if g < 365 else "1-2y" if g < 730
            else "2-5y" if g < 1825 else "5y+")


def winner_of(bout):
    for f in (bout.get("fighters") or []):
        if f.get("result") == "W":
            return f.get("fighter_slug"), f.get("name")
    return None, None


def belt_weight(links_for_belt, weights):
    ws = [weights.get(r["bout_slug"]) for r in links_for_belt]
    ws = [w for w in ws if w]
    return sorted(ws)[len(ws) // 2] if ws else None


def link_continuations(reigns, belts, links):
    """A reign that begins with no bout usually continues one that just ended: an interim
    champion promoted, a belt carried across a division rename, or a champion inherited
    when the parent absorbed another promotion. Link only on tight evidence -- the loose
    version mostly finds fighters who simply held two belts at once.

    Requires in every case that the PREDECESSOR BELT IS DEAD. A predecessor still holding
    bouts afterwards means the fighter held two belts, not that one became the other.
    """
    import datetime
    weights = {}
    for line in (DATA / "bouts.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        m = re.match(r"^(\d+(?:\.\d+)?)$", str(b.get("weight_badge") or ""))
        if b.get("bout_slug") and m:
            weights[b["bout_slug"]] = float(m.group(1))
    bw = {bid: belt_weight(rows, weights) for bid, rows in links.items()}
    own = collections.defaultdict(set)
    for line in (DATA / "event_details.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        pid = (d.get("promotion") or {}).get("promotion_id")
        if pid and d.get("ownership"):
            own[pid].add(d["ownership"])

    def pid_of(bid):
        o = (belts.get(bid) or {}).get("owner", "")
        if o.startswith("pid:"):
            t = o.split("|")[0][4:]
            return int(t) if t.isdigit() else None

    def days(a, b):
        try:
            return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
        except Exception:
            return None

    byfighter = collections.defaultdict(list)
    for r in reigns:
        if r.get("champion_slug"):
            byfighter[r["champion_slug"]].append(r)
    linked = collections.Counter()
    for r in reigns:
        if r.get("began") != "not_in_a_bout":
            continue
        start = r.get("first_seen_date")
        me = belts.get(r["belt_id"], {})
        best = None
        for p in byfighter.get(r["champion_slug"], []):
            if p is r:
                continue
            # A reign that never ended ran at least to the last bout on its belt. Dating it
            # from its START instead put Pat Miletich's 1998 reign 1,147 days before the
            # 2001 renamed belt and outside the window, so the rename never linked.
            end = (p.get("lost_at_date")
                   or belts.get(p["belt_id"], {}).get("last_bout")
                   or p.get("first_seen_date"))
            g = days(end, start) if (end and start) else None
            if g is None or g < 0 or g > 1100:
                continue
            ob = belts.get(p["belt_id"], {})
            same_belt = p["belt_id"] == r["belt_id"]
            if not same_belt and (ob.get("last_bout") or "9999") > start:
                continue                      # predecessor still active: two belts, not one
            kind = None
            if same_belt and p.get("title") != r.get("title"):
                kind = "interim_promoted"
            elif not same_belt and ob.get("owner") == me.get("owner"):
                kind = "era_carryover"
            elif not same_belt and pid_of(p["belt_id"]) == pid_of(r["belt_id"]):
                w1, w2 = bw.get(p["belt_id"]), bw.get(r["belt_id"])
                if w1 and w2 and abs(w1 - w2) <= 10:
                    kind = "division_renamed"
            elif not same_belt:
                a, b_ = pid_of(p["belt_id"]), pid_of(r["belt_id"])
                if (a and b_ and (own.get(a, set()) & own.get(b_, set()))
                        and ob.get("division") == me.get("division")
                        and ob.get("gender") == me.get("gender")):
                    kind = "promotion_absorbed"
            if kind and (best is None or g < best[0]):
                best = (g, p, kind)
        if best:
            g, p, kind = best
            r["continued_from"] = {"belt_id": p["belt_id"], "kind": kind,
                                   "reign_started": p.get("won_at_date") or p.get("first_seen_date"),
                                   "gap_days": g}
            r["began"] = kind
            linked[kind] += 1
    print("\nreign continuations linked:")
    for k, v in linked.most_common():
        print(f"  {v:>5,}  {k}")
    print(f"  {sum(1 for r in reigns if r.get('began') == 'not_in_a_bout'):>5,}  still unlinked")
    return linked


UNAWARDED_MAX_GAP = 200      # days; beyond this the belt lapsed and was genuinely re-won


def drop_unawarded(reigns, links, bouts, audit):
    """You cannot contest a vacant title you already hold.

    When the same fighter wins the same belt in two consecutive VACANT-title bouts with no
    defence between them, the first bout did not award the belt -- almost always because
    the winner missed weight. Tapology usually flags that as `Ineligible to Win Belt`, but
    not always: at UFC Fight Night 169 it records a plain `Vacant`/`Vacant`, so Deiveson
    Figueiredo reads as champion from February 2020 when the title was in fact not awarded
    until the July rematch.

    Bounded by `UNAWARDED_MAX_GAP`. A long gap is the other story entirely -- a promotion
    let the belt lapse and the same fighter won it again years later, which is two real
    reigns, so those are left alone.
    """
    vac, win = {}, {}
    for rows in links.values():
        for r in rows:
            vac[(r["belt_id"], r["bout_slug"])] = bool(
                re.search(r"\b(Vacant|Inaugural)\b", r.get("title_bout") or "", re.I))
    for slug, b in bouts.items():
        for f in (b.get("fighters") or []):
            if f.get("result") == "W":
                win[slug] = f.get("fighter_slug")
    out = []
    for r in reigns:
        w, lost = r.get("won_at_bout"), r.get("lost_at_bout")
        if (r["began"] == "vacant" and r["defenses"] == 0 and w and lost
                and vac.get((r["belt_id"], w)) and vac.get((r["belt_id"], lost))
                and win.get(lost) == r["champion_slug"]
                and (gap_days(r["won_at_date"], r["lost_at_date"]) or 9999) <= UNAWARDED_MAX_GAP):
            audit["reign dropped: vacant title was not awarded"] += 1
            continue
        out.append(r)
    return out


def main():
    det, bouts, belts, links = load()
    reigns, audit = [], collections.Counter()
    bygap = collections.defaultdict(lambda: [0, 0])
    disagreements = []

    VACANT_W = re.compile(r"\b(Vacant|Inaugural)\b", re.I)
    belts_of = collections.defaultdict(list)
    for bid, rws in links.items():
        for r in rws:
            belts_of[r["bout_slug"]].append(bid)
    for belt_id, rows in links.items():
        # An uncrawled bout has no `Belt Status Before Fight`, but the card still names the
        # title, and "Vacant"/"Inaugural" in that name is the same statement. Admit it with
        # evidence `uncrawled` so it stays filterable rather than dropping the bout.
        rows = [r for r in rows if r["bout_slug"] in bouts]
        for r in rows:
            if r["bout_slug"] not in det:
                det[r["bout_slug"]] = {
                    "bout_slug": r["bout_slug"], "belt_holder": None,
                    "belt_status_kind": ("vacant" if VACANT_W.search(r.get("title_bout") or "")
                                         else "absent"),
                    "_uncrawled": True}
                audit["uncrawled bout admitted from the card"] += 1
        if not rows:
            audit["belts with no bout at all"] += 1
            continue
        rows.sort(key=lambda r: (r["event_date"] or "9999", r["bout_slug"]))
        # A one-bout belt has no lineage to speak of, but it still crowns someone -- and a
        # later belt may continue FROM it (Pulver's UFC 30, Coleman's Superfight title).
        audit["belts with a lineage" if len(rows) >= 2 else "single-bout belts"] += 1

        # An interim belt is the SAME belt contested while the undisputed champion is
        # unavailable, so the two run concurrently: an interim win does not displace the
        # undisputed champion, and an undisputed bout ends any open interim reign
        # (unification, or the interim simply lapsing).
        derived = {"undisputed": None, "interim": None}
        cur = {"undisputed": None, "interim": None}
        prev_date = None

        def close(track, slug, date):
            if cur[track]:
                cur[track]["lost_at_bout"], cur[track]["lost_at_date"] = slug, date
                reigns.append(cur[track]); cur[track] = None

        for i, r in enumerate(rows):
            slug = r["bout_slug"]
            d, b = det[slug], bouts.get(slug)
            track = "interim" if r.get("interim") else "undisputed"
            rec_slug, rec_name = holder_slug(d.get("belt_holder"), b)
            kind = d.get("belt_status_kind")
            # `Belt Status Before Fight` is one value for the whole bout, but a unification
            # puts several belts on the line with DIFFERENT statuses. Thibeault vs Robinson
            # says "Held by Robinson" -- true of the IBF and WBO belts, false of the Ring
            # title, vacant since Shields left it. A holder cannot be defending a belt whose
            # first bout in the corpus is this one, so refuse the status there.
            if (kind == "held" and i == 0 and len(belts_of.get(slug, ())) > 1):
                kind, rec_slug, rec_name = "vacant", None, None
                audit["held status refused: belt's first bout in a unification"] += 1
            # A card that names the bout "Vacant ... Championship" is stating the belt was
            # vacant, and it agrees with the crawled status 7,445 times against 79
            # conflicts. Where the page carries NO status (1,093 bouts) that marker is the
            # only evidence there is, and without it the previous winner is wrongly carried
            # forward -- most of ACA's and LFA's vacant-title wins read as `defeated_champion`.
            # `held` is left alone: it is the more specific statement.
            if kind == "absent" and VACANT_W.search(r.get("title_bout") or ""):
                kind = "vacant"
                audit["vacancy taken from the card (page had no status)"] += 1
            # With no recorded status, a title bout that the DERIVED champion is not even
            # fighting in cannot be a defence: either he vacated or our chain is stale.
            # Either way the winner did not beat the champion, so open the reign as vacant
            # rather than inventing a `defeated_champion`. This is most of ACA's and LFA's
            # vacated titles, where the card says only "ACA Welterweight Championship".
            elif kind == "absent" and derived[track] and b:
                here = {f.get("fighter_slug") for f in (b.get("fighters") or [])}
                if derived[track] not in here:
                    kind = "vacant"
                    audit["champion absent from his own title bout -> vacant"] += 1
            gb = gap_bucket(gap_days(prev_date, r["event_date"]) if prev_date else None)

            if kind == "held" and rec_slug and derived[track]:
                if rec_slug == derived[track]:
                    audit["AGREE"] += 1; bygap[gb][0] += 1
                else:
                    audit["DISAGREE"] += 1; bygap[gb][1] += 1
                    disagreements.append({"belt_id": belt_id, "bout_slug": slug,
                                          "date": r["event_date"], "track": track,
                                          "recorded": d.get("belt_holder"),
                                          "derived_slug": derived[track]})
            elif kind == "vacant" and derived[track]:
                audit["vacant despite a previous winner (stripped/vacated)"] += 1
            elif kind == "held" and not derived[track]:
                audit["recorded holder, nothing derived yet"] += 1
            elif kind == "absent":
                audit["status absent"] += 1

            incoming = rec_slug if kind == "held" and rec_slug else (
                None if kind == "vacant" else derived[track])
            if cur[track] and cur[track]["champion_slug"] != incoming:
                close(track, slug, r["event_date"])
            if incoming and not cur[track]:
                cur[track] = {"belt_id": belt_id, "title": track,
                              "champion_slug": incoming, "champion_name": rec_name,
                              "won_at_bout": None, "won_at_date": None,
                              # no bout crowned them -- promoted from interim, unified, or
                              # already champion when our coverage of this belt begins
                              "began": "not_in_a_bout", "first_seen_date": r["event_date"],
                              "evidence": ("uncrawled" if d.get("_uncrawled")
                                           else "recorded" if rec_slug else "derived"),
                              "gap_to_previous": gb, "defenses": 0,
                              "lost_at_bout": None, "lost_at_date": None}
            if cur[track] and cur[track]["champion_slug"] == incoming:
                cur[track]["defenses"] += 1

            w_slug, w_name = winner_of(b)
            # A fighter who missed weight is "Ineligible to Win Belt": they can win the
            # fight but not the title, so the belt does not move. Yoel Romero beat Luke
            # Rockhold at UFC 221 and the interim title stayed vacant; without this the
            # lineage crowns him. Same for Topuria (CW 94), Koike (RIZIN 43), Piersma
            # (LFA 233), and it is why Oliveira's reign ends at UFC 274, not after it.
            if w_slug and w_slug in (r.get("ineligible") or []):
                audit["winner ineligible for the belt (missed weight)"] += 1
                if cur[track] and cur[track]["champion_slug"] == w_slug:
                    # the CHAMPION missed weight: they were stripped at the weigh-in, so
                    # the reign ends here and the belt is vacant going forward
                    close(track, slug, r["event_date"])
                    derived[track] = None
                prev_date = r["event_date"]
                continue
            # An admitted tournament final IS the crowning bout: treat the belt as vacant
            # going in, so the winner opens a reign instead of inheriting one.
            if r.get("admitted") == "tournament_final" and kind != "held":
                kind = "vacant"
            if w_slug:
                if not cur[track] or cur[track]["champion_slug"] != w_slug:
                    close(track, slug, r["event_date"])
                    cur[track] = {"belt_id": belt_id, "title": track,
                                  "champion_slug": w_slug, "champion_name": w_name,
                                  "won_at_bout": slug, "won_at_date": r["event_date"],
                                  "first_seen_date": r["event_date"],
                                  # An inaugural title bout has no champion to defeat.
                                  # With nothing derived and no recorded holder the belt
                                  # was not being defended, so the reign begins vacant --
                                  # Frank Shamrock at UFC 16, every PFL lineal belt, and
                                  # the first bout of most RIZIN and KSW divisions.
                                  "began": ("vacant" if kind == "vacant"
                                            or not (derived[track] or rec_slug)
                                            else "defeated_champion"),
                                  "evidence": ("uncrawled" if d.get("_uncrawled") else
                                               "recorded" if kind == "held" and rec_slug
                                               else "derived"),
                                  "gap_to_previous": gb, "defenses": 0,
                                  "lost_at_bout": None, "lost_at_date": None}
                derived[track] = w_slug
                # An undisputed bout resolves the division only if the INTERIM CHAMPION is
                # in it -- that is the unification. An undisputed title fight he is not
                # part of leaves his interim reign running: Lesnar beat Couture at UFC 91
                # while Nogueira was still interim champion and defended it against Mir six
                # weeks later. Closing on any undisputed bout erased Nogueira's reign and
                # made Mir's UFC 92 win read `vacant` instead of `defeated_champion`.
                if track == "undisputed" and cur["interim"]:
                    here = {f.get("fighter_slug") for f in (b.get("fighters") or [])}
                    if cur["interim"]["champion_slug"] in here:
                        audit["interim reign closed by an undisputed bout"] += 1
                        close("interim", slug, r["event_date"])
                        derived["interim"] = None
            prev_date = r["event_date"]
            # a draw or no-contest leaves the champion in place
        for t in ("undisputed", "interim"):
            if cur[t]:
                reigns.append(cur[t])

    reigns = drop_unawarded(reigns, links, bouts, audit)
    cont = link_continuations(reigns, belts, links)

    with (DATA / "belt_reigns.jsonl").open("w", encoding="utf-8") as f:
        for r in reigns:
            r["belt"] = {k: belts[r["belt_id"]][k]
                         for k in ("owner", "division", "gender")
                         if r["belt_id"] in belts}
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ag, dis = audit["AGREE"], audit["DISAGREE"]
    lines = [f"reigns reconstructed : {len(reigns):,}",
             f"belts with a lineage : {audit['belts with a lineage']:,}",
             f"single-bout belts    : {audit['single-bout belts']:,}",
             "",
             "cross-check of RECORDED vs DERIVED champion:",
             f"  agree    {ag:,}",
             f"  disagree {dis:,}   ({100*dis/max(ag+dis,1):.2f}%)",
             "",
             f"  vacant despite a previous winner : {audit['vacant despite a previous winner (stripped/vacated)']:,}",
             f"  recorded holder, nothing derived : {audit['recorded holder, nothing derived yet']:,}",
             f"  status absent                    : {audit['status absent']:,}",
             "",
             "Disagreement tracks the GAP between consecutive bouts we hold for a belt,",
             "so it measures COMPLETENESS, not correctness: a 5-year gap means the belt",
             "changed hands in fights this corpus does not contain.",
             f"{'gap to previous bout':<24}{'agree':>8}{'disagree':>10}{'disagree %':>12}"]
    for k in ("<90d", "90-365d", "1-2y", "2-5y", "5y+", "unknown"):
        a, d = bygap[k]
        if a + d:
            lines.append(f"{k:<24}{a:>8,}{d:>10,}{100*d/(a+d):>11.1f}%")
    lines += [f"interim reigns closed by an undisputed bout: {audit['interim reign closed by an undisputed bout']:,}",
              "", "<90d is the exception: two title fights for one belt inside 90 days",
              "usually means the key merged two genuinely different belts.",
              f"reigns by evidence: {dict(collections.Counter(r.get('evidence') for r in reigns))}",
              f"reigns by title: {dict(collections.Counter(r.get('title') for r in reigns))}"]
    text = "\n".join(lines)
    print(text)
    with (STATE / "lineage_audit.log").open("w") as f:
        f.write(text + "\n\nDISAGREEMENTS\n")
        for d in disagreements[:400]:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
