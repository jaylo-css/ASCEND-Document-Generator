"""Prompt shared by LLM providers: describes the template's fields and the JSON to return."""
from __future__ import annotations

import datetime as dt
import json
import re

from ..registry import Template


def describe_fields(tpl: Template) -> str:
    lines = []
    for f in tpl.fields:
        if f["type"] == "table":
            shape = f"array of rows, each row an array of strings in this column order: {json.dumps(f['columns'])}"
        elif f["type"] == "images":
            shape = 'array of {"id": <uploaded image id>, "caption": string}'
        elif f["type"] == "longtext":
            shape = "string; separate paragraphs with \\n"
        else:
            shape = "string"
        req = "required" if f.get("required") else ("attachment, optional" if f.get("attachment") else "optional")
        hint = f" Guidance: {f['hint']}" if f.get("hint") else ""
        lines.append(f'- "{f["id"]}" ({f["label"]}; {req}): {shape}.{hint}')
    return "\n".join(lines)


def system_prompt(tpl: Template, values: dict, skipped: list[str], missing: list[dict], image_ids: list[str]) -> str:
    filled = {k: v for k, v in values.items() if v}
    return f"""You are the document assistant of ASCEnD (Academic Society of Computer Engineering Discipline, NU Fairview).
You help officers fill out the SDAO form "{tpl.name}" from rough notes, chat messages, photos and uploaded files (PDF, Word, PowerPoint, Excel, text).
Today is {dt.date.today().isoformat()}.

FIELDS OF THE FORM
{describe_fields(tpl)}

CURRENT VALUES (already in the document)
{json.dumps(filled, ensure_ascii=False, indent=1) if filled else "(none yet)"}

SKIPPED BY THE USER (do not ask about these again): {json.dumps(skipped)}
STILL MISSING: {json.dumps([m["id"] for m in missing])}
IMAGES ATTACHED TO THIS MESSAGE (ids): {json.dumps(image_ids)}

YOUR JOB
1. Extract every field value you can from the user's message and images. Only use facts the user gave; never invent names, dates, numbers or venues.
2. Write "summary" as formal past-tense prose for a university office, covering what, where, when and who, using the facts available. Rewrite it whenever new facts arrive.
3. For each attached image decide what it is: an event photo goes to "pictures" with a one-sentence caption describing what is happening; a scanned invitation, pubmat/poster, or liquidation form/receipt goes to that field; a photo of an attendance sheet, program, or committee list should be transcribed into the matching table (and not added to pictures). Images labelled "page N of <file>" are pages of an uploaded document: use a page as an attachment image (for example invitation or liquidation form) when it is that document, otherwise only read it; never put document pages in "pictures".
   Text between "--- Attached file: ... ---" markers was extracted from an uploaded file; treat it as notes from the user and pull event details, tables and lists out of it.
4. For a table field, return the full table (existing rows plus new ones), not just the new rows. Leave out the "No." column; it is added automatically.
5. Only include fields that you are adding or changing in "updates".
6. In "reply", briefly say what you filled in, then ask for at most two or three of the still-missing fields in a friendly, concise way, mentioning that they can skip any attachment they do not have. If nothing is missing, say the report is ready to review and download. Plain text, no markdown headings.

Respond with JSON only, exactly this shape:
{{"updates": {{"<field_id>": <value>}}, "reply": "<message to the user>"}}"""


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("model did not return JSON")
    return json.loads(text[start:end + 1])
