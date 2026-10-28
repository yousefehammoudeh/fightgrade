"""Whole fights from round scores, with judges who watch the same fight (DESIGN.md §8).

Scored independently, three judges' cards agree less than real panels do, and
fight-level probabilities come out too sure (a 94% favourite won 88%). Judges
share what the stats miss, so each round gets a shared effect u (all three
judges) and each fight a shared effect v (every round), and a judge's score
becomes scale * eta + u + v, with the cut-points scaled alike. Fitted by maximum
likelihood on the out-of-fold judge scores of 2010-26 (3,601 fights): round sd
1.82, fight sd 1.26, scale 1.55. The scale keeps each judge's round predictions
as calibrated as they were (log loss 0.4693 -> 0.4702); fight-level calibration
error fell from 0.033 to 0.014.

Everything here integrates u and v by simulation: DRAWS shared draws per fight,
common to every rater, so any panel - the real one, a swap, a random one - is
scored on the same imagined fights.
"""
import numpy as np
from scipy.special import expit

PANEL = {"round_sd": 1.816, "fight_sd": 1.262, "scale": 1.545}   # fitted out of fold, 2026-10-27
DRAWS = 200


def round_scores(basis, model, rounds, raters: list) -> tuple[np.ndarray, ...]:
    """eta and the cut-points, [rounds x raters], for each rater on these rounds."""
    X, years = basis.transform(rounds), rounds["year"].to_numpy()
    out = [model.score(X, np.full(len(rounds), r, dtype=object), years) for r in raters]
    return tuple(np.stack([o[i] for o in out], axis=1) for i in range(4))


def class_probs(x, t1, t2, t3):
    s = expit
    up1, up2, up3, dn1, dn2, dn3 = s(x - t1), s(x - t2), s(x - t3), s(-x - t1), s(-x - t2), s(-x - t3)
    return np.maximum(np.stack([dn3, dn2 - dn3, dn1 - dn2, 1 - up1 - dn1, up1 - up2, up2 - up3, up3], -1), 1e-12)


def draws(n_rounds: int, seed: int, n: int = DRAWS) -> np.ndarray:
    """[n, rounds] shared effects, u + v, the same for every rater."""
    rng = np.random.default_rng(seed)
    return PANEL["fight_sd"] * rng.standard_normal((n, 1)) + PANEL["round_sd"] * rng.standard_normal((n, n_rounds))


def cards(eta, t1, t2, t3, shared, shift: float) -> tuple[np.ndarray, np.ndarray]:
    """Per draw and rater: P(card has A ahead, level, B ahead) [n, raters, 3], and the
    distribution of A's margin over the fight [n, raters, 7R+1] (before deductions;
    index 3R is level). shift is A's deductions less B's, taken off after the rounds."""
    k = PANEL["scale"]
    P = class_probs(k * eta[None] + shared[:, :, None], k * t1[None], k * t2[None], k * t3[None])   # [n, R, C, 7]
    n, R, C, _ = P.shape
    dist = np.zeros((n, C, 7 * R + 1))
    dist[:, :, 3 * R] = 1.0
    for r in range(R):
        new = np.zeros_like(dist)
        for m in range(-3, 4):
            new += np.roll(dist, m, axis=2) * P[:, r, :, m + 3][..., None]
        dist = new
    s = np.arange(7 * R + 1) - 3 * R - shift
    card = np.stack([dist[..., s > 0].sum(-1), dist[..., s == 0].sum(-1), dist[..., s < 0].sum(-1)], -1)
    return card, dist


def panel_probs(card: np.ndarray, members: np.ndarray) -> np.ndarray:
    """P(A wins, draw, B wins) for each panel. card [n, raters, 3]; members [panels, 3]
    indices into raters. A wins when more cards have A ahead than B."""
    c = card[:, members]                                    # [n, panels, 3, 3]
    v = np.stack([c[..., 2], c[..., 1], c[..., 0]], -1)     # value -1 (B card), 0, +1 (A card)
    tot = v[:, :, 0]
    for j in (1, 2):
        nxt = np.zeros(tot.shape[:-1] + (tot.shape[-1] + 2,))
        for i in range(3):
            nxt[..., i:i + tot.shape[-1]] += tot * v[:, :, j, i][..., None]
        tot = nxt                                           # values -3 .. +3
    p = tot.mean(axis=0)
    return np.stack([p[:, 4:].sum(-1), p[:, 3], p[:, :3].sum(-1)], -1)


def decision_types(card: np.ndarray, members) -> dict[str, float]:
    """The real panel's result by kind: unanimous, split, majority, or a draw."""
    import itertools
    c = card[:, list(members)]
    out: dict[str, float] = {}
    for combo in itertools.product(range(3), repeat=3):
        p = float(np.prod([c[:, j, combo[j]] for j in range(3)], axis=0).mean())
        na, nd, nb = combo.count(0), combo.count(1), combo.count(2)
        if na > nb:
            k = "a_unanimous" if na == 3 else ("a_majority" if nd == 1 and nb == 0 else "a_split")
        elif nb > na:
            k = "b_unanimous" if nb == 3 else ("b_majority" if nd == 1 and na == 0 else "b_split")
        else:
            k = "draw"
        out[k] = out.get(k, 0.0) + p
    return out
