"""Manually clear the circuit-breaker halt — the ONLY way trading resumes
after a trip. Run it on the host, deliberately, after reviewing what
happened:

    venv\\Scripts\\python.exe scripts\\clear_halt.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import models  # noqa: E402


def main() -> int:
    models.init_db()
    if not models.is_halted():
        print("Halt flag is already clear — nothing to do.")
        return 0
    print("Halt flag is SET (circuit breaker or manual /halt).")
    answer = input("Have you reviewed the cause and want to clear it? "
                   "Type YES to confirm: ")
    if answer.strip() != "YES":
        print("Aborted — halt flag unchanged.")
        return 1
    models.set_halted(False)
    print("Halt flag cleared. The bot may trade again on its next tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
