"""Correlation filter (mcx-correlation-filter skill): two trades that move
together are one bigger bet in disguise. Blocks same-direction risk within
a cluster and caps total cluster margin utilisation."""

from config import settings
from config.symbols import CORRELATION_CLUSTERS, cluster_of
from risk.position_sizing import estimated_margin


def correlation_check(new_symbol: str, new_side: str,
                      open_positions: list[dict]) -> tuple[bool, str]:
    """Block stacking same-direction exposure inside a correlated cluster.

    open_positions: dicts with at least symbol + side (database rows work).
    """
    cluster = cluster_of(new_symbol)
    if cluster is None:
        return True, "no cluster — trade allowed"

    members = CORRELATION_CLUSTERS[cluster]["members"]
    for pos in open_positions:
        if cluster_of(pos["symbol"]) == cluster and pos["side"] == new_side:
            return False, (
                f"BLOCKED: already {new_side} {pos['symbol']} in {cluster} "
                f"cluster ({', '.join(members)}). Adding {new_symbol} "
                f"{new_side} would double correlated exposure."
            )
    return True, f"allowed — no conflicting {cluster} exposure"


def cluster_exposure_ok(new_symbol: str, new_lots: int, new_price: float,
                        open_positions: list[dict], capital: float,
                        max_cluster_pct: float | None = None
                        ) -> tuple[bool, str]:
    """Cap total estimated margin per cluster at MAX_CLUSTER_MARGIN_PCT
    of capital."""
    cluster = cluster_of(new_symbol)
    if cluster is None:
        return True, "no cluster limit applies"
    max_pct = (max_cluster_pct if max_cluster_pct is not None
               else settings.MAX_CLUSTER_MARGIN_PCT)

    current = sum(
        estimated_margin(pos["symbol"], pos["entry_price"], pos["qty"])
        for pos in open_positions if cluster_of(pos["symbol"]) == cluster
    )
    total = current + estimated_margin(new_symbol, new_price, new_lots)
    limit = capital * max_pct / 100
    if total > limit:
        return False, (f"BLOCKED: {cluster} margin would be ~₹{total:,.0f}, "
                       f"over the ₹{limit:,.0f} cap ({max_pct}%)")
    return True, f"{cluster} margin OK (~₹{total:,.0f} / ₹{limit:,.0f})"
