"""Where events and uploaded files are kept.

- Without configuration everything is local: events in SQLite (data/ascend.db, ASCEND_DB to change),
  uploads as files in data/uploads.
- With FIREBASE_CREDENTIALS set (a path to a Firebase service-account .json key, or the key's JSON
  itself), events are kept in Cloud Firestore and every uploaded file is also copied into Firestore,
  so nothing is lost when the server's disk is wiped (e.g. Render's free plan restarting). Files are
  read back into the local upload folder on first use.

Firestore layout (only the server's admin key can read it; keep the database in production mode):

    events/{event id}                       {"data": event JSON without drafts, "doc_ids": [...], "updated_at"}
    events/{event id}/documents/{form id}   {"data": draft JSON}
    uploads/{file key}                      {"uid", "path", "size", "chunks", "created_at"}
    upload_chunks/{file key}~{n}            {"data": bytes}      (<= 900 KB each, Firestore's limit is 1 MiB)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from .registry import ROOT

CHUNK = 900_000
_db = None
_db_lock = threading.Lock()


def firestore_client():
    """The Firestore client, or None when FIREBASE_CREDENTIALS is not set."""
    global _db
    creds = os.environ.get("FIREBASE_CREDENTIALS", "").strip()
    if not creds:
        return None
    with _db_lock:
        if _db is None:
            import firebase_admin
            from firebase_admin import credentials, firestore
            cert = credentials.Certificate(json.loads(creds) if creds.startswith("{") else creds)
            app = firebase_admin.initialize_app(cert, name="ascend-docgen")
            _db = firestore.client(app)
    return _db


def backend_name() -> str:
    return "firestore" if os.environ.get("FIREBASE_CREDENTIALS", "").strip() else "sqlite"


# ================================================================ events

class SqliteEvents:
    """One row per event; the drafts live inside the event's JSON."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)")
        return con

    def list(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT data FROM events ORDER BY updated_at DESC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, eid: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT data FROM events WHERE id = ?", (eid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, event: dict):
        with self._connect() as con:
            con.execute("INSERT OR REPLACE INTO events (id, data, updated_at) VALUES (?, ?, ?)",
                        (event["id"], json.dumps(event, ensure_ascii=False), event["updated_at"]))

    def put_document(self, eid: str, tid: str, doc: dict, updated_at: str) -> bool:
        event = self.get(eid)
        if event is None:
            return False
        event.setdefault("documents", {})[tid] = doc
        event["updated_at"] = updated_at
        self.put(event)
        return True

    def delete(self, eid: str) -> bool:
        with self._connect() as con:
            return con.execute("DELETE FROM events WHERE id = ?", (eid,)).rowcount > 0


class FirestoreEvents:
    """The event (notes, files) in one Firestore document, each form's draft in its own document."""

    def __init__(self, db):
        self.col = db.collection("events")

    @staticmethod
    def _main(snap) -> dict:
        d = snap.to_dict()
        event = json.loads(d["data"])
        event["documents"] = {tid: None for tid in d.get("doc_ids", [])}  # names only, for listings
        return event

    def list(self) -> list[dict]:
        snaps = self.col.order_by("updated_at", direction="DESCENDING").stream()
        return [self._main(s) for s in snaps]

    def get(self, eid: str) -> dict | None:
        snap = self.col.document(eid).get()
        if not snap.exists:
            return None
        event = self._main(snap)
        event["documents"] = {d.id: json.loads(d.to_dict()["data"])
                              for d in self.col.document(eid).collection("documents").stream()}
        return event

    def put(self, event: dict):
        main = {k: v for k, v in event.items() if k != "documents"}
        self.col.document(event["id"]).set({
            "data": json.dumps(main, ensure_ascii=False), "updated_at": event["updated_at"],
            "doc_ids": sorted(event.get("documents") or {})})

    def put_document(self, eid: str, tid: str, doc: dict, updated_at: str) -> bool:
        from google.cloud.firestore_v1 import ArrayUnion
        ref = self.col.document(eid)
        if not ref.get().exists:
            return False
        ref.collection("documents").document(tid).set({"data": json.dumps(doc, ensure_ascii=False)})
        ref.update({"doc_ids": ArrayUnion([tid]), "updated_at": updated_at})
        return True

    def delete(self, eid: str) -> bool:
        ref = self.col.document(eid)
        if not ref.get().exists:
            return False
        for d in ref.collection("documents").stream():
            d.reference.delete()
        ref.delete()
        return True


def events_backend(sqlite_path: Path):
    db = firestore_client()
    return FirestoreEvents(db) if db is not None else SqliteEvents(sqlite_path)


# ================================================================ uploaded files

def _key(rel: str) -> str:
    return rel.replace("/", "~")


def backup(uid: str, paths: list[Path], upload_dir: Path):
    """Copy freshly uploaded files (all named <uid>.*, possibly in a subfolder) into Firestore."""
    db = firestore_client()
    if db is None:
        return
    from firebase_admin import firestore
    for path in paths:
        data = path.read_bytes()
        rel = path.relative_to(upload_dir).as_posix()
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        for start in range(0, len(chunks), 8):  # a commit may carry at most 10 MiB
            batch = db.batch()
            for n in range(start, min(start + 8, len(chunks))):
                batch.set(db.collection("upload_chunks").document(f"{_key(rel)}~{n}"), {"data": chunks[n]})
            batch.commit()
        batch = db.batch()  # the index entry last, so a half-written file is never listed
        batch.set(db.collection("uploads").document(_key(rel)), {
            "uid": uid, "path": rel, "size": len(data), "chunks": len(chunks),
            "created_at": firestore.SERVER_TIMESTAMP})
        batch.commit()


_restored: set[str] = set()


def restore(uid: str, upload_dir: Path) -> bool:
    """Bring an upload's files back from Firestore into the local folder (after the disk was wiped)."""
    db = firestore_client()
    if db is None or uid in _restored:
        return False
    _restored.add(uid)  # one attempt per process, even if it is not there
    found = False
    from google.cloud.firestore_v1.base_query import FieldFilter
    for snap in db.collection("uploads").where(filter=FieldFilter("uid", "==", uid)).stream():
        meta = snap.to_dict()
        target = upload_dir / meta["path"]
        if target.exists():
            continue
        parts = [db.collection("upload_chunks").document(f"{snap.id}~{n}").get() for n in range(meta["chunks"])]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"".join(p.to_dict()["data"] for p in parts if p.exists))
        found = True
    return found
