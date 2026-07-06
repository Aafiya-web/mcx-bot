"""Paper-mode preflight self-check — the "is the bot runnable?" report.

Run:  venv\\Scripts\\python.exe scripts\\preflight.py

Grows with each build step; from step 1 onward it must always exit 0 in a
fresh checkout with an empty .env (paper mode needs no configuration).
Exits non-zero only on a real failure (e.g. live mode requested while
unconfigured, DB not writable).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings, symbols          # noqa: E402
from database import models                   # noqa: E402
from notifications import telegram            # noqa: E402


def main() -> int:
    failures: list[str] = []
    mode = "LIVE" if settings.LIVE_TRADING else "PAPER"

    print("=" * 58)
    print("MCX TRADING BOT — PREFLIGHT")
    print("=" * 58)
    print(f"Mode            : {mode}")

    # 1. Config gate
    try:
        settings.validate_live_config()
        if settings.LIVE_TRADING:
            print("Config gate     : OK (live fully configured)")
        else:
            unset = [n for n in ("INITIAL_CAPITAL", "MAX_DAILY_LOSS_PCT",
                                 "MAX_DRAWDOWN_PCT")
                     if getattr(settings, n) is None]
            note = f"(money placeholders unset: {', '.join(unset)} — " \
                   f"fine for paper)" if unset else ""
            print(f"Config gate     : OK {note}")
            print(f"Paper capital   : ₹{settings.PAPER_CAPITAL:,.0f} "
                  f"(simulation only)")
    except settings.ConfigError as e:
        failures.append(f"Config gate: {e}")
        print(f"Config gate     : FAIL — {e}")

    # 2. Database
    try:
        models.init_db()
        models.set_state("preflight_ok", "1")
        assert models.get_state("preflight_ok") == "1"
        halted = models.is_halted()
        print(f"Database        : OK ({settings.DB_FILE.name}, WAL)")
        print(f"Halt flag       : {'SET — trading halted!' if halted else 'clear'}")
        if halted:
            failures.append("Halt flag is set — manual review required")
    except Exception as e:
        failures.append(f"Database: {e}")
        print(f"Database        : FAIL — {e}")

    # 3. Telegram
    print(f"Telegram        : {'configured' if telegram.enabled() else 'disabled (no token — OK for paper)'}")

    # 4. Broker credentials (informational until step 2 wires the broker)
    creds = all([settings.ANGEL_API_KEY, settings.ANGEL_CLIENT_ID,
                 settings.ANGEL_PASSWORD, settings.ANGEL_TOTP_KEY])
    print(f"Angel One creds : {'present' if creds else 'absent (mock/paper data only)'}")

    # 5. Instrument lineup
    print(f"Mini contracts  : {'ON' if symbols.USE_MINI_CONTRACTS else 'off'}")
    print("-" * 58)
    print(f"{'INSTRUMENT':<12} {'TRADES AS':<13} {'STRATEGY':<22} {'TF':<15} CLUSTER")
    for base in symbols.ACTIVE_SYMBOLS:
        info = symbols.INSTRUMENTS[base]
        print(f"{base:<12} {symbols.active_symbol(base):<13} "
              f"{info['strategy']:<22} {info['timeframe']:<15} "
              f"{info['cluster']}")
    print("-" * 58)

    if failures:
        print(f"PREFLIGHT FAILED ({len(failures)}):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("PREFLIGHT OK — paper mode is runnable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
