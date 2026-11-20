"""Experiments: who is shown which version of a page, and which version does better.

An experiment (config.Experiment, `[[site.experiment]]`) splits visitors between arms,
the original and its variants, or in a multivariate test every combination of its
factors' variants. c.js shows each visitor theirs and counts two things, once each per
visitor (per tab, for one who hasn't consented): that they were shown the experiment
(exposed), and that they then reached each of its metrics (converted). The collector
keeps them as counters, one partition an experiment (store.py: `x|<site>|<key>`):

    <day>#e#<arm>              visitors first exposed that day
    <day>#c#<arm>#<metric>     ... who reached the metric for the first time that day
    <day>#n#<arm>#<metric>     every time the metric was reached
    <day>#v#<arm>#<metric>     the sum of the events' `value`, in hundredths

Which arm a visitor gets is the same every time, as far as anything allows: a number
from 0 to 1 (`bucket`), cut by the arms' shares. A consented visitor's comes from their
visitor cookie, in c.js. Anyone else's comes from the collector (GET /_c/x): a keyed hash
of their address's network (/24, or /48) and browser, so the same person on the same
network and browser lands in the same arm in any tab, on any day, and nothing is kept
anywhere to do it. Within a tab, c.js remembers the arm it showed, so a change of
network or of the shares never switches a page mid-visit; with consent, it remembers it
for good.

How shares are set (`weights`):

    split     the variants' weights, fixed: an A/B test
    thompson  a bandit: each arm's chance of being the best at the goal, from a Beta
              posterior of its conversions, so traffic moves to what works as it learns
    greedy    a bandit: the arm with the best rate so far, scored as if each had begun
              with one success (an epsilon-greedy bandit, as in Loren Shure's blog post
              on the multi-armed bandit problem, MathWorks, 2016)

A bandit keeps `explore` of traffic split evenly, whatever it has learned.

How arms are compared (`compare`), each against the original:

    rate          converted visitors over exposed ones, with a Wilson interval
    lift          the rate against the original's, relative, with an interval
    beat          the chance the arm's true rate is above the original's (Beta posteriors, exact)
    best          the chance it's the best of all arms (Monte Carlo, seeded: the same counts
                  always give the same answer)
    p             an always-valid p-value: a mixture sequential probability ratio test
                  (Johari, Pekelis and Walsh, "Always Valid Inference", 2017), taken at
                  each day's running totals. Looking every day doesn't inflate it, as it
                  would a t-test's, so a result can be read whenever it's looked at.

The lift's interval is the always-valid one that goes with `p`, so the two agree.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import random
from collections import Counter
from statistics import NormalDist

from .config import Experiment
from .store import truncate_ip

DRAWS = 5000              # Monte Carlo draws for the chance each arm is best
MIN_UNITS = 10            # a day's running totals count for the p-value from this many visitors an arm...
MIN_CONVERSIONS = 5       # ... and this many conversions between the two
EFFECT = 0.25             # the sequential test is tuned to effects about this big, relative to the rate


# ---------------------------------------------------------------- counting


def tally(items: list[dict]) -> dict[str, dict[str, Counter]]:
    """An experiment's counter items (`<day>#<kind>#<arm>[#<metric>]`) by day, then arm:
    {"e": n, ("c", metric): n, ("n", metric): n, ("v", metric): hundredths}."""
    out: dict[str, dict[str, Counter]] = {}
    for item in items:
        parts = str(item.get("sk", "")).split("#", 3)
        if len(parts) < 3:
            continue
        day, kind, arm = parts[:3]
        key = kind if kind == "e" else (kind, parts[3] if len(parts) > 3 else "")
        out.setdefault(day, {}).setdefault(arm, Counter())[key] += int(item.get("n") or 0)
    return dict(sorted(out.items()))


def totals(days: dict[str, dict[str, Counter]]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for arms in days.values():
        for arm, c in arms.items():
            out.setdefault(arm, Counter()).update(c)
    return out


# ---------------------------------------------------------------- statistics


def wilson(c: int, n: int, confidence: float = 0.95) -> tuple[float | None, float | None]:
    """A rate's interval that behaves at 0 and at small counts, where the usual one doesn't."""
    if not n:
        return None, None
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = c / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, mid - half), min(1.0, mid + half)


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def beat(c1: int, n1: int, c0: int, n0: int) -> float | None:
    """The chance arm 1's rate is above arm 0's, both Beta(1 + conversions, 1 + the rest):
    Evan Miller's closed form, a sum of one term per conversion of arm 1."""
    if not n1 or not n0:
        return None
    a1, b1, a0, b0 = 1 + c1, 1 + n1 - c1, 1 + c0, 1 + n0 - c0
    total = 0.0
    for i in range(a1):
        total += math.exp(_log_beta(a0 + i, b0 + b1) - math.log(b1 + i) - _log_beta(1 + i, b1) - _log_beta(a0, b0))
    return min(1.0, max(0.0, total))


def best(counts: list[tuple[int, int]], draws: int = DRAWS, seed: str = "") -> list[float]:
    """Each arm's chance of the best rate, from its Beta posterior, by drawing. `seed` fixes
    the draws, so the same counts always give the same shares."""
    if not counts:
        return []
    rng = random.Random(hashlib.sha256(f"{seed}|{counts}".encode()).digest())
    wins = [0] * len(counts)
    params = [(1 + c, 1 + max(0, n - c)) for c, n in counts]
    for _ in range(draws):
        values = [rng.betavariate(a, b) for a, b in params]
        wins[values.index(max(values))] += 1
    return [w / draws for w in wins]


def _variance(c0: int, n0: int, c1: int, n1: int) -> float:
    r0, r1 = (c0 + 0.5) / (n0 + 1), (c1 + 0.5) / (n1 + 1)
    return r0 * (1 - r0) / n0 + r1 * (1 - r1) / n1


def _tau2(c0: int, n0: int, c1: int, n1: int) -> float:
    pooled = (c0 + c1 + 0.5) / (n0 + n1 + 1)
    return (EFFECT * max(pooled, 0.01)) ** 2


def sequential(points: list[tuple[int, int, int, int]], confidence: float = 0.95) -> dict:
    """An always-valid comparison of arm 1 with arm 0 from their running totals at each
    look, `(c0, n0, c1, n1)`, oldest first: the mSPRT p-value (its least over the looks),
    and the interval for the difference in rates at the last look.

    With V the estimate's variance and a normal mixture of effects of variance tau², the
    likelihood ratio at a look is sqrt(V / (V + tau²)) * exp(d² tau² / (2 V (V + tau²)))
    for an observed difference d; p is 1 over the largest it has been."""
    alpha = 1 - confidence
    p = 1.0
    for c0, n0, c1, n1 in points:
        if n0 < MIN_UNITS or n1 < MIN_UNITS or c0 + c1 < MIN_CONVERSIONS:
            continue
        v, t2 = _variance(c0, n0, c1, n1), _tau2(c0, n0, c1, n1)
        d = c1 / n1 - c0 / n0
        exponent = d * d * t2 / (2 * v * (v + t2))
        ratio = math.sqrt(v / (v + t2)) * math.exp(min(exponent, 700))
        p = min(p, 1.0, 1 / ratio)
    out = {"p": round(p, 6), "low": None, "high": None}
    if points and points[-1][1] and points[-1][3]:
        c0, n0, c1, n1 = points[-1]
        v, t2 = _variance(c0, n0, c1, n1), _tau2(c0, n0, c1, n1)
        d = c1 / n1 - c0 / n0
        half = math.sqrt(2 * v * (v + t2) / t2 * math.log(math.sqrt((v + t2) / v) / alpha))
        out["low"], out["high"] = d - half, d + half
    return out


# ---------------------------------------------------------------- allocation


def weights(exp: Experiment, counts: dict[str, Counter]) -> dict[str, float]:
    """Each arm's share of the visitors in the test, from now: the configured split, or a
    bandit's, from the goal's conversions so far (`counts`: `totals()` of its tally)."""
    arms = exp.arms
    if exp.allocation == "split":
        total = sum(a.weight for a in arms)
        return {a.id: a.weight / total for a in arms}
    goal = exp.goal.text
    seen = [(counts.get(a.id, Counter())[("c", goal)], counts.get(a.id, Counter())["e"]) for a in arms]
    if exp.allocation == "greedy":
        scores = [(c + 1) / (n + 1) for c, n in seen]
        top = max(scores)
        leaders = [i for i, s in enumerate(scores) if s == top]
        learned = [1 / len(leaders) if i in leaders else 0.0 for i in range(len(arms))]
    else:
        learned = best(seen, seed=exp.key)
    even = exp.explore / len(arms)
    return {a.id: even + (1 - exp.explore) * share for a, share in zip(arms, learned)}


def bucket(secret: str, site_id: str, key: str, ip: str | None, ua: str | None) -> float:
    """Where a visitor who hasn't consented falls in an experiment, from 0 to 1: a keyed
    hash of their address's network and their browser. The same network and browser
    always land in the same place; another experiment, somewhere unrelated."""
    net = truncate_ip(ip or "")
    mac = hmac.new((secret or "carterlitics").encode(), f"bucket|{site_id}|{key}|{net}|{ua or ''}".encode(),
                   hashlib.sha256).digest()
    return int.from_bytes(mac[:8], "big") / 2 ** 64


# ---------------------------------------------------------------- what c.js gets


def client(exp: Experiment, shares: dict[str, float]) -> dict | None:
    """The experiment as c.js runs it, or None for one it shouldn't know about (paused).
    A draft is there only to preview (?cl_x=key:variant); a done one shows its winner."""
    status = {"running": "run", "done": "done", "draft": "draft"}.get(exp.status)
    if not status:
        return None

    def level(v) -> dict:
        out: dict = {"id": v.id}
        if v.css:
            out["css"] = v.css
        if v.text:
            out["text"] = [[sel, value] for sel, value in v.text]
        if v.attrs:
            out["attrs"] = [[sel, [list(p) for p in pairs]] for sel, pairs in v.attrs]
        if v.redirect:
            out["go"] = v.redirect
        return out

    out = {
        "k": exp.key, "s": status,
        "f": [{"id": f.id, "lv": [level(v) for v in f.variants]} for f in exp.factors],
        "arms": [{"id": a.id, "w": round(shares.get(a.id, 0.0), 4)} for a in exp.arms],
        "m": [{"t": m.text, "k": m.kind, "n": m.name, **({"p": [list(p) for p in m.props]} if m.props else {})}
              for m in exp.all_metrics],
    }
    if exp.pages:
        out["pages"] = [p.name for p in exp.pages]
    if exp.traffic < 1:
        out["tr"] = exp.traffic
    if exp.audience == "consented":
        out["aud"] = "consent"
    if exp.status == "done":
        out["win"] = exp.winner
    return out


# ---------------------------------------------------------------- the console's report


def _share(a: float, b: float) -> float | None:
    return a / b if b else None


def _round(x, digits: int = 6):
    return None if x is None else round(x, digits)


def compare(exp: Experiment, days: dict[str, dict[str, Counter]], arm: str, control: str, metric: str) -> dict:
    """One arm against the original on one metric: its rate, the lift and how sure it is."""
    def running(name: str) -> list[tuple[int, int]]:
        c = n = 0
        out = []
        for arms in days.values():
            got = arms.get(name, Counter())
            c, n = c + got[("c", metric)], n + got["e"]
            out.append((c, n))
        return out

    mine, base = running(arm), running(control)
    c1, n1 = mine[-1] if mine else (0, 0)
    c0, n0 = base[-1] if base else (0, 0)
    total = sum(d.get(arm, Counter())[("n", metric)] for d in days.values())
    value = sum(d.get(arm, Counter())[("v", metric)] for d in days.values())
    low, high = wilson(c1, n1, exp.confidence)
    out = {"converted": c1, "conversions": total, "rate": _round(_share(c1, n1)), "low": _round(low),
           "high": _round(high), "value": round(value / 100, 2) if value else 0,
           "per_visitor": _round(_share(value / 100, n1), 4) if value else None}
    if arm == control:
        return out
    seq = sequential([(b[0], b[1], m[0], m[1]) for b, m in zip(base, mine)], exp.confidence)
    r0 = _share(c0, n0)
    out["lift"] = _round(_share(c1 / n1 - r0, r0)) if n1 and r0 else None
    # A rate is never below 0, so neither is the lift below -100%, however wide the interval.
    out["lift_low"] = _round(max(seq["low"] / r0, -1.0)) if r0 and seq["low"] is not None else None
    out["lift_high"] = _round(seq["high"] / r0) if r0 and seq["high"] is not None else None
    out["beat"] = _round(beat(c1, n1, c0, n0), 4)
    out["p"] = seq["p"]
    out["significant"] = ("better" if c1 / n1 > r0 else "worse") if seq["p"] < 1 - exp.confidence and n1 and \
        r0 is not None else ""
    return out


def report(exp: Experiment, items: list[dict], shares: dict[str, float] | None = None) -> dict:
    """Everything the console shows of one experiment, from its counter items."""
    days = tally(items)
    sums = totals(days)
    arms = exp.arms
    control = arms[0].id
    shares = shares if shares is not None else weights(exp, sums)
    metrics = [m.text for m in exp.all_metrics]
    rows = []
    for a in arms:
        n = sums.get(a.id, Counter())["e"]
        rows.append({"id": a.id, "name": a.name, "share": round(shares.get(a.id, 0.0), 4), "units": n,
                     "metrics": {m: compare(exp, days, a.id, control, m) for m in metrics}})
    for m in metrics:
        chances = best([(r["metrics"][m]["converted"], r["units"]) for r in rows], seed=f"{exp.key}|{m}")
        for r, chance in zip(rows, chances):
            r["metrics"][m]["best"] = round(chance, 4) if any(x["units"] for x in rows) else None
    out = {
        "key": exp.key, "name": exp.name or exp.key, "status": exp.status, "allocation": exp.allocation,
        "explore": exp.explore, "audience": exp.audience, "traffic": exp.traffic, "confidence": exp.confidence,
        "pages": [p.text for p in exp.pages], "changes": exp.changes, "winner": exp.winner, "goal": exp.goal.text,
        "metrics": metrics, "multivariate": exp.multivariate,
        "factors": [{"id": f.id, "variants": [{"id": v.id, "name": v.label} for v in f.variants]} for f in exp.factors],
        "since": next(iter(days), ""), "days": len(days), "units": sum(r["units"] for r in rows),
        "arms": rows, "series": [{"day": d, **{a.id: arms_.get(a.id, Counter())["e"] for a in arms}}
                                 for d, arms_ in days.items()],
    }
    if exp.multivariate:
        out["effects"] = effects(exp, days)
    out["verdict"] = verdict(exp, rows)
    return out


def effects(exp: Experiment, days: dict[str, dict[str, Counter]]) -> list[dict]:
    """A multivariate test's main effects on the goal: each factor's variants, every arm
    that has one added together, against the factor's first."""
    goal = exp.goal.text
    out = []
    for i, f in enumerate(exp.factors):
        merged: dict[str, dict[str, Counter]] = {}
        for day, arms in days.items():
            for arm, counts in arms.items():
                parts = arm.split(".")
                if len(parts) == len(exp.factors):
                    merged.setdefault(day, {}).setdefault(parts[i], Counter()).update(counts)
        levels = []
        for v in f.variants:
            n = sum(d.get(v.id, Counter())["e"] for d in merged.values())
            levels.append({"id": v.id, "name": v.label, "units": n,
                           **compare(exp, merged, v.id, f.variants[0].id, goal)})
        out.append({"factor": f.id, "levels": levels})
    return out


def verdict(exp: Experiment, rows: list[dict]) -> dict:
    """In a sentence: which arm wins on the goal, if any can be said to yet."""
    goal = exp.goal.text
    if exp.status == "done":
        name = next((r["name"] for r in rows if r["id"] == exp.winner), exp.winner)
        return {"state": "done", "arm": exp.winner, "text": f"Done: everyone now sees {name}."}
    if exp.status == "draft":
        return {"state": "none", "arm": "", "text": "A draft: only a preview shows it, and nothing is counted until it runs."}
    if not any(r["units"] for r in rows):
        return {"state": "none", "arm": "", "text": "Nobody has been in it yet."}
    better = [r for r in rows[1:] if r["metrics"][goal].get("significant") == "better"]
    worse = [r for r in rows[1:] if r["metrics"][goal].get("significant") == "worse"]
    if better:
        top = max(better, key=lambda r: r["metrics"][goal]["rate"] or 0)
        m = top["metrics"][goal]
        return {"state": "winner", "arm": top["id"],
                "text": f"{top['name']} beats the original at {goal}: {m['lift']:+.0%} "
                        f"({m['beat']:.0%} chance it's better)."}
    if worse and len(worse) == len(rows) - 1:
        return {"state": "loser", "arm": rows[0]["id"],
                "text": "The original beats every variant: keep it."}
    leader = max(rows, key=lambda r: r["metrics"][goal]["best"] or 0)
    chance = leader["metrics"][goal]["best"]
    return {"state": "waiting", "arm": leader["id"],
            "text": f"No clear result yet. {leader['name']} leads, with a {chance:.0%} chance of being the best."
            if chance is not None else "No clear result yet."}
