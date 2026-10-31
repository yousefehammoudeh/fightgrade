# News gold set: how a headline is labelled

`dbt/seeds/news_event_labels.csv`, one row per headline (`news_url_id`). A label is what
**the headline alone** says. Nobody opens the article: the classifier only sees the slug, so
the gold set must not either.

## Columns

| column | meaning |
|---|---|
| `news_url_id` | `stg_news_headlines.news_url_id` |
| `stratum` | `<year>_<cand\|non>`: the year sampled and whether the keyword pass marked it |
| `site`, `published_date` | as in `stg_news_headlines`; the classifier sees both |
| `headline` | the slug as words, for review |
| `event_type` | one of the types below |
| `status` | `confirmed`, `speculative`, `denied`; empty for `none` |
| `fighters` | the fighter(s) the event happens to, `\|`-separated, as written in the slug |
| `replaced_fighter` | for a replacement: who is out |
| `opponent` | the opponent in the bout concerned, if named |
| `event` | the card, if named (`ufc 254`, `ufc vegas 12`) |
| `reason` | for a withdrawal, cancellation or suspension: `injury`, `illness`, `weight_cut`, `visa`, `doping`, `medical`, `legal`, `other`, if named |
| `reviewed` | who checked the row after drafting: `claude` (a second pass the user delegated, 2026-10-31), `y` once the user has |

## Event types

- **retirement**: a fighter retires, or is said to have retired (Dana White "Jon Jones
  officially retired"). Not a coach, referee or commentator.
- **return**: a retired or long-inactive fighter comes back (un-retires, is booked for a
  return fight). A routine "returns at UFC 300" after a normal layoff is `booking`.
- **withdrawal**: a fighter is out of a booked bout (injury, illness, weight cut, visa), or the
  bout is cancelled or postponed, with no replacement named.
- **replacement**: a new opponent is named for a bout whose fighter withdrew ("X steps in
  for Y", "X replaces Y", "X now faces Z after Y's withdrawal"). It implies the withdrawal;
  `replaced_fighter` holds who is out.
- **weight_miss**: a fighter misses weight. A preview of the scale, or a vow not to miss, is
  `none`.
- **suspension**: a fighter is suspended or banned: doping (`reason=doping`), a commission's
  disciplinary ban, or a post-fight medical suspension (`reason=medical`).
- **release**: a promotion cuts or releases a fighter, or they part ways.
- **signing**: a fighter signs with a promotion (new contract, new promotion).
- **fought_injured**: a fighter reveals they fought hurt.
- **booking**: a bout is announced or set, not as a replacement.
- **none**: everything else: results, previews, interviews, call-outs, opinion, rankings,
  boxing, grappling, other sports.

Rulings made while drafting (2026-10-31), so the same case always gets the same label:

- **A failed drug test** is `suspension` with `reason=doping`, before any ban is announced.
- **A withdrawal or replacement is `confirmed` only for a bout that was booked.** A plan or talks
  ("was set to face Prochazka before the Strickland swerve", "in talks to replace") is
  `speculative`. The user's ruling, 2026-10-31: Chimaev vs Prochazka was never confirmed, and
  Tapology has no such cancelled bout. The headline label is what the headline claims; whether
  it happened is settled against the fight data (README, step 6).
- **A replacement need not name the new fighter**: "X gets new opponent", "newcomer swoops in on
  four days notice" are `replacement`. "X pulled, replacement being looked for" is `withdrawal`.
- **Injury news with no bout named as lost** ("breaks hand in training", "injury update ... UFC
  189", "knee surgery to result in lengthy recovery") is a `speculative` withdrawal. An injury
  revealed after a fight the fighter went through with is `fought_injured`.
- **A fight billed as someone's last** ("final fight", "last faceoff ever") is a `confirmed`
  retirement.
- **"Returns"** after an ordinary layoff, or to a promotion after fighting elsewhere, is
  `booking`. `return` is only after a retirement or a long absence from fighting.
- **Not MMA** is `none`, even with MMA fighters: boxing (including exhibitions), BKFC (bare-knuckle
  boxing), RAF wrestling, grappling, kickboxing, pro wrestling, Karate Combat.
- **A truncated slug** is read as its full title most likely ran: "dana white jon jones had right
  to fight so canceling daniel cormier" ends "...wasn't an option", so it is `none`.
- **"Draws" an opponent** means is matched with them: `booking`.
- **A fighter's death** is `none`: there is no type for it yet, though rankings need it.
- **Removed from the UFC roster** is `release`.
- A whole card or event cancelled is `withdrawal` with no fighter.

One headline, one label: the most specific event wins (replacement over withdrawal over
booking). A headline about someone else's event (a reaction to Jones' retirement) carries
that event with `fighters` set to whom it happened to, and the status the headline gives it.

## Status

- **confirmed**: the headline states it as fact ("announces retirement", "out of UFC 300",
  "suspended six months").
- **speculative**: hints, considers, could, may, wants to, plans to, ponders, rumoured,
  "hints at retirement", "open to serving as a replacement".
- **denied**: the fighter or promotion says it is not so ("not retiring", "Dana White
  denies Jones retired months ago").

## The sample

400 headlines, drawn deterministically (`order by sha2(news_url_id || 'gold-v1')`):

| year | candidates | non-candidates | why |
|---|---|---|---|
| 2008 | 60 | 20 | the early era; 2005, the README's first choice, has 6 headlines in all |
| 2015 | 100 | 40 | the middle |
| 2025 | 100 | 40 | Vox to WordPress (2025-08-05) |
| 2026 | 30 | 10 | WordPress only: what the daily run sees |

## The bar

Precision ≥ 0.9 on **confirmed** retirements, withdrawals and replacements; recall reported
beside it, per event type and decade. Non-candidate strata measure the keyword pass's recall:
an event among them is one the pass drops.
