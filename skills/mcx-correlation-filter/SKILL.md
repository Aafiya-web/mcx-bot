---
name: mcx-correlation-filter
description: >
  Prevents doubled-up correlated exposure across MCX commodities. Use this skill
  when the user asks about correlation, over-exposure, whether two positions are
  too similar, energy or metals cluster risk, or why a trade was blocked. Also
  triggers for "am I too exposed to energy?", "should I take gold AND silver?",
  "is this doubling my risk?", or any portfolio-level correlation question.
---

# MCX Correlation Filter

Stops the bot from piling into positions that move together — which secretly
doubles real risk while looking like diversification. Adapted from the
SPY/QQQ-BTC correlation-filter idea, mapped to MCX commodity clusters.

## Correlation Clusters (MCX)

```python
# Commodities that move together — treat as shared exposure
CORRELATION_CLUSTERS = {
    "ENERGY": {
        "members": ["CRUDEOIL", "NATURALGAS"],
        "note": "Both driven by energy demand, EIA data, geopolitics",
        "correlation": 0.6,   # Approximate positive correlation
    },
    "PRECIOUS_METALS": {
        "members": ["GOLD", "SILVER"],
        "note": "Both driven by USD, Fed policy, risk sentiment. "
                "Silver = higher beta version of gold",
        "correlation": 0.8,   # Strong positive correlation
    },
    "BASE_METALS": {
        "members": ["COPPER", "ZINC", "ALUMINIUM"],
        "note": "Industrial demand, China growth, USD",
        "correlation": 0.65,
    },
}

# Inverse relationships (often move opposite)
INVERSE_PAIRS = {
    ("GOLD", "CRUDEOIL"): "Weak — gold is risk-off, crude is risk-on",
    # Add observed relationships as data accumulates
}
```

## Core Rule

```python
def correlation_check(new_symbol, new_side, open_positions):
    """
    Block a new trade if it would stack same-direction risk within
    a correlated cluster.

    Returns: (allowed: bool, reason: str)
    """
    cluster = find_cluster(new_symbol)
    if not cluster:
        return True, "No cluster — trade allowed"

    # Find existing positions in the same cluster
    cluster_members = CORRELATION_CLUSTERS[cluster]["members"]
    same_cluster_positions = [
        p for p in open_positions
        if p['symbol'] in cluster_members
    ]

    # Block if already holding same-direction risk in this cluster
    for pos in same_cluster_positions:
        if pos['side'] == new_side:
            return False, (
                f"BLOCKED: Already {new_side} {pos['symbol']} in "
                f"{cluster} cluster. Adding {new_symbol} {new_side} "
                f"would double correlated exposure."
            )

    return True, f"Allowed — no conflicting {cluster} exposure"

def find_cluster(symbol):
    for cluster, data in CORRELATION_CLUSTERS.items():
        if symbol in data["members"]:
            return cluster
    return None
```

## Exposure Budget (Optional Advanced Rule)

Instead of a hard block, cap total exposure per cluster:

```python
def cluster_exposure_ok(new_symbol, new_qty, new_price,
                        open_positions, capital, max_cluster_pct=15.0):
    """
    Limit total capital deployed in any one correlation cluster.
    E.g. no more than 15% of capital across all energy positions combined.
    """
    cluster = find_cluster(new_symbol)
    if not cluster:
        return True, "No cluster limit applies"

    members = CORRELATION_CLUSTERS[cluster]["members"]
    current_exposure = sum(
        p['qty'] * p['entry_price']
        for p in open_positions if p['symbol'] in members
    )
    new_exposure = new_qty * new_price
    total = current_exposure + new_exposure
    limit = capital * max_cluster_pct / 100

    if total > limit:
        return False, (
            f"BLOCKED: {cluster} exposure would be ₹{total:.0f}, "
            f"over the ₹{limit:.0f} cap ({max_cluster_pct}%)"
        )
    return True, f"{cluster} exposure OK (₹{total:.0f} / ₹{limit:.0f})"
```

## Output Format

```
🔗 CORRELATION CHECK: CRUDEOIL BUY
----------------------------------
Cluster    : ENERGY (CRUDEOIL, NATURALGAS)
Open in cluster: NATURALGAS BUY (1 lot)
Decision   : ❌ BLOCKED
Reason     : Already long energy via NATURALGAS.
             A CRUDEOIL long doubles energy exposure.
Suggestion : Skip, or close NATURALGAS first.
```

## Why This Matters

Two "different" trades that move together aren't diversification — they're one
bigger bet in disguise. If crude and natural gas both tank on a demand shock,
holding both long means taking the full hit twice. The filter keeps the bot's
real risk equal to what the position sizer *thinks* the risk is.

## Integration Point

Call this in the risk-management gate, AFTER position sizing but BEFORE the
portfolio manager's final approval. It can veto or resize a trade. Chain:

```
signal -> regime OK -> risk sizing -> CORRELATION CHECK -> portfolio guard -> execute
```
