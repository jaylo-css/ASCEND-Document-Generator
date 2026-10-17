"""ASCEnD Document Generator API + web app.

Run:  uvicorn app.main:app --reload     (from the project root)
"""
from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import uuid
import zipfile
from urllib.parse import quote
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine, events, files, storage
from .ai.base import ImageInput, ProviderError, get_provider
from .registry import ROOT, get_template, list_templates, load_config, merge_updates, missing_fields, resolved_values

UPLOAD_DIR = Path(os.environ.get("ASCEND_UPLOAD_DIR", ROOT / "data" / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = 25 * 1024 * 1024
ALLOWED = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"}
MAX_SIDE = 2000  # px: longest side of stored photos (plenty for a printed report)


def _shrink(data: bytes, mime: str) -> tuple[bytes, str]:
    """Scale large photos down and turn them upright (phones store the rotation in EXIF, which Word
    ignores). Small images and GIFs are kept as they are."""
    if mime == "image/gif" or len(data) < 1_000_000:
        return data, mime
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        if mime == "image/png" and im.mode in ("RGBA", "LA", "P"):
            im.save(buf, "PNG", optimize=True)
        else:
            im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
            mime = "image/jpeg"
        return (buf.getvalue(), mime) if buf.tell() < len(data) else (data, mime)
    except Exception:  # unreadable image: store it unchanged
        return data, mime

app = FastAPI(title="ASCEnD Document Generator")

# Optional shared password (HTTP Basic auth; any username). Set ASCEND_PASSWORD when the
# app is reachable from the internet. /api/health stays open for the host's health check.
PASSWORD = os.environ.get("ASCEND_PASSWORD", "")


@app.middleware("http")
async def require_password(request: Request, call_next):
    if PASSWORD and request.url.path != "/api/health":
        given = ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2]
            except ValueError:
                pass
        if not secrets.compare_digest(given.encode(), PASSWORD.encode()):
            return Response("Password required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="ASCEnD DocGen", charset="UTF-8"'})
    return await call_next(request)


class DocState(BaseModel):
    values: dict = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)
    signatories: dict = Field(default_factory=dict)
    sig_titles: dict = Field(default_factory=dict)   # role -> position shown under the line
    sig_removed: list[str] = Field(default_factory=list)  # removable roles left off this report


class DocRequest(BaseModel):
    template_id: str
    state: DocState = Field(default_factory=DocState)


class ChatRequest(DocRequest):
    message: str = ""
    image_ids: list[str] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)
    event_id: str = ""        # use (and add to) this event's saved information
    generate: bool = False    # "fill this form from the event": also show the AI the event's images


class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    notes: str = Field("", max_length=20000)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class FilesIn(BaseModel):
    ids: list[str] = Field(max_length=50)
    names: dict[str, str] = Field(default_factory=dict)  # upload id -> original file name (images)


class DocumentIn(BaseModel):
    state: DocState = Field(default_factory=DocState)
    chat: list[dict] = Field(default_factory=list, max_length=200)


def _template(tid: str):
    try:
        return get_template(tid)
    except KeyError:
        raise HTTPException(404, f"Unknown template {tid}")


def _soffice() -> str | None:
    return files.soffice()


@app.get("/api/health")
def health():
    try:
        provider = get_provider().name
    except Exception as e:  # misconfigured provider
        provider = f"error: {e}"
    return {"ok": True, "ai_provider": provider, "pdf_export": bool(_soffice()), "protected": bool(PASSWORD),
            "storage": storage.backend_name()}


@app.get("/api/templates")
def templates():
    return [{"id": t.id, "name": t.name, "office": t.schema.get("office"),
             "description": t.schema.get("description")} for t in list_templates()]


@app.get("/api/templates/{tid}")
def template_detail(tid: str):
    cfg = load_config()
    return {**_template(tid).public(), "config_signatories": cfg.get("signatories", {})}


@app.get("/api/templates/{tid}/media/{name}")
def template_media(tid: str, name: str):
    if not re.fullmatch(r"[\w.-]+", name):
        raise HTTPException(400)
    with zipfile.ZipFile(_template(tid).path) as z:
        try:
            data = z.read(f"word/media/{name}")
        except KeyError:
            raise HTTPException(404)
    return Response(data, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/preview")
def preview(req: DocRequest):
    tpl, cfg = _template(req.template_id), load_config()
    state = req.state.model_dump()
    return {"html": engine.render_html(tpl, state, cfg, UPLOAD_DIR),
            "missing": missing_fields(tpl, state, cfg),
            "values": resolved_values(tpl, state, cfg)}


@app.post("/api/chat")
def chat(req: ChatRequest, x_gemini_key: str = Header("", max_length=200)):
    """X-Gemini-Key: the user's own Gemini key from the browser; used for this request only, never stored."""
    tpl, cfg = _template(req.template_id), load_config()
    state = req.state.model_dump()
    ctx = engine.Context(tpl, state, cfg, UPLOAD_DIR)
    message = req.message

    def add_image(iid, source=""):
        path = ctx.image_path(iid)
        if path:
            images.append(ImageInput(id=iid, mime=mimetypes.guess_type(path.name)[0] or "image/png",
                                     data=path.read_bytes(), name=path.name, source=source))

    images, context, event = [], "", None
    if req.event_id:
        event = _event(req.event_id)
        context = events.context_text(event, UPLOAD_DIR, tpl.id, cfg)
        if req.generate:
            for iid, name, source in events.context_images(event):
                add_image(iid, source)
    for iid in req.image_ids:
        add_image(iid)
    for fid in req.file_ids:
        doc = files.load_document(fid, UPLOAD_DIR) if re.fullmatch(r"\w+", fid) else None
        if not doc:
            continue
        record, text = doc
        message += files.as_message(record, text)
        for n, iid in enumerate(record.get("pages", []), 1):
            add_image(iid, source=f"page {n} of {record['name']}")
    try:
        provider = get_provider(api_key=x_gemini_key.strip() or None)
        if provider.name == "offline" and req.generate and context:
            message = context + "\n\n" + message  # the rule-based parser reads labelled notes
        result = provider.extract(tpl=tpl, values=resolved_values(tpl, state, cfg),
                                  skipped=state["skipped"], missing=missing_fields(tpl, state, cfg),
                                  message=message, images=images, history=req.history, context=context)
    except (ProviderError, ValueError) as e:
        raise HTTPException(502, str(e))
    new_state, changed = merge_updates(tpl, state, result.updates)
    # facts learned in this chat go back into the event (not when filling *from* the event: nothing new there)
    remembered = [] if not event or req.generate else _new_facts(event, result.remember)
    if remembered:
        events.update_event(event["id"], lambda e: e["notes"].extend(
            events.new_note(r, source="assistant") for r in remembered))
    return {"state": new_state, "reply": result.reply, "changed": changed, "remembered": remembered,
            "missing": missing_fields(tpl, new_state, cfg), "provider": provider.name}


# ---------------------------------------------------------------- events

def _new_facts(event: dict, facts: list) -> list[str]:
    """Facts the assistant wants to remember, minus ones already in the event's notes."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    saved = " | ".join(norm(n["text"]) for n in event.get("notes", []))
    out = []
    for f in facts[:10]:
        f = str(f).strip()[:1000]
        if f and norm(f) not in saved and f not in out:
            out.append(f)
    return out


def _event(eid: str) -> dict:
    event = events.get_event(eid) if re.fullmatch(r"\w+", eid or "") else None
    if not event:
        raise HTTPException(404, "Event not found")
    return event


def _changed(eid: str, change) -> dict:
    event = events.update_event(_event(eid)["id"], change)
    if not event:
        raise HTTPException(404, "Event not found")
    return event


@app.get("/api/events")
def list_events():
    return events.list_events()


@app.post("/api/events")
def create_event(body: EventIn):
    return events.create_event(body.name.strip(), body.notes)


@app.get("/api/events/{eid}")
def get_event(eid: str):
    return _event(eid)


@app.patch("/api/events/{eid}")
def rename_event(eid: str, body: EventIn):
    return _changed(eid, lambda e: e.update(name=body.name.strip()))


@app.delete("/api/events/{eid}")
def delete_event(eid: str):
    _event(eid)
    events.delete_event(eid)
    return {"ok": True}


@app.post("/api/events/{eid}/notes")
def add_note(eid: str, body: NoteIn):
    return _changed(eid, lambda e: e["notes"].append(events.new_note(body.text)))


@app.delete("/api/events/{eid}/notes/{nid}")
def delete_note(eid: str, nid: str):
    return _changed(eid, lambda e: e.update(notes=[n for n in e["notes"] if n["id"] != nid]))


@app.post("/api/events/{eid}/files")
def add_files(eid: str, body: FilesIn):
    """Attach already-uploaded files (ids from /api/uploads) to the event's information."""
    found = []
    for uid in body.ids:
        if not re.fullmatch(r"\w+", uid):
            continue
        doc = files.load_document(uid, UPLOAD_DIR)
        if doc:
            rec = doc[0]
            found.append({"id": uid, "name": rec["name"], "kind": "document", "pages": rec.get("pages", [])})
        elif list(UPLOAD_DIR.glob(uid + ".*")):
            found.append({"id": uid, "name": (body.names.get(uid) or uid)[:200], "kind": "image"})

    def change(e):
        known = {f["id"] for f in e["files"]}
        e["files"].extend(f for f in found if f["id"] not in known)
    return _changed(eid, change)


@app.delete("/api/events/{eid}/files/{fid}")
def delete_file(eid: str, fid: str):
    return _changed(eid, lambda e: e.update(files=[f for f in e["files"] if f["id"] != fid]))


@app.put("/api/events/{eid}/documents/{tid}")
def save_document(eid: str, tid: str, body: DocumentIn):
    """Save one form's draft (values, signatories, chat) inside the event."""
    _template(tid)
    doc = {"state": body.state.model_dump(), "chat": body.chat[-60:], "updated_at": events._now()}
    if not events.save_document(_event(eid)["id"], tid, doc):
        raise HTTPException(404, "Event not found")
    return {"ok": True}


@app.post("/api/uploads")
async def upload(uploads: list[UploadFile] = File(..., alias="files")):
    out = []
    for f in uploads:
        name = f.filename or "file"
        mime = f.content_type or mimetypes.guess_type(name)[0] or ""
        is_doc = Path(name).suffix.lower() in files.DOC_EXT
        if mime not in ALLOWED and not is_doc:
            raise HTTPException(415, f"{name}: unsupported file type. Use images, PDF, Word, PowerPoint, Excel or text files")
        data = await f.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{name}: larger than 25 MB")
        if is_doc:
            try:
                rec = files.save_document(data, name, UPLOAD_DIR)
            except files.FileError as e:
                raise HTTPException(422, f"{name}: {e}")
            storage.backup(rec["id"], sorted(files.docs_dir(UPLOAD_DIR).glob(rec["id"] + ".*")), UPLOAD_DIR)
            for p in rec["pages"]:
                storage.backup(p, list(UPLOAD_DIR.glob(p + ".*")), UPLOAD_DIR)
            out.append({**rec, "kind": "document",
                        "pages": [{"id": p, "url": f"/api/uploads/{p}"} for p in rec["pages"]]})
            continue
        data, mime = _shrink(data, mime)
        uid = uuid.uuid4().hex[:16]
        ext = {"image/jpeg": ".jpg"}.get(mime) or mimetypes.guess_extension(mime) or ".png"
        path = UPLOAD_DIR / f"{uid}{ext}"
        path.write_bytes(data)
        storage.backup(uid, [path], UPLOAD_DIR)
        out.append({"id": uid, "name": name, "kind": "image", "url": f"/api/uploads/{uid}"})
    return out


@app.get("/api/uploads/{uid}")
def get_upload(uid: str):
    if not re.fullmatch(r"[\w-]+", uid):
        raise HTTPException(400)
    hits = list(UPLOAD_DIR.glob(uid + ".*"))
    if not hits and storage.restore(uid, UPLOAD_DIR):
        hits = list(UPLOAD_DIR.glob(uid + ".*"))
    if not hits:
        raise HTTPException(404)
    return FileResponse(hits[0])


def _filename(tpl, state, cfg, ext):
    values = resolved_values(tpl, state, cfg)
    pattern = tpl.schema.get("output_name", tpl.name)
    name = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1)) or "Untitled"), pattern)
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip()[:120] + ext


def _disposition(name):
    ascii_name = name.encode("ascii", "ignore").decode() or "document"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"}


@app.post("/api/export/docx")
def export_docx(req: DocRequest):
    tpl, cfg = _template(req.template_id), load_config()
    state = req.state.model_dump()
    data = engine.fill_docx(tpl, state, cfg, UPLOAD_DIR)
    name = _filename(tpl, state, cfg, ".docx")
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers=_disposition(name))


@app.post("/api/export/pdf")
def export_pdf(req: DocRequest):
    soffice = _soffice()
    if not soffice:
        raise HTTPException(501, "PDF export needs LibreOffice installed on the server")
    tpl, cfg = _template(req.template_id), load_config()
    state = req.state.model_dump()
    data = engine.fill_docx(tpl, state, cfg, UPLOAD_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.docx"
        src.write_bytes(data)
        try:
            subprocess.run([soffice, "--headless", f"-env:UserInstallation=file://{tmp}/lo",
                            "--convert-to", "pdf", "--outdir", tmp, str(src)],
                           check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as e:
            raise HTTPException(500, f"PDF conversion failed: {e}")
        pdf = (Path(tmp) / "doc.pdf").read_bytes()
    name = _filename(tpl, state, cfg, ".pdf")
    return Response(pdf, media_type="application/pdf",
                    headers=_disposition(name))


WEB = ROOT / "web"


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=WEB), name="static")
