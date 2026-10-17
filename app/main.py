"""ASCEnD Document Generator API + web app.

Run:  uvicorn app.main:app --reload     (from the project root)
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import uuid
from urllib.parse import quote
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine, files
from .ai.base import ImageInput, ProviderError, get_provider
from .registry import ROOT, get_template, list_templates, load_config, merge_updates, missing_fields, resolved_values

UPLOAD_DIR = Path(os.environ.get("ASCEND_UPLOAD_DIR", ROOT / "data" / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = 25 * 1024 * 1024
ALLOWED = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"}

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
    return {"ok": True, "ai_provider": provider, "pdf_export": bool(_soffice())}


@app.get("/api/templates")
def templates():
    return [{"id": t.id, "name": t.name, "office": t.schema.get("office"),
             "description": t.schema.get("description")} for t in list_templates()]


@app.get("/api/templates/{tid}")
def template_detail(tid: str):
    cfg = load_config()
    return {**_template(tid).public(), "config_signatories": cfg.get("signatories", {})}


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

    images = []
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
        result = provider.extract(tpl=tpl, values=resolved_values(tpl, state, cfg),
                                  skipped=state["skipped"], missing=missing_fields(tpl, state, cfg),
                                  message=message, images=images, history=req.history)
    except (ProviderError, ValueError) as e:
        raise HTTPException(502, str(e))
    new_state, changed = merge_updates(tpl, state, result.updates)
    return {"state": new_state, "reply": result.reply, "changed": changed,
            "missing": missing_fields(tpl, new_state, cfg), "provider": provider.name}


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
            out.append({**rec, "kind": "document",
                        "pages": [{"id": p, "url": f"/api/uploads/{p}"} for p in rec["pages"]]})
            continue
        uid = uuid.uuid4().hex[:16]
        ext = mimetypes.guess_extension(mime) or ".png"
        (UPLOAD_DIR / f"{uid}{ext}").write_bytes(data)
        out.append({"id": uid, "name": name, "kind": "image", "url": f"/api/uploads/{uid}"})
    return out


@app.get("/api/uploads/{uid}")
def get_upload(uid: str):
    if not re.fullmatch(r"[\w-]+", uid):
        raise HTTPException(400)
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
