"""Events: everything the organization knows about one activity, kept on the server so every
officer and every form can use it.

An event is one JSON document:

    {"id", "name", "created_at", "updated_at",
     "notes":     [{"id", "text", "source": "user" | "assistant", "created_at"}],
     "files":     [{"id", "name", "kind": "image" | "document", "pages": [image ids]}],
     "documents": {template_id: {"state": {...}, "chat": [...], "updated_at"}}}

Notes and files are the event's context: the assistant reads them when filling any form for the
event, and facts it learns in a chat are added back as notes (source "assistant"). Documents hold
each form's draft so it can be reopened later or by another officer.

Stored in SQLite: data/ascend.db by default, ASCEND_DB to change it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from . import files as docfiles
from .registry import ROOT

DB_PATH = Path(os.environ.get("ASCEND_DB", ROOT / "data" / "ascend.db"))
MAX_CONTEXT = 80_000      # characters of event context given to the AI
MAX_EVENT_IMAGES = 12     # saved images sent to the AI when a form is filled from the event
_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)")
    return con


def _save(con, event: dict):
    event["updated_at"] = _now()
    con.execute("INSERT OR REPLACE INTO events (id, data, updated_at) VALUES (?, ?, ?)",
                (event["id"], json.dumps(event, ensure_ascii=False), event["updated_at"]))


def summary(event: dict) -> dict:
    return {"id": event["id"], "name": event["name"], "updated_at": event["updated_at"],
            "notes": len(event.get("notes", [])), "files": len(event.get("files", [])),
            "documents": sorted(event.get("documents", {}))}


def list_events() -> list[dict]:
    with _lock, _connect() as con:
        rows = con.execute("SELECT data FROM events ORDER BY updated_at DESC").fetchall()
    return [summary(json.loads(r[0])) for r in rows]


def get_event(eid: str) -> dict | None:
    with _lock, _connect() as con:
        row = con.execute("SELECT data FROM events WHERE id = ?", (eid,)).fetchone()
    return json.loads(row[0]) if row else None


def create_event(name: str, notes: str = "") -> dict:
    event = {"id": uuid.uuid4().hex[:12], "name": name, "created_at": _now(), "updated_at": _now(),
             "notes": [], "files": [], "documents": {}}
    if notes.strip():
        event["notes"].append(new_note(notes))
    with _lock, _connect() as con:
        _save(con, event)
    return event


def update_event(eid: str, change) -> dict | None:
    """Read-modify-write one event under the lock; change(event) edits it in place."""
    with _lock, _connect() as con:
        row = con.execute("SELECT data FROM events WHERE id = ?", (eid,)).fetchone()
        if not row:
            return None
        event = json.loads(row[0])
        change(event)
        _save(con, event)
    return event


def delete_event(eid: str) -> bool:
    with _lock, _connect() as con:
        return con.execute("DELETE FROM events WHERE id = ?", (eid,)).rowcount > 0


def new_note(text: str, source: str = "user") -> dict:
    return {"id": uuid.uuid4().hex[:10], "text": text.strip(), "source": source, "created_at": _now()}


# ---------------------------------------------------------------- context for the AI

def _doc_values_text(tpl, state: dict, config: dict) -> str:
    from .registry import resolved_values
    lines, values = [], resolved_values(tpl, state, config)
    for f in tpl.fields:
        v = values.get(f["id"])
        if not v or f["type"] == "images":
            continue
        if f["type"] == "table":
            v = "\n".join("   | " + " | ".join(row) for row in v)
            lines.append(f"  {f['label']} ({' | '.join(f['columns'])}):\n{v}")
        else:
            lines.append(f"  {f['label']}: {v}")
    return "\n".join(lines)


def context_text(event: dict, upload_dir: Path, current_tid: str, config: dict) -> str:
    """Everything saved about the event, as text for the system prompt."""
    from .registry import get_template
    parts = [f'Event name: {event["name"]}']
    notes = event.get("notes", [])
    if notes:
        parts.append("Notes saved for this event:\n" + "\n".join(f"- {n['text']}" for n in notes))
    images = []
    for f in event.get("files", []):
        if f.get("kind") == "document":
            doc = docfiles.load_document(f["id"], upload_dir)
            if doc:
                parts.append(docfiles.as_message(*doc).strip())
        else:
            images.append(f"{f['id']} ({f['name']})")
    if images:
        parts.append("Images saved for this event (ids you may use in image fields): " + ", ".join(images))
    for tid, d in event.get("documents", {}).items():
        if tid == current_tid:
            continue
        try:
            tpl = get_template(tid)
        except KeyError:
            continue
        text = _doc_values_text(tpl, d.get("state") or {}, config)
        if text:
            parts.append(f'Already filled in this event\'s "{tpl.name}":\n{text}')
    out = "\n\n".join(parts)
    return out if len(out) <= MAX_CONTEXT else out[:MAX_CONTEXT] + "\n[... event information cut off ...]"


def context_images(event: dict) -> list[tuple[str, str, str]]:
    """(upload id, file name, source) of saved images and document pages, for filling a form."""
    out = []
    for f in event.get("files", []):
        if f.get("kind") == "document":
            out += [(p, f["name"], f"page {n} of {f['name']}") for n, p in enumerate(f.get("pages", []), 1)]
        else:
            out.append((f["id"], f["name"], ""))
    return out[:MAX_EVENT_IMAGES]
