"""The re-read gate: a version change never sends again, unasked, what was already done (docs/install.md).

A paid stage's version changes with its prompt's wording or schema, its model or effort, the privacy policy, a Jev
question set, a cut or a method. Each stage that would then send again what it did under the old version splits what's
due: work never done, and work whose own input changed (a re-transcribed episode's payload, a card whose text
changed), go ahead under the current version as before; work done under another version whose input is the same is
held, its old result staying live, until `im reread STAGE` sends it. `im run` and `im pending` report what's held, per
stage, with what it would cost: the logged cost of those items' last requests.
"""

from collections import Counter
from collections.abc import Callable, Iterable

from im import llm

STAGES = ("extract", "sensitivity", "connect", "merges", "lenses")


def estimate(conn, egress_ids: Iterable[int], to_model: Callable[[str], str]) -> tuple[float, str]:
    """What sending again would cost, and how that was estimated: the logged cost of each request (counted once),
    scaled by the price ratio when the model it would go to now, `to_model(its model)`, is another and both prices
    are known (llm.PRICES), and otherwise the old model's cost."""
    ids = sorted(set(egress_ids))
    rows = conn.execute(
        """SELECT model, cost_usd, coalesce(input_tokens, 0), coalesce(output_tokens, 0) FROM im.egress_log
           WHERE egress_id = ANY(%s)""", (ids,)).fetchall() if ids else []
    total, scaled, unpriced = 0.0, set(), set()
    for model, cost, tokens_in, tokens_out in rows:
        cost, now = float(cost), to_model(model)
        if now != model:
            old_price, new_price = llm.price(model), llm.price(now)
            if old_price and new_price:
                was = tokens_in * old_price[0] + tokens_out * old_price[1]
                cost *= ((tokens_in * new_price[0] + tokens_out * new_price[1]) / was if was
                         else new_price[0] / old_price[0])
                scaled.add(f"{model} to {now}")
            else:
                unpriced.update(m for m, price in ((model, old_price), (now, new_price)) if not price)
        total += cost
    how = "the logged cost of their last requests"
    if scaled:
        how += f", scaled by the price ratio ({'; '.join(sorted(scaled))})"
    if unpriced:
        how += f", at the old model's cost (no price known for {'; '.join(sorted(unpriced))})"
    return round(total, 6), how


def report(stage: str, versions: Iterable[str], to: str, cost: tuple[float, str]) -> dict | None:
    """A stage's held work as `im run` and `im pending` show it: how many, from which version(s) to which, the
    estimate, and the command that sends it. None when nothing is held."""
    n = Counter(versions)
    if not n:
        return None
    return {"held": n.total(), "from": dict(n.most_common()), "to": to, "estimated_usd": cost[0],
            "estimate": cost[1], "approve": f"im reread {stage}"}


def add(stats: dict, held: dict | None) -> dict:
    """`stats` with the held report under `held`, when anything is held: a run with nothing held reads as before."""
    if held:
        stats["held"] = held
    return stats
