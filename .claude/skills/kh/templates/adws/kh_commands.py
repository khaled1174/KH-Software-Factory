#!/usr/bin/env python3
"""Read-only KH trace snapshots; does not initialize a database or signal PIDs."""
import os
if os.name == "nt":
    raise SystemExit("Native Windows is not supported. Use WSL2 (not WSL1) with Ubuntu; run KH and install.py inside Ubuntu, with the project under ~/ (not /mnt/c).")
import argparse
import json
from pathlib import Path
import sqlite3
import sys

QUERIES = {
    "sessions": "SELECT adw_id,status,substr(request,1,50) AS request,total_tokens,round(total_cost,4) AS cost FROM sessions ORDER BY started_at DESC LIMIT 10",
    "phases": "SELECT seq,name,kind,owner,status,attempt FROM phases WHERE adw_id=? ORDER BY seq",
    "tail": "SELECT rowid,type,name,started_at FROM events WHERE adw_id=? ORDER BY rowid DESC LIMIT 25",
    "procs": "SELECT kind,name,pid,command,started_at FROM processes WHERE adw_id=? AND ended_at IS NULL ORDER BY id",
}


def snapshot(database: Path, action: str, run_id=None) -> dict:
    if not database.is_file():
        raise ValueError(f"No trace database exists: {database}")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        if action != "sessions" and run_id is None:
            row = connection.execute("SELECT adw_id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
            if row is None:
                return {"action": action, "run_id": None, "rows": []}
            run_id = row[0]
        query = QUERIES[action]
        if action == "sessions":
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            known = "cost_known" if "cost_known" in columns else "NULL AS cost_known"
            query = query.replace(" FROM sessions", f",{known} FROM sessions")
        rows = connection.execute(query, () if action == "sessions" else (run_id,)).fetchall()
        result = [dict(row) for row in rows]
        if action == "sessions":
            for row in result:
                row["cost_known"] = row["cost_known"] == 1
                row["known_cost_subtotal"] = row["cost"]
                if not row["cost_known"]:
                    row["cost"] = None
        return {"action": action, "run_id": run_id, "rows": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=QUERIES)
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--db", type=Path, default=Path("adws/adw_data/kh.db"))
    args = parser.parse_args()
    if args.action == "sessions" and args.run_id is not None:
        parser.error("sessions does not accept a run ID")
    try:
        print(json.dumps(snapshot(args.db, args.action, args.run_id), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
