#!/usr/bin/env python3
"""Controller-only local observations (stdlib).

This module never runs a model, edits an active skill, retries denied operations,
changes a task verdict, or automatically copies project context into shared data.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

STATES = {"open", "actioned", "declined", "superseded", "parked"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError(f"Symlink storage paths are not allowed: {part}")
    return path.resolve()


def _root(project_root):
    root = _safe(project_root)
    if not root.is_dir():
        raise ValueError("Project root must be an existing directory")
    return root.resolve()


def _local(root, path):
    p = _safe(root / path)
    if not p.is_relative_to(root):
        raise ValueError("Evidence must stay within its source project")
    if not p.is_file():
        raise ValueError(f"Evidence file missing: {p}")
    return {"path": p.relative_to(root).as_posix(), "sha256": _sha(p.read_bytes())}


@contextmanager
def _db(project_root):
    root = _root(project_root)
    directory = _safe(root / ".kh-observer")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = _safe(directory / "observations.sqlite3")
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS observations (
          id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          version INTEGER NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS occurrences (
          id TEXT PRIMARY KEY, observation_id TEXT NOT NULL, provenance TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        conn.execute("BEGIN IMMEDIATE")
        yield conn, root
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_observation(project_root, *, title, issue, suggestion, principle,
                       skill, run_id, phase, source_revision, evidence=()):
    """Record/deduplicate local content; return id, version, status, deduplicated.

    Every call retains separate run/phase/revision/evidence provenance. Caller is
    the controller. Workers submit dictionaries and never invoke the store.
    """
    fields = dict(title=title, issue=issue, suggestion=suggestion, principle=principle,
                  skill=skill, run_id=run_id, phase=phase, source_revision=source_revision)
    if any(not isinstance(v, str) or not v.strip() for v in fields.values()):
        raise ValueError("All observation and provenance fields must be nonempty strings")
    payload = {k: fields[k] for k in ("title", "issue", "suggestion", "principle", "skill")}
    fingerprint = _sha(_json(payload).encode())
    with _db(project_root) as (db, root):
        refs = [_local(root, p) for p in evidence]
        row = db.execute("SELECT * FROM observations WHERE fingerprint=?", (fingerprint,)).fetchone()
        ident = row["id"] if row else str(uuid.uuid4())
        if row is None:
            db.execute("INSERT INTO observations VALUES (?, ?, 'open', 1, ?, ?)",
                       (ident, fingerprint, _json(payload), _now()))
        provenance = dict(run_id=run_id, phase=phase, source_revision=source_revision,
                          evidence=refs, recorded_at=_now())
        db.execute("INSERT INTO occurrences VALUES (?, ?, ?)",
                   (str(uuid.uuid4()), ident, _json(provenance)))
        return dict(id=ident, version=row["version"] if row else 1,
                    status=row["status"] if row else "open", deduplicated=row is not None)


def list_observations(project_root, status=None):
    if status is not None and status not in STATES:
        raise ValueError("Unknown observation status")
    with _db(project_root) as (db, _):
        rows = db.execute("SELECT * FROM observations ORDER BY created, id").fetchall()
        result = []
        for row in rows:
            if status and row["status"] != status:
                continue
            rec = dict(row)
            rec["payload"] = json.loads(rec["payload"])
            rec["occurrences"] = [json.loads(r[0]) for r in db.execute(
                "SELECT provenance FROM occurrences WHERE observation_id=? ORDER BY rowid", (row["id"],))]
            result.append(rec)
        return result


def set_status(project_root, ident, status, expected_version):
    if status not in STATES:
        raise ValueError("Unknown observation status")
    with _db(project_root) as (db, _):
        n = db.execute("UPDATE observations SET status=?, version=version+1 WHERE id=? AND version=?",
                       (status, ident, expected_version)).rowcount
        if n != 1:
            raise ValueError("Observation update conflict: reload current version")
    return expected_version + 1


def review_due(last_review, open_count, today=None, interval_days=7):
    """Never/stale/invalid dates trigger an offer when observations are open."""
    today = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
    if interval_days < 1 or open_count < 0:
        raise ValueError("Invalid review interval/count")
    invalid = False
    if not last_review or last_review == "never":
        reason, due = "never", True
    else:
        try:
            previous = date.fromisoformat(last_review)
            invalid = previous > today
            due = invalid or (today - previous).days >= interval_days
            reason = "invalid_date" if invalid else ("stale" if due else "recent")
        except (ValueError, TypeError):
            invalid, due, reason = True, True, "invalid_date"
    return {"offer_review": bool(open_count and due), "reason": reason,
            "invalid_date": invalid, "open_count": open_count}


def review_status(project_root, today=None):
    with _db(project_root) as (db, _):
        count = db.execute("SELECT count(*) FROM observations WHERE status='open'").fetchone()[0]
        row = db.execute("SELECT value FROM metadata WHERE key='last_review'").fetchone()
        result = review_due(row[0] if row else "never", count, today)
        result["last_review"] = row[0] if row else "never"
        return result


def mark_reviewed(project_root, reviewed_on=None):
    value = reviewed_on or date.today().isoformat()
    if date.fromisoformat(value) > date.today():
        raise ValueError("Review date cannot be in the future")
    with _db(project_root) as (db, _):
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_review', ?)", (value,))


def promote_lesson(project_root, ident, shared_root, *, sanitized_lesson,
                   reviewed_by, sanitization_confirmed=False):
    """Explicit reviewed export. Never infers a lesson from raw project content."""
    if sanitization_confirmed is not True or not reviewed_by.strip() or not sanitized_lesson.strip():
        raise ValueError("Explicit sanitized text, reviewer, and confirmation required")
    root = _root(project_root)
    rows = list_observations(root)
    if not any(r["id"] == ident for r in rows):
        raise ValueError("Unknown observation")
    target = _safe(shared_root)
    if target == root or target.is_relative_to(root):
        raise ValueError("Shared knowledge directory must be separate from the source project")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Only caller-supplied sanitized text and opaque provenance leave the project.
    body = {"lesson": sanitized_lesson, "reviewed_by": reviewed_by,
            "source_ref": _sha((str(root) + '\0' + ident).encode()),
            "source_observation_id": ident, "promoted_at": _now()}
    path = target / (str(uuid.uuid4()) + ".json")
    with path.open("x", encoding="utf-8") as f:
        f.write(_json(body) + "\n")
    return str(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    sub = ap.add_subparsers(dest="command", required=True)
    add = sub.add_parser("record")
    add.add_argument("--json", required=True, help="JSON file with record_observation keyword fields")
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=sorted(STATES))
    reminder = sub.add_parser("review-status")
    reminder.add_argument("--today")
    sub.add_parser("mark-reviewed")
    st = sub.add_parser("status")
    st.add_argument("id"); st.add_argument("status", choices=sorted(STATES)); st.add_argument("--version", type=int, required=True)
    prom = sub.add_parser("promote")
    prom.add_argument("id"); prom.add_argument("--shared-root", required=True)
    prom.add_argument("--lesson-file", required=True); prom.add_argument("--reviewed-by", required=True)
    prom.add_argument("--confirm-sanitized", action="store_true")
    args = ap.parse_args()
    try:
        if args.command == "record":
            result = record_observation(args.project, **json.loads(Path(args.json).read_text()))
        elif args.command == "list": result = list_observations(args.project, args.status)
        elif args.command == "review-status": result = review_status(args.project, args.today)
        elif args.command == "mark-reviewed": result = mark_reviewed(args.project)
        elif args.command == "status": result = set_status(args.project, args.id, args.status, args.version)
        else:
            result = promote_lesson(args.project, args.id, args.shared_root,
                                    sanitized_lesson=Path(args.lesson_file).read_text(),
                                    reviewed_by=args.reviewed_by, sanitization_confirmed=args.confirm_sanitized)
        print(_json({"ok": True, "result": result}))
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as exc:
        print(_json({"ok": False, "error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
