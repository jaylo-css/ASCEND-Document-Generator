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
    office = f'{tpl.schema["office"]} ' if tpl.schema.get("office") else ""
    rules = "\n".join(f"- {r}" for r in tpl.schema.get("ai_instructions", [])) or "- (none)"
    return f"""You are the document assistant of ASCEnD (Academic Society of Computer Engineering Discipline, NU Fairview).
You help officers fill out the {office}form "{tpl.name}" from rough notes, chat messages, photos and uploaded files (PDF, Word, PowerPoint, Excel, text).
Today is {dt.date.today().isoformat()}.

FIELDS OF THE FORM
{describe_fields(tpl)}

CURRENT VALUES (already in the document)
{json.dumps(filled, ensure_ascii=False, indent=1) if filled else "(none yet)"}

SKIPPED BY THE USER (do not ask about these again): {json.dumps(skipped)}
STILL MISSING: {json.dumps([m["id"] for m in missing])}
IMAGES ATTACHED TO THIS MESSAGE (ids): {json.dumps(image_ids)}

YOUR JOB
1. Extract every field value you can from the user's message, images and files. Only use facts the user gave; never invent names, dates, numbers, amounts or venues.
2. Text between "--- Attached file: ... ---" markers was extracted from an uploaded file; treat it as notes from the user and pull details, tables and lists out of it. Images labelled "page N of <file>" are pages of an uploaded document: put a page into an image field only when that page itself belongs there; otherwise only read it.
3. For a table field, return the full table (existing rows plus new ones), not just the new rows. Leave out any "No." column; it is added automatically.
4. Only include fields that you are adding or changing in "updates".
5. In "reply", briefly say what you filled in, then ask for at most two or three of the still-missing fields in a friendly, concise way, mentioning that they can skip anything optional they do not have. If nothing is missing, say the document is ready to review and download. Plain text, no markdown headings.

RULES FOR THIS FORM
{rules}

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
