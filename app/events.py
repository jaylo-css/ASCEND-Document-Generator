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

Stored by app/storage.py: SQLite locally (data/ascend.db, ASCEND_DB to change), or Cloud Firestore
when FIREBASE_CREDENTIALS is set.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import uuid
from pathlib import Path

from . import files as docfiles
from . import storage
from .registry import ROOT

DB_PATH = Path(os.environ.get("ASCEND_DB", ROOT / "data" / "ascend.db"))
MAX_CONTEXT = 80_000      # characters of event context given to the AI
MAX_EVENT_IMAGES = 12     # saved images sent to the AI when a form is filled from the event
_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _store():
    return storage.events_backend(DB_PATH)


def summary(event: dict) -> dict:
    return {"id": event["id"], "name": event["name"], "updated_at": event["updated_at"],
            "notes": len(event.get("notes", [])), "files": len(event.get("files", [])),
            "documents": sorted(event.get("documents") or {})}


def list_events() -> list[dict]:
    with _lock:
        return [summary(e) for e in _store().list()]


def get_event(eid: str) -> dict | None:
    with _lock:
        return _store().get(eid)


def create_event(name: str, notes: str = "") -> dict:
    event = {"id": uuid.uuid4().hex[:12], "name": name, "created_at": _now(), "updated_at": _now(),
             "notes": [], "files": [], "documents": {}}
    if notes.strip():
        event["notes"].append(new_note(notes))
    with _lock:
        _store().put(event)
    return event


def update_event(eid: str, change) -> dict | None:
    """Read-modify-write an event's name, notes or files under the lock; change(event) edits it in place.
    Drafts are saved with save_document()."""
    with _lock:
        store = _store()
        event = store.get(eid)
        if event is None:
            return None
        change(event)
        event["updated_at"] = _now()
        store.put(event)
    return event


def save_document(eid: str, tid: str, doc: dict) -> bool:
    with _lock:
        return _store().put_document(eid, tid, doc, _now())


def delete_event(eid: str) -> bool:
    with _lock:
        return _store().delete(eid)


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
