#!/usr/bin/env python3
"""What is actually at stake in a title bout, per bout and per side.

A lineage answers "who was champion". Elo needs the other question: **what could each
fighter win or lose here**. Those come apart, and the cases that separate them are exactly
the ones a lineage gets wrong:

  UFC 227  Cejudo vs Johnson   Challenger / Champion          one belt moves, 11 defences behind it
  UFC 232  Jones vs Gustafsson Vacant / Vacant                belt is won, nobody can lose it
  UFC 274  Oliveira vs Gaethje Champion-ineligible / Challenger  champion already lost it on the scale
  UFC 221  Romero vs Rockhold  Vacant-ineligible / Vacant     only ONE man can leave with it

Tapology states the per-side role directly in `comparison.title` -- `Champion`,
`Challenger`, `Vacant`, `Interim Champion`, and the `Ineligible to Win/Lose Belt` variants.
It is present on 64% of title bouts. `roles_source` records which rung of the evidence
ladder each row came from, and they are NOT of equal quality:

  stated        37,389  62.9%  Tapology's own per-side row
  title_string   1,324   2.2%  the card names the bout "Vacant ... Championship"
  page_status      194   0.3%  `Belt Status Before Fight` and nothing better
  lineage       20,523  34.5%  our own reconstruction -- 84% accurate, see below

Scored against the 37,390 bouts where Tapology states the roles, the lineage rung
reproduces them **83.9%** of the time, and what it still gets wrong now runs in the
conservative direction: 4,191 bouts where a champion was defending read as vacant, against
585 the other way. A belt vacated between fights remains invisible to a bout-bounded
lineage. Weight or gate on `roles_source`; do not treat a `lineage` row as equal evidence
to a `stated` one.

`stake` is a PRE-fight field and `outcome` is the post-fight one. `stake` reads `defence`
for every bout a champion walks into, including the 5,826 he loses -- right for a rating
model, wrong as a description of the night. `outcome` names what happened: a man who did
not hold the belt going in and leaves with it has WON the title (`title_win`); only a man
who already held it and kept it has DEFENDED it (`defence`). Plus `retained` (kept without
winning -- draw or NC), `still_vacant`, `vacated`, and null when there is no result.

Outputs `data/bout_title_stakes.jsonl`, one row per (bout, belt) -- a unification bout
yields one row per belt, because the stakes differ per belt.

  python3 derive/bout_title_stakes.py
"""
import json, collections, datetime, sys, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# 200/300/400/500 kg, converted to lb. A promotion writes its uncapped class this way.
KG_UNLIMITED = {441, 661, 882, 1102}

ROLE = {"champion": "champion", "interim champion": "interim_champion",
        "challenger": "challenger", "vacant": "vacant"}


def parse_role(label):
    """-> (role, can_win_override, can_retain_override).

    `Champion Ineligible to Win Belt` is the champion who missed weight. He is champion
    going in and cannot WIN -- which for a champion means he cannot RETAIN: the belt leaves
    him whatever happens. That is Charles Oliveira at UFC 274, and without the retain axis
    he is indistinguishable from a champion making a normal defence.

    `Champion Ineligible to Lose Belt` is its mirror -- his opponent missed weight, so he
    keeps the belt whatever happens.
    """
    if not label:
        return None, None, None
    s = str(label).strip().lower()
    win = retain = None
    if "ineligible to win" in s:
        win = retain = False
        s = s.replace("ineligible to win belt", "").strip()
    elif "ineligible to lose" in s:
        retain = True
        s = s.replace("ineligible to lose belt", "").strip()
    return ROLE.get(s) or (ROLE.get(s) if s else None), win, retain


def days(a, b):
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except Exception:
        return None


def main():
    belts = {}
    for line in (DATA / "belts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); belts[r["belt_id"]] = r

    links = collections.defaultdict(list)
    for line in (DATA / "bout_belts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); links[r["belt_id"]].append(r)
    belts_of = collections.defaultdict(list)
    for bid, rows in links.items():
        for r in rows:
            belts_of[r["bout_slug"]].append(r)

    reigns = collections.defaultdict(list)
    for line in (DATA / "belt_reigns.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); reigns[r["belt_id"]].append(r)

    det = {}
    for line in (DATA / "bout_details.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); det[r["bout_slug"]] = r

    wanted = set(belts_of)
    bouts = {}
    for line in (DATA / "bouts.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        if b.get("bout_slug") in wanted:
            bouts[b["bout_slug"]] = b

    # bouts per belt in date order, for "how many title bouts has this belt seen"
    order = {}
    for bid, rows in links.items():
        seen, uniq = set(), []
        for r in sorted(rows, key=lambda r: (r["event_date"] or "9999", r["bout_slug"])):
            if r["bout_slug"] not in seen:
                seen.add(r["bout_slug"]); uniq.append(r)
        order[bid] = uniq

    out, stats = [], collections.Counter()
    for bid, rows in order.items():
        belt = belts.get(bid, {})
        first_date = belt.get("first_bout")
        for i, r in enumerate(rows):
            slug = r["bout_slug"]
            b = bouts.get(slug)
            if not b:
                continue
            fs = b.get("fighters") or []
            if len(fs) != 2:
                stats["skipped: not a two-fighter bout"] += 1
                continue
            date = r["event_date"]
            track = "interim" if r.get("interim") else "undisputed"
            multi_first = len(belts_of[slug]) > 1 and first_date == date

            # ---- roles: Tapology's own per-side statement first ----
            tc = (b.get("comparison") or {}).get("title") or {}
            roles = [None, None]
            winov = [None, None]
            retov = [None, None]
            for k, side_key in ((0, "left"), (1, "right")):
                roles[k], winov[k], retov[k] = parse_role(tc.get(side_key))
            source = "stated" if all(roles) else None
            if source and len(belts_of[slug]) > 1 and first_date == date and \
                    "champion" in roles:
                # same page-level problem as the belt status: a "champion" role cannot
                # refer to a belt this bout is the first ever contest for
                roles, source = [None, None], None
                stats["stated roles refused: belt's first bout in a unification"] += 1

            # ---- fallback ladder, strongest evidence first ----
            # `comparison` and `Belt Status Before Fight` never contradict each other on
            # vacancy in this corpus (0 rows), so both are direct statements. The title
            # string is next. Only then the lineage, which cannot see a belt vacated
            # between fights and so invents a defending champion -- scored below.
            # `Belt Status Before Fight` and `comparison.title` are PAGE-level: one value
            # for a bout that may put several belts on the line with different statuses.
            # Thibeault vs Robinson was billed "Ring Magazine, IBF World & WBO World Female
            # Middleweight Titles" and reads "Held by Robinson" -- true of the IBF and WBO
            # belts she held, false of the Ring title, which was vacant since Shields left
            # it. Copying the status onto every belt made her a defending Ring champion of
            # a belt she had never held.
            #
            # A holder cannot be defending a belt whose earliest bout in the corpus IS this
            # one, so on a multi-belt bout that status is refused for such a belt and the
            # row falls through to the weaker evidence below. 195 rows.
            page_status_ok = True
            if len(belts_of[slug]) > 1 and first_date == date:
                page_status_ok = False
                stats["page status refused: belt's first bout in a unification"] += 1

            if not source:
                d = det.get(slug, {})
                kind, holder = d.get("belt_status_kind"), d.get("belt_holder")
                if not page_status_ok:
                    kind, holder = "absent", None
                if kind == "held" and holder:
                    hn = holder.lower().strip()
                    idx = None
                    for k, f in enumerate(fs):
                        n = (f.get("name") or "").lower()
                        if n and (hn in n or n.split()[-1] == hn.split()[-1]):
                            idx = k
                    if idx is not None:
                        roles = ["champion" if k == idx else "challenger" for k in (0, 1)]
                        source = "page_status"
                elif kind == "vacant":
                    roles = ["vacant", "vacant"]; source = "page_status"
                if not source and re.search(r"\b(Vacant|Inaugural)\b",
                                            r.get("title_bout") or "", re.I):
                    roles = ["vacant", "vacant"]; source = "title_string"
                if source:
                    inelig = set(r.get("ineligible") or [])
                    for k, f in enumerate(fs):
                        if f.get("fighter_slug") in inelig:
                            winov[k] = False
                            if roles[k] == "champion":
                                retov[k] = False

            if not source:
                champ = None
                for rg in reigns.get(bid, []):
                    if rg.get("title") != track:
                        continue
                    st = rg.get("won_at_date") or rg.get("first_seen_date")
                    en = rg.get("lost_at_date")
                    if st and st <= date and (en is None or date <= en):
                        # the bout that ENDS a reign is the one being classified, so the
                        # champion going in is the holder of the reign that ends here
                        if en == date and rg.get("lost_at_bout") != slug:
                            continue
                        # ...and the bout that BEGINS a reign is not a defence of it. A
                        # reign won here cannot make its holder the champion going in --
                        # that credits the winner retroactively and turns every vacant
                        # title fight into a defence with a 0-day reign behind it.
                        if rg.get("won_at_bout") == slug:
                            continue
                        champ = rg.get("champion_slug"); break
                held = [f.get("fighter_slug") == champ for f in fs] if champ else [False, False]
                if any(held):
                    roles = ["champion" if h else "challenger" for h in held]
                else:
                    roles = ["vacant", "vacant"]
                source = "lineage"
                inelig = set(r.get("ineligible") or [])
                for k, f in enumerate(fs):
                    if f.get("fighter_slug") in inelig:
                        winov[k] = False
                        if roles[k] == "champion":
                            retov[k] = False

            holds = [ro in ("champion", "interim_champion") for ro in roles]
            # Champion vs Champion is a champ-champ unification: only one of them holds
            # THIS belt, and the belts are usually SPLIT between them.
            #
            # `belt_holder` cannot settle it. It is one string for the whole bout, so it
            # returns the same fighter for every belt on the card. Shields vs Hammer 2019
            # reads "Held by Shields" and we credited her with all four -- but Hammer held
            # the WBO. The evidence that does settle it is the belt's OWN previous bout:
            # WBA/WBC/IBF were last won by Shields, WBO by Hammer, and that is already in
            # the corpus. So for this one case derived beats recorded, because recorded is
            # not per-belt and derived is.
            if all(holds):
                idx = None
                if i > 0:
                    prev = bouts.get(rows[i - 1]["bout_slug"])
                    w = next((f.get("fighter_slug") for f in (prev.get("fighters") or [])
                              if f.get("result") == "W"), None) if prev else None
                    for k, f in enumerate(fs):
                        if w and f.get("fighter_slug") == w:
                            idx = k
                    if idx is not None:
                        stats["champ-vs-champ settled by the belt's own last bout"] += 1
                if idx is None:      # no prior bout on this belt -- fall back to the page
                    hn = (det.get(slug, {}).get("belt_holder") or "").lower().strip()
                    for k, f in enumerate(fs):
                        n = (f.get("name") or "").lower()
                        if hn and n and (hn in n or n.split()[-1] == hn.split()[-1]):
                            idx = k
                    if idx is not None:
                        stats["champ-vs-champ settled by the page (no prior bout)"] += 1
                if idx is None:
                    holds = [False, False]
                    stats["champ-vs-champ, holder of this belt unresolved"] += 1
                else:
                    holds = [k == idx for k in (0, 1)]

            # Three independent axes per side. A holder can RETAIN or LOSE; a non-holder can
            # only WIN. Missed weight breaks the usual coupling, which is the whole point.
            can_win = [(not holds[k]) if winov[k] is None else winov[k] for k in (0, 1)]
            can_retain = [holds[k] if retov[k] is None else retov[k] for k in (0, 1)]
            can_lose = [holds[k] and not (retov[k] is True) for k in (0, 1)]

            if any(holds):
                if not any(can_retain):
                    # the belt leaves the champion whatever happens -- he was stripped at
                    # the weigh-in and only his opponent can pick it up
                    stake = "champion_cannot_retain"
                elif not any(can_lose):
                    # his opponent missed weight, so he keeps it whatever happens
                    stake = "champion_cannot_lose"
                elif not any(can_win):
                    # the CHALLENGER missed weight. The champion can still lose the belt --
                    # but to vacancy, not to his opponent, so it cannot change hands.
                    stake = "challenger_cannot_win"
                else:
                    stake = "defence"
            elif sum(can_win) == 2:
                stake = "vacant_open"        # either man leaves with it
            elif sum(can_win) == 1:
                stake = "vacant_one_way"     # Romero: only one man can leave with it
            else:
                stake = "not_on_the_line"
            # will the belt certainly be held by someone other than its current holder?
            certain = (any(holds) and not any(can_retain)) or (not any(holds) and all(can_win))

            # ---- what actually HAPPENED to the belt ----
            # `stake` is a PRE-fight question: what could each man win or lose walking in.
            # It reads `defence` for every bout a champion starts, including the ones he
            # loses -- which is right for Elo and wrong as a description of the night. The
            # post-fight answer needs its own word: a man who did not hold the belt going
            # in and leaves with it has WON the title; only a man who already held it and
            # kept it has DEFENDED it. McGregor over Aldo is a title win, not a defence.
            res = [f.get("result") for f in fs]
            wins = [k for k in (0, 1) if res[k] == "W"]
            if len(wins) == 1:
                w, l = wins[0], 1 - wins[0]
                if holds[w]:
                    # the holder won -- he keeps it unless he was stripped on the scale
                    outcome = "defence" if can_retain[w] else "vacated"
                elif can_win[w]:
                    outcome = "title_win"
                elif can_lose[l]:
                    # the champion lost, but the winner had missed weight and could not
                    # take the belt off him: it goes vacant rather than changing hands
                    outcome = "vacated"
                elif holds[l]:
                    outcome = "retained"        # he lost the fight and kept the belt
                else:
                    outcome = "still_vacant"    # Romero: the only eligible man did not win
            elif res[0] == res[1] and res[0] in ("D", "NC"):
                # nobody won, so nobody takes it
                if any(holds[k] and can_retain[k] for k in (0, 1)):
                    outcome = "retained"
                elif any(holds):
                    outcome = "vacated"         # stripped champion, and no winner to take it
                else:
                    outcome = "still_vacant"
            else:
                outcome = None          # not fought yet, or a malformed result pair

            stats["stake=" + stake] += 1
            stats["outcome=" + str(outcome)] += 1
            stats["roles=" + source] += 1

            # ---- magnitude ----
            champ_slug = next((f.get("fighter_slug") for k, f in enumerate(fs) if holds[k]), None)
            defs_before, reign_start = None, None
            if champ_slug:
                for rg in reigns.get(bid, []):
                    if rg.get("champion_slug") != champ_slug or rg.get("title") != track:
                        continue
                    st = rg.get("won_at_date") or rg.get("first_seen_date")
                    if st and st <= date and (rg.get("lost_at_date") is None
                                              or date <= rg["lost_at_date"]):
                        # A reign carried across a rename or an era split starts on the
                        # PREDECESSOR belt. Pulver held the 155 lb title from Feb 2001;
                        # keying it from the renamed belt's own first bout resets him to
                        # nought on the very night he defended it.
                        cf = rg.get("continued_from") or {}
                        reign_start = cf.get("reign_started") or st
                        n = sum(1 for x in rows[:i]
                                if x["event_date"] and x["event_date"] >= st
                                and bool(x.get("interim")) == (track == "interim"))
                        # the bout that CROWNED him is in that window and is not a defence
                        defs_before = max(n - (1 if rg.get("won_at_bout") else 0), 0)
                        break

            # The division NAME is what the promotion called it that night, and promotions
            # rename divisions without moving them. Jens Pulver's 2001 "UFC Bantamweight
            # Championship" was contested at 155 lb and Dominick Cruz's 2011 one at 135 lb;
            # by name they are the same belt, by weight Pulver belongs with Khabib. Carry
            # the contested weight so the ledger can be grouped by what was actually fought.
            # An UNLIMITED class is not a weight. Tapology writes it two ways: the badge
            # "HW", and a round kg cap converted to pounds -- 441/661/882/1102 lb are
            # exactly 200/300/400/500 kg. Both mean "no upper limit", so both are recorded
            # as openweight with NO number, rather than as a 1,102 lb fight that would sit
            # at the top of any weight sort and skew every aggregate.
            wb = b.get("weight_badge")
            weight, wlabel, unlimited = None, "—", False
            if wb is not None and str(wb).strip():
                m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(wb))
                if m:
                    v = float(m.group(1))
                    if round(v) in KG_UNLIMITED:
                        wlabel, unlimited = "openweight", True
                    else:
                        weight = v; wlabel = f"{v:g} lb"
                elif str(wb).strip().upper() == "HW":
                    wlabel, unlimited = "openweight", True
                else:
                    wlabel = str(wb).strip()

            other = [x for x in belts_of[slug] if x["belt_id"] != bid]
            out.append({
                "bout_slug": slug, "belt_id": bid, "event_date": date,
                "sport": belt.get("sport"), "level": b.get("level"),
                "division": belt.get("division"),
                "weight_lb": weight, "weight_label": wlabel, "unlimited": unlimited,
                "gender": belt.get("gender"), "owner": belt.get("owner"),
                "interim": bool(r.get("interim")),
                "roles": roles, "roles_source": source,
                "can_win": can_win, "can_retain": can_retain, "can_lose": can_lose,
                "stake": stake, "outcome": outcome, "belt_certainly_moves": certain,
                "fighters": [f.get("fighter_slug") for f in fs],
                "champion_slug": champ_slug,
                "champion_defenses_before": defs_before,
                "champion_reign_days": days(reign_start, date) if reign_start else None,
                "belts_on_the_line": 1 + len(other),
                "unification": bool(other) or sorted(roles) == ["champion", "interim_champion"],
                "belt_bouts_before": i,
                "belt_age_days": days(first_date, date) if first_date else None,
            })

    with (DATA / "bout_title_stakes.jsonl").open("w", encoding="utf-8") as f:
        for r in sorted(out, key=lambda r: (r["event_date"] or "", r["bout_slug"], r["belt_id"])):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"rows {len(out):,}  ({len({r['bout_slug'] for r in out}):,} distinct bouts)")
    for k, v in sorted(stats.items()):
        print(f"  {k:<48} {v:>7,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
