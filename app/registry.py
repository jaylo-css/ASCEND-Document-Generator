"""Template registry and document state helpers.

Every SDAO form lives in templates/<id>/ with two files:

    schema.json    fields, their types, and the signatory roles the form uses
    template.docx  the original form with {{tokens}} where values go (see engine.py)

Adding a new form means adding a folder; nothing in the code changes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "templates"
CONFIG_PATH = Path(os.environ.get("ASCEND_CONFIG", ROOT / "config.json"))

FIELD_TYPES = {"text", "longtext", "table", "images"}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def is_placeholder(value: str) -> bool:
    """Config values like "[PRESIDENT NAME]" are placeholders still to be set."""
    return bool(value) and value.strip().startswith("[") and value.strip().endswith("]")


@dataclass
class Template:
    id: str
    dir: Path
    schema: dict
    fields: list[dict] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.dir / self.schema.get("file", "template.docx")

    @property
    def name(self) -> str:
        return self.schema.get("name", self.id)

    def field(self, field_id: str) -> dict | None:
        return next((f for f in self.fields if f["id"] == field_id), None)

    def public(self) -> dict:
        return {k: self.schema.get(k) for k in ("id", "name", "office", "description", "fields", "signatories",
                                                "removable_signatories", "greeting")}


def _load(folder: Path) -> Template:
    schema = json.loads((folder / "schema.json").read_text(encoding="utf-8"))
    schema.setdefault("id", folder.name)
    for f in schema.get("fields", []):
        if f.get("type") not in FIELD_TYPES:
            raise ValueError(f"{folder.name}: field {f.get('id')} has unknown type {f.get('type')}")
    return Template(id=schema["id"], dir=folder, schema=schema, fields=schema.get("fields", []))


def list_templates() -> list[Template]:
    return [_load(d) for d in sorted(TEMPLATES_DIR.iterdir()) if (d / "schema.json").exists()]


def get_template(template_id: str) -> Template:
    for t in list_templates():
        if t.id == template_id:
            return t
    raise KeyError(template_id)


# ---------------------------------------------------------------- state
#
# A document's state is plain JSON kept by the browser:
#   {"values": {field_id: value}, "skipped": [field_id], "signatories": {role: name},
#    "sig_titles": {role: position}, "sig_removed": [role]}
# value shapes: text/longtext -> str, table -> [[cell, ...], ...],
#               images -> [{"id": upload_id, "caption": str}]


def _resolve_default(default: str, config: dict, values: dict | None = None) -> str:
    """"{organization.name}" reads config.json; "{event_name}" reads another field of the form."""
    def lookup(m):
        if values and isinstance(values.get(m.group(1)), str):
            return values[m.group(1)]
        cur = config
        for part in m.group(1).split("."):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        return cur if isinstance(cur, str) else ""
    return re.sub(r"\{([\w.]+)\}", lookup, default)


def normalize_value(f: dict, value):
    t = f["type"]
    if value is None:
        return None
    if t in ("text", "longtext"):
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value)
        value = str(value).strip()
        return value or None
    if t == "table":
        cols = len(f.get("columns", []))
        rows = []
        for row in value if isinstance(value, list) else []:
            if isinstance(row, dict):
                row = [row.get(c, "") for c in f.get("columns", [])]
            if not isinstance(row, list):
                row = [row]
            row = [("" if c is None else str(c)).strip() for c in row][:cols]
            row += [""] * (cols - len(row))
            if any(row):
                rows.append(row)
        return rows or None
    if t == "images":
        out = []
        for item in value if isinstance(value, list) else []:
            if isinstance(item, str):
                item = {"id": item}
            if isinstance(item, dict) and item.get("id"):
                out.append({"id": str(item["id"]), "caption": str(item.get("caption") or "").strip()})
        return out or None
    return value


def resolved_values(tpl: Template, state: dict, config: dict) -> dict:
    """State values normalized per field type, with schema defaults applied."""
    raw = (state or {}).get("values") or {}
    out = {}
    for f in tpl.fields:
        v = normalize_value(f, raw.get(f["id"]))
        if v is None and f.get("default") and f["id"] not in raw:
            v = normalize_value(f, _resolve_default(f["default"], config, out))
        out[f["id"]] = v
    return out


def missing_fields(tpl: Template, state: dict, config: dict) -> list[dict]:
    """Fields the assistant should still ask about: empty and not skipped.
    Required fields come first, then attachments."""
    values = resolved_values(tpl, state, config)
    skipped = set((state or {}).get("skipped") or [])
    todo = [f for f in tpl.fields if values.get(f["id"]) is None and f["id"] not in skipped]
    todo.sort(key=lambda f: 0 if f.get("required") else 1)
    return [{"id": f["id"], "label": f["label"], "required": bool(f.get("required")), "type": f["type"]} for f in todo]


def merge_updates(tpl: Template, state: dict, updates: dict) -> tuple[dict, list[str]]:
    """Apply AI-extracted updates to state. Returns (new_state, changed_field_ids)."""
    state = json.loads(json.dumps(state or {}))
    values = state.setdefault("values", {})
    skipped = state.setdefault("skipped", [])
    changed = []
    for fid, raw in (updates or {}).items():
        f = tpl.field(fid)
        if not f:
            continue
        v = normalize_value(f, raw)
        if v is None:
            continue
        if f["type"] == "images":
            existing = values.get(fid) or []
            known = {i["id"] for i in existing}
            for item in v:
                if item["id"] in known:
                    for e in existing:
                        if e["id"] == item["id"] and item["caption"]:
                            e["caption"] = item["caption"]
                else:
                    existing.append(item)
            v = existing
        if values.get(fid) != v:
            values[fid] = v
            changed.append(fid)
        if fid in skipped:
            skipped.remove(fid)
    return state, changed
