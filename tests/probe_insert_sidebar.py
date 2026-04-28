"""Insert a probe row into Zed's sidebar_threads pointing at session-load-probe.

Zed MUST be closed when this runs — otherwise sqlite is locked. Cmd+Q first.

After running this, reopen Zed, open the workspace at /Users/shulyugin/Sites/team,
click the row titled 'PROBE — does Zed send session/load?' in the sidebar,
then read /tmp/acp_probe.log.
"""

import datetime
import sqlite3
import sys
import uuid
from pathlib import Path

DB = Path("~/Library/Application Support/Zed/db/0-stable/db.sqlite").expanduser()

# Same namespace playmaker uses, so probe rows don't collide with real ones.
NAMESPACE = uuid.UUID("0e6e7d4a-3a3c-4f6a-9c4e-1f7e2c1ab842")

AGENT_ID = "session-load-probe"
SESSION_ID = "probe-session-aaa-bbb-ccc"
TITLE = "PROBE — does Zed send session/load?"
FOLDER = "/Users/shulyugin/Sites/team"


def main() -> None:
    if not DB.exists():
        sys.exit(f"Zed DB not found at {DB}")

    thread_id = uuid.uuid5(NAMESPACE, f"{AGENT_ID}:{SESSION_ID}").bytes
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    con = sqlite3.connect(str(DB), timeout=10.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute(
            """
            INSERT INTO sidebar_threads (
                thread_id, session_id, agent_id, title,
                updated_at, created_at, interacted_at,
                folder_paths, folder_paths_order,
                main_worktree_paths, main_worktree_paths_order,
                archived
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(thread_id) DO UPDATE SET
                title = excluded.title,
                updated_at = excluded.updated_at,
                interacted_at = excluded.interacted_at
            """,
            (
                thread_id, SESSION_ID, AGENT_ID, TITLE,
                now, now, now,
                FOLDER, "0",
                FOLDER, "0",
            ),
        )
        con.commit()
    finally:
        con.close()

    print(f"Inserted probe row")
    print(f"  thread_id  : {thread_id.hex()}")
    print(f"  session_id : {SESSION_ID}")
    print(f"  agent_id   : {AGENT_ID}")
    print(f"  folder     : {FOLDER}")


if __name__ == "__main__":
    main()
