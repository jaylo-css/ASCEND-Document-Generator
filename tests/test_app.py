"""Run with:  python -m pytest -q   (from the project root)"""
import io
import json

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

from app import main
from app.ai.base import ImageInput
from app.ai.gemini import GeminiProvider
from app.registry import get_template, load_config, merge_updates, missing_fields

NOTES = """Event: Arduino Basics Workshop
Date: October 10, 2026
Venue: Room 401
Forty-five first-year students learned breadboarding.

Program flow:
1:00 PM - Registration (Secretariat)
1:30 PM - Opening Remarks by Mr. Jhamil G. Gutierrez

Attendance:
Juan Dela Cruz, BSCpE 1
Maria Reyes, BSCpE 1
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("AI_PROVIDER", "offline")
    return TestClient(main.app)


def doc_text(data: bytes) -> str:
    d = Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        parts += [c.text for r in t.rows for c in r.cells]
    return "\n".join(parts)


def test_offline_chat_fills_fields(client):
    r = client.post("/api/chat", json={"template_id": "post_activity_report", "message": NOTES})
    assert r.status_code == 200, r.text
    v = r.json()["state"]["values"]
    assert v["event_name"] == "Arduino Basics Workshop"
    assert v["venue"] == "Room 401"
    assert v["program_flow"][1] == ["1:30 PM", "Opening Remarks", "Mr. Jhamil G. Gutierrez"]
    assert v["attendance"] == [["Juan Dela Cruz", "BSCpE 1"], ["Maria Reyes", "BSCpE 1"]]
    assert "breadboarding" in v["summary"]
    missing = [m["id"] for m in r.json()["missing"]]
    assert "event_name" not in missing and "evaluation" in missing


def test_docx_export_fills_tokens_and_signatories(client):
    state = {"values": {"event_name": "Arduino Basics Workshop", "summary": "Para one.\nPara two.",
                        "attendance": [["Juan Dela Cruz", "BSCpE 1"]]},
             "skipped": ["liquidation_form"], "signatories": {"president": "Juan P. Santos"}}
    r = client.post("/api/export/docx", json={"template_id": "post_activity_report", "state": state})
    assert r.status_code == 200
    text = doc_text(r.content)
    assert "{{" not in text
    assert "Academic Society of Computer Engineering Discipline (ASCEnD)" in text  # default
    assert "Juan P. Santos" in text and "Dr. Corazon B. Rebong, PCpE" in text
    assert "Engr. Jhamil G. Gutierrez, MSCpE" in text and "Engr. Mary Jane F. Somao-i, MSCpE" in text
    assert "List of Attendance – see Annex A" in text
    assert "Liquidation Form – N/A" in text
    assert "Para two." in text and "Juan Dela Cruz" in text


def test_preview_and_skip(client):
    state = {"values": {}, "skipped": ["invitation"]}
    r = client.post("/api/preview", json={"template_id": "post_activity_report", "state": state})
    body = r.json()
    assert 'data-field="event_name"' in body["html"]
    assert 'data-sig="dean"' in body["html"]
    assert "invitation" not in [m["id"] for m in body["missing"]]


def test_image_upload_goes_into_annex(client):
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    up = client.post("/api/uploads", files=[("files", ("p.png", png, "image/png"))]).json()
    state = {"values": {"pictures": [{"id": up[0]["id"], "caption": "Group photo"}]}}
    r = client.post("/api/export/docx", json={"template_id": "post_activity_report", "state": state})
    d = Document(io.BytesIO(r.content))
    assert len(d.inline_shapes) == 1
    assert "Figure 1. Group photo" in doc_text(r.content)


def _docx_bytes(text: str) -> bytes:
    d = Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_document_upload_feeds_chat(client):
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    up = client.post("/api/uploads", files=[("files", ("notes.docx", _docx_bytes(NOTES), docx_mime))])
    assert up.status_code == 200, up.text
    rec = up.json()[0]
    assert rec["kind"] == "document" and rec["chars"] > 0
    r = client.post("/api/chat", json={"template_id": "post_activity_report", "file_ids": [rec["id"]]})
    v = r.json()["state"]["values"]
    assert v["event_name"] == "Arduino Basics Workshop" and v["venue"] == "Room 401"
    assert ["Juan Dela Cruz", "BSCpE 1"] in v["attendance"]


def test_pdf_upload_renders_pages_but_not_into_pictures(client):
    pdf = open("examples/Post-Activity Report - Arduino Basics Workshop 2026.pdf", "rb").read()
    rec = client.post("/api/uploads", files=[("files", ("report.pdf", pdf, "application/pdf"))]).json()[0]
    assert rec["pages"] and rec["chars"] > 0
    assert client.get(rec["pages"][0]["url"]).headers["content-type"] == "image/png"
    r = client.post("/api/chat", json={"template_id": "post_activity_report", "file_ids": [rec["id"]]})
    assert "pictures" not in r.json()["state"]["values"]


def test_unsupported_upload_is_rejected(client):
    r = client.post("/api/uploads", files=[("files", ("tool.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 415


def test_merge_updates_validates_types():
    tpl, cfg = get_template("post_activity_report"), load_config()
    state, changed = merge_updates(tpl, {"skipped": ["program_flow"]}, {
        "program_flow": [{"Time": "8:00", "Activity": "Opening"}], "not_a_field": "x", "event_name": 42})
    assert state["values"]["program_flow"] == [["8:00", "Opening", ""]]
    assert state["values"]["event_name"] == "42"
    assert "program_flow" not in state["skipped"]
    assert "not_a_field" not in state["values"]


def test_gemini_provider_parses_json(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        seen["body"] = json
        reply = {"updates": {"event_name": "Hackathon"}, "reply": "Got it."}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(reply)}]}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    tpl, cfg = get_template("post_activity_report"), load_config()
    p = GeminiProvider(api_key="test")
    res = p.extract(tpl=tpl, values={}, skipped=[], missing=missing_fields(tpl, {}, cfg), message="notes",
                    images=[ImageInput(id="abc", mime="image/png", data=b"x")], history=[])
    assert res.updates == {"event_name": "Hackathon"} and res.reply == "Got it."
    parts = seen["body"]["contents"][-1]["parts"]
    assert any("inline_data" in part for part in parts)
    assert "program_flow" in seen["body"]["systemInstruction"]["parts"][0]["text"]


def test_password_protects_everything_but_health(client, monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "s3cret")
    assert client.get("/api/templates").status_code == 401
    assert client.get("/api/templates", auth=("any", "wrong")).status_code == 401
    assert client.get("/api/templates", auth=("any", "s3cret")).status_code == 200
    assert client.get("/api/health").status_code == 200
