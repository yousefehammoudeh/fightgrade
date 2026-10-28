# Free-text promoters

`Promotion:` in an event's production block is sometimes a link to a promotion page and
sometimes bare text. The linked case gives a `promotion_id` that joins to
`promotions.jsonl` and `promotions_detail.jsonl`. The bare-text case gives a name and
nothing else -- no id, no page, no logo, no headquarters, and no way to fetch any,
because there is no page to fetch.

5,265 events are in the bare-text case: no `promotion_id`, no Ownership fallback, but a
promoter named in the production block. They were previously flagged `no_promotion` and
grouped as `null`, which is what these files fix.

## Files

| file | rows | key |
|---|---|---|
| `data/promoters_freetext.jsonl` | 2,786 | `id` (1000001..1002786) |
| `data/event_promoter_freetext.jsonl` | 5,265 | `event_slug` -> `promoter_id` |
| `logs/promoter_merge_candidates.jsonl` | 139 | review queue, see below |

`{id, slug, name}` mirrors `promotions.jsonl` so the two can be unioned for grouping.
`slug` is always `null` and marks the row as label-only: **no `promotions_detail` row
exists for these ids and none ever will.** Ids start at 1,000,000 so they cannot
collide with Tapology's own (max real id is currently 7247).

## These are not all organisations

The field mixes promotion companies with individual promoters. The largest by event
count are people: Manop Iamthuam (49), Don King (31), Tarik Solak (25), Frank Warren
(23). Useful as a "who ran this" grouping key; not interchangeable with a promotion.

## Names are folded, not resolved

Grouping is by a normalised key (case, punctuation, accents stripped). That folds 18
spelling variants -- `César Moreira`/`Cesar Moreira`, `Rus-Fighters`/`Rus Fighters` --
and the surviving raw forms are kept in `name_variants`.

It does **not** attempt to resolve a promoter to an existing promotion, even on an exact
name match. 131 names match one, and the match is unreliable in both directions:

- **Tapology itself reuses names.** `African Fighting Championship` is promotions 2072,
  2073 and 3397; `Empire Promotions` is 1664, 4729 and 5954. A name cannot pick one.
- **44 of the 131 conflict geographically** -- `Arena fight` is Brazil linked vs Russia
  free-text, `Global MMA` Argentina vs Russia, `Braveheart Promotions` Scotland vs
  Philippines. Different outfits sharing a name.
- **But some matches are real.** `Lewis Boxing Promotions` (id 6199, HQ Vienna) has 3
  linked Vienna events; its 5 free-text events are 2 in Vienna and 3 in Lagos, and the
  series name "Battle in the Ballroom" appears on both sides. Same promoter, expanded.

So every free-text promoter gets its own id, and the 139 name-match pairs are written to
`logs/promoter_merge_candidates.jsonl` with the geography, venue and event names on both
sides. Merging is a manual judgement; nothing merges automatically.
