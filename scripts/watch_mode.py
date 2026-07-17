"""(Re)arm first-trades watch mode: heightened Telegram verbosity for the
next N closed trades (entry gate-chain detail + exit fill/slippage detail).

    venv/bin/python scripts/watch_mode.py        # arm for 5 trades
    venv/bin/python scripts/watch_mode.py 10     # arm for 10
    venv/bin/python scripts/watch_mode.py 0      # disarm now

Temporary scaffolding by design — it auto-disables after N closed trades.
Re-arm after any major pipeline change so the first trades through new
code paths are fully observable (see HANDOFF.md).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings          # noqa: E402
from database import models          # noqa: E402


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else settings.WATCH_FIRST_TRADES
    models.init_db()
    models.set_state("watch_mode_remaining", str(n))
    print(f"watch mode armed for the next {n} closed trade(s)"
          if n > 0 else "watch mode disarmed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
