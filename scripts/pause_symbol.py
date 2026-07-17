"""Pause / resume new entries on one instrument (open positions are
unaffected — the monitor keeps managing them).

    venv/bin/python scripts/pause_symbol.py CRUDEOIL "reason text"
    venv/bin/python scripts/pause_symbol.py CRUDEOIL --clear

The pause is a bot_state key (`symbol_pause:<BASE>`) the engine checks
before scanning the symbol; the scanner panel shows `paused: <reason>`
every cycle. Setting/clearing writes ONE decision_log row so the quiet
days stay explained in the audit trail. Contract maintenance auto-clears
a pause when the instrument rolls to a new contract month.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.symbols import ACTIVE_SYMBOLS          # noqa: E402
from database import models                         # noqa: E402
from notifications.telegram import send_message     # noqa: E402


def pause(base: str, reason: str, db_path=None) -> None:
    models.set_state(f"symbol_pause:{base}", reason, db_path)
    models.log_decision("system", base, "system", "PAUSE", reason,
                        db_path=db_path)
    send_message(f"⏸️ <b>{base} entries PAUSED</b>\n{reason}")


def resume(base: str, reason: str = "manual resume", db_path=None) -> None:
    models.set_state(f"symbol_pause:{base}", "", db_path)
    models.log_decision("system", base, "system", "RESUME", reason,
                        db_path=db_path)
    send_message(f"▶️ <b>{base} entries RESUMED</b>\n{reason}")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base = sys.argv[1].upper()
    if base not in ACTIVE_SYMBOLS:
        print(f"unknown symbol {base!r} — active: {list(ACTIVE_SYMBOLS)}")
        return 2
    models.init_db()
    if sys.argv[2] == "--clear":
        resume(base)
        print(f"{base} resumed")
    else:
        pause(base, " ".join(sys.argv[2:]))
        print(f"{base} paused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
