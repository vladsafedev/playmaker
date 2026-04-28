"""Remove the probe row from Zed's sidebar_threads after path-B verification.

Zed MUST be closed when this runs (Cmd+Q first), otherwise sqlite is locked.
"""

import sqlite3
import sys
import uuid
from pathlib import Path

DB = Path("~/Library/Application Support/Zed/db/0-stable/db.sqlite").expanduser()
NAMESPACE = uuid.UUID("0e6e7d4a-3a3c-4f6a-9c4e-1f7e2c1ab842")
AGENT_ID = "session-load-probe"
SESSION_ID = "probe-session-aaa-bbb-ccc"


def main() -> None:
    if not DB.exists():
        sys.exit(f"Zed DB not found at {DB}")
    thread_id = uuid.uuid5(NAMESPACE, f"{AGENT_ID}:{SESSION_ID}").bytes

    con = sqlite3.connect(str(DB), timeout=10.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        cur = con.execute(
            "DELETE FROM sidebar_threads WHERE thread_id = ?", (thread_id,)
        )
        con.commit()
        print(f"Removed probe row (rows affected: {cur.rowcount})")
    finally:
        con.close()


if __name__ == "__main__":
    main()
