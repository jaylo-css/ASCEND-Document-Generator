"""Read uploaded documents (PDF, Word, PowerPoint, Excel, text) so the AI can use them.

Each document is stored under <upload_dir>/docs/<id>.<ext> with its extracted text
next to it (<id>.txt) and a small JSON record (<id>.json). PDF pages are also
rendered to PNG and stored as ordinary image uploads, so a scanned invitation or
liquidation form can be placed in the report as an annex like any photo.

Legacy formats (.doc, .ppt, .xls, .odt, ...) are converted with LibreOffice when
it is installed; without it they are rejected with a clear message.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

MAX_PDF_PAGES = 10     # pages rendered to images per PDF
MAX_TEXT = 30_000      # characters of a document's text sent to the AI

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".rtf"}
NATIVE_EXT = {".pdf", ".docx", ".pptx", ".xlsx"} | TEXT_EXT
LEGACY_EXT = {".doc": ".docx", ".odt": ".docx", ".ppt": ".pptx", ".odp": ".pptx", ".xls": ".xlsx", ".ods": ".xlsx"}
DOC_EXT = NATIVE_EXT | set(LEGACY_EXT)


class FileError(ValueError):
    pass


def soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def docs_dir(upload_dir: Path) -> Path:
    d = upload_dir / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _convert(data: bytes, ext: str) -> tuple[bytes, str]:
    target = LEGACY_EXT[ext]
    exe = soffice()
    if not exe:
        raise FileError(f"{ext} files need LibreOffice on the server; save it as {target} or PDF and upload that")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{ext}"
        src.write_bytes(data)
        try:
            subprocess.run([exe, "--headless", f"-env:UserInstallation=file://{tmp}/lo",
                            "--convert-to", target.lstrip("."), "--outdir", tmp, str(src)],
                           check=True, capture_output=True, timeout=120)
            return (Path(tmp) / f"in{target}").read_bytes(), target
        except (subprocess.SubprocessError, OSError) as e:
            raise FileError(f"could not convert {ext} file: {e}") from e


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(f"[Page {i}]\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1))


def _pdf_pages(data: bytes, upload_dir: Path) -> list[str]:
    import pypdfium2 as pdfium
    ids = []
    pdf = pdfium.PdfDocument(data)
    try:
        for i in range(min(len(pdf), MAX_PDF_PAGES)):
            img = pdf[i].render(scale=150 / 72).to_pil().convert("RGB")
            uid = uuid.uuid4().hex[:16]
            img.save(upload_dir / f"{uid}.jpg", quality=80, optimize=True)  # a fraction of a PNG's size
            ids.append(uid)
    finally:
        pdf.close()
    return ids


def _docx_text(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    out = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        out.append("[Table]")
        for row in t.rows:
            out.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(out)


def _pptx_text(data: bytes) -> str:
    from pptx import Presentation
    out = []
    for n, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        out.append(f"[Slide {n}]")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    out.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append("Notes: " + slide.notes_slide.notes_text_frame.text.strip())
    return "\n".join(out)


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"[Sheet {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                out.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(out)


def _plain_text(data: bytes, ext: str) -> str:
    text = data.decode("utf-8-sig", errors="replace")
    if ext in {".csv", ".tsv"}:
        rows = csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ",")
        text = "\n".join(" | ".join(r) for r in rows)
    return text


def save_document(data: bytes, filename: str, upload_dir: Path) -> dict:
    """Store an uploaded document, extract its text and (for PDFs) page images."""
    ext = Path(filename).suffix.lower()
    if ext not in DOC_EXT:
        raise FileError("unsupported file type")
    uid = uuid.uuid4().hex[:16]
    d = docs_dir(upload_dir)
    original = (d / f"{uid}{ext}", data)
    if ext in LEGACY_EXT:
        data, ext = _convert(data, ext)
    original[0].write_bytes(original[1])
    try:
        if ext == ".pdf":
            text, pages = _pdf_text(data), _pdf_pages(data, upload_dir)
        else:
            reader = {".docx": _docx_text, ".pptx": _pptx_text, ".xlsx": _xlsx_text}.get(ext)
            text, pages = (reader(data) if reader else _plain_text(data, ext)), []
    except FileError:
        raise
    except Exception as e:  # corrupt or password-protected file
        raise FileError(f"could not read this file ({e.__class__.__name__})") from e
    (d / f"{uid}.txt").write_text(text, encoding="utf-8")
    record = {"id": uid, "name": filename, "pages": pages, "chars": len(text.strip())}
    (d / f"{uid}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def load_document(uid: str, upload_dir: Path) -> tuple[dict, str] | None:
    from . import storage
    d = docs_dir(upload_dir)
    meta, txt = d / f"{uid}.json", d / f"{uid}.txt"
    if not meta.exists() and not storage.restore(uid, upload_dir):
        return None
    if not meta.exists():
        return None
    return json.loads(meta.read_text(encoding="utf-8")), txt.read_text(encoding="utf-8") if txt.exists() else ""


def as_message(record: dict, text: str) -> str:
    """The block of text appended to the chat message for one document."""
    body = text.strip()
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT] + "\n[... rest of the file cut off ...]"
    if not body:
        body = "(no selectable text; see the page images)" if record.get("pages") else "(the file has no readable text)"
    return f'\n\n--- Attached file: {record["name"]} ---\n{body}\n--- End of {record["name"]} ---'
