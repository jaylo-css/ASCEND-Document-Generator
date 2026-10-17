"""Rule-based fallback used when no AI key is configured.

It understands notes written as "Label: value" lines and sections such as

    Program flow:
    8:00 AM - Registration
    9:00 AM - Talk by Engr. Cruz

so the app can be tried end to end without an API key. It cannot read images,
so attached images are added to "Pictures" for the user to caption. Text from
uploaded documents arrives inside the message, so labelled notes in a file work too.
"""
from __future__ import annotations

import re

from .base import AIProvider, ExtractionResult

SYNONYMS = {
    "organization_name": ["organization", "org", "organization name", "name of the organization"],
    "event_name": ["event", "event name", "title", "activity", "name of the event", "activity title"],
    "event_date": ["date", "when", "date and time", "time and date", "schedule"],
    "venue": ["venue", "where", "location", "place"],
    "summary": ["summary", "highlights", "summary/highlights", "description", "what happened", "notes"],
    "program_flow": ["program flow", "program", "flow", "agenda", "program of activities"],
    "committee_members": ["committee", "committee members", "committees", "organizers", "officers in charge"],
    "attendance": ["attendance", "attendees", "list of attendance", "participants", "attendance list"],
    "evaluation": ["evaluation", "evaluation results", "feedback", "eval"],
}
TIME_ROW = re.compile(r"^\s*(\d{1,2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?(?:\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?)?)\s*[-–—|:]\s*(.+)$")
SPLIT = re.compile(r"\s+[-–—|]\s+|\s*\|\s*|\t|,\s*")


def _key_for(label: str, tpl) -> str | None:
    label = label.strip().lower().rstrip(":").strip()
    for f in tpl.fields:
        names = SYNONYMS.get(f["id"], []) + [f["label"].lower(), f["id"].replace("_", " ")]
        if label in names:
            return f["id"]
    return None


class OfflineProvider(AIProvider):
    name = "offline"

    def extract(self, *, tpl, values, skipped, missing, message, images, history) -> ExtractionResult:
        updates: dict = {}
        tables: dict[str, list] = {}
        loose: list[str] = []
        section = None

        for raw in (message or "").splitlines():
            line = raw.strip()
            if not TIME_ROW.match(line):
                line = line.lstrip("•*-").strip()
            if not line:
                section = None
                continue
            head = re.match(r"^([A-Za-z][A-Za-z /&]{1,40}):\s*(.*)$", line)
            if head and _key_for(head.group(1), tpl):
                fid = _key_for(head.group(1), tpl)
                rest = head.group(2).strip()
                f = tpl.field(fid)
                if f["type"] == "table":
                    section = fid
                    if rest:
                        tables.setdefault(fid, []).append(self._row(f, rest))
                elif rest:
                    updates[fid] = (updates.get(fid, "") + "\n" + rest).strip() if f["type"] == "longtext" else rest
                    section = fid if f["type"] == "longtext" else None
                else:
                    section = fid
                continue
            if section:
                f = tpl.field(section)
                if f["type"] == "table":
                    tables.setdefault(section, []).append(self._row(f, line))
                elif f["type"] == "longtext":
                    updates[section] = (updates.get(section, "") + "\n" + line).strip()
                else:
                    updates[section] = line
                    section = None
                continue
            m = TIME_ROW.match(line)
            if m and tpl.field("program_flow"):
                tables.setdefault("program_flow", []).append(self._row(tpl.field("program_flow"), line))
                continue
            loose.append(line)

        for fid, rows in tables.items():
            updates[fid] = (values.get(fid) or []) + rows

        if loose and "summary" not in updates and tpl.field("summary"):
            facts = {**values, **updates}
            lead = []
            if facts.get("event_name"):
                when = f" on {facts['event_date']}" if facts.get("event_date") else ""
                where = f" at {facts['venue']}" if facts.get("venue") else ""
                org = facts.get("organization_name") or "The organization"
                lead.append(f"{org} conducted {facts['event_name']}{when}{where}.")
            updates["summary"] = " ".join(lead + loose)

        photos = [i for i in images if not i.source]  # skip pages rendered from uploaded documents
        if photos and tpl.field("pictures"):
            updates["pictures"] = [{"id": i.id, "caption": ""} for i in photos]

        filled = [tpl.field(k)["label"] for k in updates if tpl.field(k)]
        still = [m for m in missing if m["id"] not in updates]
        parts = []
        if filled:
            parts.append("I filled in: " + ", ".join(filled) + ".")
        else:
            parts.append("I couldn't find form details in that. Try lines like \"Event: ...\", \"Date: ...\", \"Venue: ...\".")
        if photos:
            parts.append("I added the image(s) to Pictures; click a caption in the preview to describe each one.")
        if still:
            ask = ", ".join(m["label"] for m in still[:3])
            parts.append(f"Still needed: {ask}. Send them here, or press Skip on any attachment you don't have.")
        else:
            parts.append("Everything is filled in. Review the preview and download when ready.")
        parts.append("(Offline mode: set GEMINI_API_KEY for smarter extraction and image reading.)")
        return ExtractionResult(updates=updates, reply=" ".join(parts))

    @staticmethod
    def _row(f, line: str) -> list[str]:
        cols = f.get("columns", [])
        m = TIME_ROW.match(line)
        if m and cols and cols[0].lower() == "time":
            rest = m.group(2)
            who = re.search(r"\(([^)]+)\)\s*$|\s+(?:by|-|–)\s+(.+)$", rest)
            if who:
                activity = rest[:who.start()].strip()
                return [m.group(1).strip(), activity, (who.group(1) or who.group(2)).strip()]
            return [m.group(1).strip(), rest.strip(), ""]
        line = re.sub(r"^\d+[.)]\s*", "", line)
        parts = [p.strip() for p in SPLIT.split(line, maxsplit=max(0, len(cols) - 1))]
        return parts
