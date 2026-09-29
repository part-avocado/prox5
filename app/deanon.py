"""
Root/shell-only tool to decrypt a single prox5 submission's author Slack ID.

There is nothing in the bot itself, and nothing in Slack at all, that can do
this — no button, no admin role, no "report" feature. Deanonymizing is only
ever legitimate for a submission that was already sent through (approved and
posted to the public channel); this tool refuses to run on anything still a
draft, pending review, or rejected, since there's no one left to complain
about and nothing to act on.

It's also deliberately not automated or convenient:

  1. You need shell access to the host/container running prox5 (e.g.
     `docker exec -it prox5 python3 deanon.py <id>`), and the same
     CONFESSIONS_KEY / DB_PATH the bot uses.
  2. You need the confession's internal database `id`. This tool does not
     look submissions up by public post number, search text, or anything
     else — find it yourself with a direct query, e.g.:
         sqlite3 data/confessions.db \\
             "SELECT id, number, status FROM confessions WHERE number = 42;"
  3. You must retype a fresh one-time code printed to the terminal. It's
     regenerated every run, so it can't be scripted, piped, or replayed
     from shell history.
  4. You must type a non-empty reason for the lookup.
  5. You then wait out a mandatory countdown before the ID is shown.

Every attempt — successful, wrong status, wrong code, empty reason, or
aborted — is appended to the same append-only, 0600-permissioned log
(DEANON_LOG_PATH, default data/deanon.log). That log, plus this script, is
the entire deanonymization surface. Nothing about it is reachable over the
network or from Slack. Note also that the author can permanently delete a
submission themselves (including an already-approved one) at any time,
which removes the row this tool depends on entirely.

Usage:
    python3 deanon.py <confession_id>
"""

import json
import os
import random
import sqlite3
import string
import sys
import time
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "data/confessions.db")
DEANON_LOG_PATH = os.environ.get(
    "DEANON_LOG_PATH", os.path.join(os.path.dirname(DB_PATH) or ".", "deanon.log")
)
DEANON_DELAY_SECONDS = int(os.environ.get("DEANON_DELAY_SECONDS", "20"))
OPERATOR = os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"


def audit(event: str, **fields):
    d = os.path.dirname(DEANON_LOG_PATH) or "."
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "operator": OPERATOR, **fields}
    fd = os.open(DEANON_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def fail(cid, reason, message):
    audit("deanon_attempt", confession_id=cid, result=reason)
    print(message)
    sys.exit(1)


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        sys.exit(1)
    cid = int(sys.argv[1])

    key = os.environ.get("CONFESSIONS_KEY")
    if not key:
        print("CONFESSIONS_KEY is not set in this environment.")
        sys.exit(1)
    fernet = Fernet(key.encode())

    if not os.path.exists(DB_PATH):
        print(f"No database at {DB_PATH}.")
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM confessions WHERE id=?", (cid,)).fetchone()
    if not row:
        fail(cid, "not_found", f"No confession with id={cid}.")

    if row["status"] != "approved" or not row["pub_ts"]:
        fail(
            cid, f"refused_status_{row['status']}",
            f"id={cid} has status={row['status']!r} and was never sent through to the channel. "
            "Deanonymization is only permitted for submissions that were actually posted.",
        )

    print(f"id={row['id']}  number={row['number']}  status={row['status']}  dm_ts={row['dm_ts']}")
    print()
    print("This will permanently log a decrypted Slack ID as viewed by an administrator.")
    print()

    challenge = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    typed = input(f"Type this one-time code to continue: {challenge}\n> ").strip()
    if typed != challenge:
        fail(cid, "challenge_failed", "Code did not match. Aborting.")

    reason = input("Reason for this lookup (required, will be logged): ").strip()
    if not reason:
        fail(cid, "no_reason", "A reason is required. Aborting.")

    print(f"\nProceeding in {DEANON_DELAY_SECONDS}s. Ctrl-C to abort.")
    try:
        for remaining in range(DEANON_DELAY_SECONDS, 0, -1):
            print(f"  {remaining}... ", end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        fail(cid, "aborted", "\nAborted.")
    print()

    author = fernet.decrypt(row["user_enc"].encode()).decode()
    print(f"Author Slack ID: {author}")

    audit(
        "deanon_success",
        confession_id=cid,
        confession_number=row["number"],
        reason=reason,
        author_slack_id=author,
    )


if __name__ == "__main__":
    main()
