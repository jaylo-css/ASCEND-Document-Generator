"""Fill a .docx template from document state, and render the same template as
an editable HTML preview.

Tokens (placed in the template by tools/build_*_template.py or by hand in Word):

    {{field}}          inline text field
    {{sig:role}}       signatory name: config.json signatories, overridable per document
    {{line:role}}      signature underline; blank when the document removed that signer
    {{title:role}}     signatory position: schema label, overridable per document
    {{annex:field}}    "see Annex B" / "N/A" marker for an attachment field
    {{rows:field}}     in a table row: that row is repeated once per row of a table field,
                       so the form's own styled table is filled in place
    {{total:field}}    sum of a table field's total_column (e.g. the Amount of a budget)
    {{#field}}         paragraph replaced by the field's block (paragraphs, table or images)
    {{#annexes}}       paragraph replaced by one annex page per provided attachment

A token must sit inside a single run to keep its formatting; if Word splits it
across runs the paragraph is merged into its first run so it still fills.
"""
from __future__ import annotations

import copy
import html
import io
import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.image.image import Image as DocxImage
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

from .registry import Template, is_placeholder, resolved_values

TOKEN = re.compile(r"\{\{(#?)(\w+)(?::(\w+))?\}\}")
ROWS = re.compile(r"\{\{rows:(\w+)\}\}")
BLANK_LINE = "_" * 45
SIG_LINE = "_" * 30  # fits before the 3-inch tab stop of the second signer
FONT = "Aptos"


class Context:
    def __init__(self, tpl: Template, state: dict, config: dict, upload_dir: Path):
        self.tpl = tpl
        self.values = resolved_values(tpl, state, config)
        self.skipped = set((state or {}).get("skipped") or [])
        sigs = dict(config.get("signatories", {}))
        for role, name in ((state or {}).get("signatories") or {}).items():
            if name and str(name).strip():
                sigs[role] = str(name).strip()
        self.signatories = sigs
        titles = dict(tpl.schema.get("signatories", {}))
        for role, title in ((state or {}).get("sig_titles") or {}).items():
            if role in titles and title and str(title).strip():
                titles[role] = str(title).strip()
        self.titles = titles
        removable = set(tpl.schema.get("removable_signatories", []))
        self.removed = removable & set((state or {}).get("sig_removed") or [])
        self.upload_dir = upload_dir
        self.annexes = []  # (letter, field)
        for f in tpl.fields:
            if f.get("attachment") and self.values.get(f["id"]):
                self.annexes.append((chr(ord("A") + len(self.annexes)), f))
        self.annex_letter = {f["id"]: letter for letter, f in self.annexes}

    def annex_marker(self, fid: str) -> tuple[str, str]:
        if fid in self.annex_letter:
            return f"– see Annex {self.annex_letter[fid]}", "attached"
        if fid in self.skipped:
            return "– N/A", "na"
        return "", "pending"

    def table_total(self, fid: str) -> str:
        f = self.tpl.field(fid)
        if not f or f["type"] != "table":
            return ""
        cols = f.get("columns", [])
        col = cols.index(f["total_column"]) if f.get("total_column") in cols else len(cols) - 1
        nums = [n for n in (_number(row[col]) for row in self.values.get(fid) or [] if col < len(row)) if n is not None]
        return f"{f.get('currency', '')}{sum(nums):,.2f}" if nums else ""

    def media_url(self, part, rid: str) -> str | None:
        """URL of an image embedded in the template (logos, header art) for the preview."""
        target = part.related_parts.get(rid) if rid else None
        if target is None:
            return None
        return f"/api/templates/{self.tpl.id}/media/{str(target.partname).rsplit('/', 1)[-1]}"

    def image_path(self, upload_id: str) -> Path | None:
        if not re.fullmatch(r"[\w-]+", upload_id or ""):
            return None
        hits = list(self.upload_dir.glob(upload_id + ".*"))
        return hits[0] if hits else None


def _number(text) -> float | None:
    """'₱1,500.00' -> 1500.0; None when the cell holds no number."""
    s = re.sub(r"[^\d.\-]", "", str(text or "").replace(",", ""))
    try:
        return float(s) if s.strip(".-") else None
    except ValueError:
        return None


def _in_cell(p: Paragraph) -> bool:
    return any(a.tag == qn("w:tc") for a in p._p.iterancestors())


def _paragraphs_of(container):
    for p in container.paragraphs:
        yield p
    for t in getattr(container, "tables", []):
        for row in t.rows:
            for cell in row.cells:
                yield from _paragraphs_of(cell)


def _all_paragraphs(doc):
    yield from _paragraphs_of(doc)
    for s in doc.sections:
        for part in (s.header, s.footer, s.first_page_header, s.first_page_footer):
            if part is not None and not part.is_linked_to_previous:
                yield from _paragraphs_of(part)


# ================================================================ DOCX


def fill_docx(tpl: Template, state: dict, config: dict, upload_dir: Path) -> bytes:
    ctx = Context(tpl, state, config, upload_dir)
    doc = Document(str(tpl.path))
    for t in doc.tables:
        _docx_fill_rows(t, ctx)
    blocks = []
    for p in list(_all_paragraphs(doc)):
        m = TOKEN.fullmatch(p.text.strip())
        if m and m.group(1) == "#":
            blocks.append((p, m.group(2)))
        elif "{{" in p.text:
            _fill_inline(p, ctx, blank="" if _in_cell(p) else BLANK_LINE)
    for p, name in blocks:
        if name == "annexes":
            _docx_annexes(doc, p, ctx)
        else:
            f = tpl.field(name)
            if f:
                _docx_field_block(doc, p, f, ctx.values.get(name), ctx, in_annex=False)
        p._p.getparent().remove(p._p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _inline_text(m: re.Match, ctx: Context, blank: str = BLANK_LINE) -> str:
    _, name, sub = m.groups()
    if name in ("sig", "line", "title") and sub in ctx.removed:
        return ""
    if name == "sig":
        return ctx.signatories.get(sub, f"[{sub.upper()}]")
    if name == "line":
        return SIG_LINE
    if name == "title":
        return ctx.titles.get(sub, sub)
    if name == "annex":
        return ctx.annex_marker(sub)[0]
    if name == "total":
        return ctx.table_total(sub)
    if name == "rows":
        return ""
    v = ctx.values.get(name)
    return v if isinstance(v, str) and v else blank


def _fill_inline(p: Paragraph, ctx: Context, blank: str = BLANK_LINE):
    sub = lambda m: _inline_text(m, ctx, blank)
    for r in p.runs:
        if "{{" in r.text:
            r.text = TOKEN.sub(sub, r.text)
    if TOKEN.search(p.text) and p.runs:  # token split across runs
        text = TOKEN.sub(sub, p.text)
        p.runs[0].text = text
        for r in p.runs[1:]:
            r._r.getparent().remove(r._r)


def _style_run(run, size=12, bold=None, italic=None, color=None):
    run.font.name = FONT
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def _new_par(doc, anchor: Paragraph, text="", *, model: Paragraph | None = None, size=12,
             bold=None, italic=None, align=None, after=None, page_break=False) -> Paragraph:
    """Insert a paragraph before anchor. With model, copy its paragraph and run formatting."""
    if model is not None:
        el = copy.deepcopy(model._p)
        for child in list(el):
            if child.tag != qn("w:pPr"):
                el.remove(child)
        anchor._p.addprevious(el)
        p = Paragraph(el, anchor._parent)
        rpr = model.runs[0]._r.rPr if model.runs else None
        r = p.add_run(text)
        if rpr is not None:
            r._r.insert(0, copy.deepcopy(rpr))
    else:
        p = doc.add_paragraph()
        anchor._p.addprevious(p._p)
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(text)
        _style_run(r, size, bold, italic)
    if align is not None:
        p.alignment = align
    if after is not None:
        p.paragraph_format.space_after = Pt(after)
    if page_break:
        p.paragraph_format.page_break_before = True
    return p


def _docx_field_block(doc, anchor: Paragraph, f: dict, value, ctx: Context, in_annex: bool):
    t = f["type"]
    if t in ("longtext", "text"):
        paras = [s.strip() for s in (value or "").split("\n") if s.strip()]
        if not paras:
            paras = [BLANK_LINE] * 3
        for text in paras:
            if in_annex:
                p = _new_par(doc, anchor, text, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=8)
                p.paragraph_format.first_line_indent = Inches(0.5)
            else:
                _new_par(doc, anchor, text, model=anchor, after=6 if len(paras) > 1 else None)
    elif t == "table":
        _docx_table(doc, anchor, f, value or [])
    elif t == "images":
        _docx_images(doc, anchor, f, value or [], ctx)


def _shade(cell, hex_fill):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcpr.append(shd)


def _docx_table(doc, anchor: Paragraph, f: dict, rows: list):
    cols = list(f.get("columns", []))
    numbered = f.get("numbered")
    if numbered:
        cols = ["No."] + cols
    table = doc.add_table(rows=1, cols=len(cols))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, c in enumerate(cols):
        cell = table.rows[0].cells[i]
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        _style_run(cell.paragraphs[0].add_run(c), 11, bold=True)
        _shade(cell, "D9DEEA")
    for n, row in enumerate(rows, 1):
        cells = table.add_row().cells
        vals = ([str(n)] if numbered else []) + list(row)
        for i, v in enumerate(vals):
            _style_run(cells[i].paragraphs[0].add_run(v), 11)
            if numbered and i == 0:
                cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    table.autofit = False
    total, first = 6.2, (0.55 if numbered else None)
    rest = (total - (first or 0)) / max(1, len(cols) - (1 if numbered else 0))
    widths = [Inches(first if numbered and i == 0 else rest) for i in range(len(cols))]
    for i, col in enumerate(table.columns):
        col.width = widths[i]
    for row in table.rows:
        for i, cell in enumerate(row.cells):
            cell.width = widths[i]
    tr = table.rows[0]._tr  # repeat header row on each page
    trpr = tr.get_or_add_trPr()
    hdr = OxmlElement("w:tblHeader")
    trpr.append(hdr)
    anchor._p.addprevious(table._tbl)


def _docx_fill_rows(table: Table, ctx: Context):
    """Repeat each {{rows:field}} row of a form's own table once per value row, keeping its styling."""
    for tr in list(table._tbl.tr_lst):
        m = ROWS.search("".join(t.text or "" for t in tr.iter(qn("w:t"))))
        f = ctx.tpl.field(m.group(1)) if m else None
        if not f:
            continue
        for row in ctx.values.get(f["id"]) or [[]]:  # no rows yet: one empty row, like the blank form
            new = copy.deepcopy(tr)
            for i, tc in enumerate(new.tc_lst):
                _set_cell_text(tc, row[i] if i < len(row) else "")
            tr.addprevious(new)
        tr.getparent().remove(tr)


def _set_cell_text(tc, text: str):
    """Replace a cell's content with text (one paragraph per line) in the cell's own formatting."""
    paras = tc.findall(qn("w:p"))
    first = paras[0]
    for p in paras[1:]:
        tc.remove(p)
    for child in list(first):
        if child.tag != qn("w:pPr"):
            first.remove(child)
    mark = first.find(qn("w:pPr") + "/" + qn("w:rPr"))  # the paragraph mark carries the cell's run format
    blank, cur = copy.deepcopy(first), None
    for line in (text or "").split("\n"):
        if cur is None:
            cur = first
        else:
            nxt = copy.deepcopy(blank)
            cur.addnext(nxt)
            cur = nxt
        if line:
            r = OxmlElement("w:r")
            if mark is not None:
                rpr = copy.deepcopy(mark)
                rpr.tag = qn("w:rPr")
                r.append(rpr)
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = line
            r.append(t)
            cur.append(r)


def _docx_images(doc, anchor: Paragraph, f: dict, images: list, ctx: Context):
    max_w, max_h = 6.0, (3.8 if f["id"] == "pictures" else 8.0)
    for n, item in enumerate(images, 1):
        path = ctx.image_path(item["id"])
        p = _new_par(doc, anchor, "", align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
        p.paragraph_format.keep_with_next = True
        if path is None:
            _style_run(p.add_run("[image not found]"), 11, italic=True)
        else:
            try:
                img = DocxImage.from_file(str(path))
                w_in = img.px_width / (img.horz_dpi or 96)
                h_in = img.px_height / (img.vert_dpi or 96)
                scale = min(max_w / w_in, max_h / h_in)
                p.add_run().add_picture(str(path), width=Inches(w_in * scale))
            except Exception:
                _style_run(p.add_run("[unsupported image format]"), 11, italic=True)
        caption = item.get("caption") or ""
        if f["id"] == "pictures":
            caption = f"Figure {n}. {caption}".strip()
        if caption:
            _new_par(doc, anchor, caption, size=11, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=14)
        else:
            _new_par(doc, anchor, "", size=6, after=10)


def _docx_annexes(doc, anchor: Paragraph, ctx: Context):
    for letter, f in ctx.annexes:
        _new_par(doc, anchor, f"ANNEX {letter}", size=14, bold=True,
                 align=WD_ALIGN_PARAGRAPH.CENTER, page_break=True)
        _new_par(doc, anchor, f.get("annex_title", f["label"]).upper(), size=12, bold=True,
                 align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
        event = ctx.values.get("event_name")
        if event:
            _new_par(doc, anchor, event, size=11, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=16)
        else:
            _new_par(doc, anchor, "", size=6, after=12)
        _docx_field_block(doc, anchor, f, ctx.values.get(f["id"]), ctx, in_annex=True)


# ================================================================ HTML preview

E = html.escape
TWIP_PX = 1 / 15  # 1440 twips per inch, 96 px per inch


def _twips(v) -> float:
    return int(v) * TWIP_PX if v not in (None, "") else 0.0


class _Numbering:
    """Minimal list numbering so "1.", "4.2" etc. show in the preview."""

    def __init__(self, doc):
        self.levels = {}  # (numId, ilvl) -> (fmt, text, left, hanging, isLgl)
        self.counters = {}
        try:
            root = doc.part.numbering_part.element
        except Exception:
            return
        abstract = {a.get(qn("w:abstractNumId")): a for a in root.findall(qn("w:abstractNum"))}
        for num in root.findall(qn("w:num")):
            a = abstract.get(num.find(qn("w:abstractNumId")).get(qn("w:val")))
            if a is None:
                continue
            for lvl in a.findall(qn("w:lvl")):
                g = lambda tag: (lvl.find(qn(tag)).get(qn("w:val")) if lvl.find(qn(tag)) is not None else None)
                ind = lvl.find(qn("w:pPr") + "/" + qn("w:ind"))
                self.levels[(num.get(qn("w:numId")), int(lvl.get(qn("w:ilvl"))))] = (
                    g("w:numFmt") or "decimal", g("w:lvlText") or "",
                    _twips(ind.get(qn("w:left")) if ind is not None else 0),
                    _twips(ind.get(qn("w:hanging")) if ind is not None else 0),
                    lvl.find(qn("w:isLgl")) is not None)

    @staticmethod
    def _fmt(n, fmt):
        if fmt == "lowerLetter":
            return chr(96 + n)
        if fmt == "upperLetter":
            return chr(64 + n)
        if fmt in ("lowerRoman", "upperRoman"):
            vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
            out = ""
            for v, s in vals:
                while n >= v:
                    out, n = out + s, n - v
            return out if fmt == "lowerRoman" else out.upper()
        if fmt == "bullet":
            return "•"
        return str(n)

    def label(self, p: Paragraph):
        ppr = p._p.pPr
        numpr = ppr.find(qn("w:numPr")) if ppr is not None else None
        if numpr is None or numpr.find(qn("w:numId")) is None:
            return None
        num_id = numpr.find(qn("w:numId")).get(qn("w:val"))
        ilvl_el = numpr.find(qn("w:ilvl"))
        ilvl = int(ilvl_el.get(qn("w:val"))) if ilvl_el is not None else 0
        info = self.levels.get((num_id, ilvl))
        if not info:
            return None
        fmt, text, left, hanging, is_lgl = info
        c = self.counters.setdefault(num_id, [0] * 9)
        c[ilvl] += 1
        for k in range(ilvl + 1, 9):
            c[k] = 0
        def sub(m):
            k = int(m.group(1)) - 1
            lf = self.levels.get((num_id, k), ("decimal",))[0]
            return self._fmt(max(c[k], 1), "decimal" if is_lgl and lf != "bullet" else lf)
        return re.sub(r"%(\d)", sub, text) if fmt != "bullet" else "•", left, hanging


def _par_style(p: Paragraph, num_info=None, default_after_px: float | None = None) -> tuple[str, list]:
    css = []
    ppr = p._p.pPr
    jc = ppr.find(qn("w:jc")).get(qn("w:val")) if ppr is not None and ppr.find(qn("w:jc")) is not None else None
    css.append({"center": "text-align:center", "right": "text-align:right", "end": "text-align:right",
                "both": "text-align:justify"}.get(jc, "text-align:left"))
    left = hanging = first = 0.0
    if num_info:
        left, hanging = num_info[1], num_info[2]
    elif p.style is not None and p.style.paragraph_format.left_indent:
        left = p.style.paragraph_format.left_indent / 9525  # EMU -> px
    ind = ppr.find(qn("w:ind")) if ppr is not None else None
    if ind is not None:
        if ind.get(qn("w:left")) is not None or ind.get(qn("w:start")) is not None:
            left = _twips(ind.get(qn("w:left")) or ind.get(qn("w:start")))
        if ind.get(qn("w:hanging")) is not None:
            hanging = _twips(ind.get(qn("w:hanging")))
        if ind.get(qn("w:firstLine")) is not None:
            first = _twips(ind.get(qn("w:firstLine")))
    css.append(f"padding-left:{left:.1f}px")
    if first:
        css.append(f"text-indent:{first:.1f}px")
    after = p.paragraph_format.space_after
    if after is None and default_after_px is None:
        after = p.style.paragraph_format.space_after if p.style is not None else None
    after_px = (after / 9525) if after is not None else (
        default_after_px if default_after_px is not None else 160 * TWIP_PX)
    css.append(f"margin:0 0 {after_px:.1f}px 0")
    if p.paragraph_format.page_break_before:
        css.append("break-before:page")
    tabs = []
    tabs_el = ppr.find(qn("w:tabs")) if ppr is not None else None
    if tabs_el is not None:
        tabs = [round(_twips(t.get(qn("w:pos")))) for t in tabs_el.findall(qn("w:tab"))
                if t.get(qn("w:val")) not in ("clear",)]
    return ";".join(css), tabs


def _run_css(r) -> str:
    css = []
    if r.bold:
        css.append("font-weight:700")
    if r.italic:
        css.append("font-style:italic")
    if r.underline:
        css.append("text-decoration:underline")
    if r.font.size:
        css.append(f"font-size:{r.font.size.pt * 4 / 3:.1f}px")
    return ";".join(css)


def _html_inline_token(m: re.Match, ctx: Context) -> str:
    _, name, sub = m.groups()
    if name in ("sig", "line", "title") and sub in ctx.removed:
        if name == "title":  # keep a way back
            return f'<button type="button" class="sig-add no-print" data-sig-add="{E(sub)}">+ Add second signer</button>'
        return ""
    if name == "line":
        return SIG_LINE
    if name == "title":
        x = ""
        if sub in ctx.tpl.schema.get("removable_signatories", []):
            x = (f'<button type="button" class="sig-x no-print" data-sig-remove="{E(sub)}" '
                 f'title="Remove this signer (one officer prepares the report)">×</button>')
        return (f'<span class="fld sig-title" contenteditable="plaintext-only" spellcheck="false" '
                f'data-sigtitle="{E(sub)}" title="Position (click to edit)">{E(ctx.titles.get(sub, sub))}</span>{x}')
    if name == "sig":
        role_label = ctx.titles.get(sub, sub)
        v = ctx.signatories.get(sub, "")
        cls = "fld sig" + (" placeholder" if is_placeholder(v) or not v else "")
        return (f'<span class="{cls}" contenteditable="plaintext-only" spellcheck="false" '
                f'data-sig="{E(sub)}" data-label="{E(role_label)}" title="{E(role_label)} (click to edit)">{E(v)}</span>')
    if name == "total":
        f = ctx.tpl.field(sub) or {}
        return (f'<span class="calc" title="Added up from the {E(f.get("total_column", "last"))} column">'
                f'{E(ctx.table_total(sub))}</span>')
    if name == "rows":
        return ""
    if name == "annex":
        text, state = ctx.annex_marker(sub)
        if state == "pending":
            text = "– not yet provided"
        return f'<span class="annex-tag {state}" data-annex="{E(sub)}">{E(text)}</span>'
    f = ctx.tpl.field(name)
    if not f:
        return E(m.group(0))
    v = ctx.values.get(name) or ""
    return (f'<span class="fld{"" if v else " empty"}" contenteditable="plaintext-only" '
            f'data-field="{E(name)}" data-label="{E(f["label"])}">{E(v)}</span>')


def _html_img(url: str, d, cls: str, style: str) -> str:
    """An image of a Word drawing at its displayed size, honoring Word's crop (a:srcRect)."""
    ext = d.find(qn("wp:extent"))
    w, h = int(ext.get("cx")) / 9525, int(ext.get("cy")) / 9525
    crop = d.find(".//" + qn("a:srcRect"))
    if crop is None:
        return f'<img class="{cls}" src="{E(url)}" alt="" style="{style}width:{w:.0f}px;height:{h:.0f}px">'
    l, t, r, b = (int(crop.get(k) or 0) / 100000 for k in ("l", "t", "r", "b"))  # 1/1000 of a percent
    fw, fh = w / max(1 - l - r, 0.01), h / max(1 - t - b, 0.01)
    return (f'<span class="{cls} crop" style="{style}width:{w:.0f}px;height:{h:.0f}px">'
            f'<img src="{E(url)}" alt="" style="width:{fw:.0f}px;height:{fh:.0f}px;'
            f'left:{-l * fw:.0f}px;top:{-t * fh:.0f}px"></span>')


def _html_drawing(r, p: Paragraph, ctx: Context) -> str:
    out = []
    for d in r._r.iter(qn("wp:inline"), qn("wp:anchor")):
        blip = d.find(".//" + qn("a:blip"))
        url = ctx.media_url(p.part, blip.get(qn("r:embed"))) if blip is not None else None
        if not url or d.find(qn("wp:extent")) is None:
            continue
        style = ""
        if d.tag == qn("wp:anchor"):  # floating picture in the text (e.g. a logo in a cell): keep its offset
            off = [d.find(qn(f"wp:position{a}") + "/" + qn("wp:posOffset")) for a in "HV"]
            x, y = (int(o.text) / 9525 if o is not None else 0 for o in off)
            style = f"margin:{max(y, 0):.0f}px 0 0 {max(x, 0):.0f}px;"
        out.append(_html_img(url, d, "dpic", style))
    return "".join(out)


def _html_runs(p: Paragraph, ctx: Context, images: bool = True) -> str:
    out = []
    for r in p.runs:
        if r._r.rPr is not None and r._r.rPr.find(qn("w:vanish")) is not None:
            continue
        if images and r._r.find(qn("w:drawing")) is not None:
            out.append(_html_drawing(r, p, ctx))
        text = r.text
        if not text:
            if r._r.find(qn("w:br")) is not None:
                out.append("<br>")
            continue
        css = _run_css(r)
        parts = []
        for seg in re.split(r"(\t|\n)", text):
            if seg == "\t":
                parts.append('<span class="tab"></span>')
            elif seg == "\n":
                parts.append("<br>")
            else:
                pos, buf = 0, []
                for m in TOKEN.finditer(seg):
                    buf.append(E(seg[pos:m.start()]))
                    buf.append(_html_inline_token(m, ctx))
                    pos = m.end()
                buf.append(E(seg[pos:]))
                parts.append("".join(buf))
        inner = "".join(parts)
        out.append(f'<span style="{css}">{inner}</span>' if css else inner)
    return "".join(out)


def _html_par(p: Paragraph, ctx: Context, numbering: _Numbering, *, images: bool = True,
              default_after_px: float | None = None) -> str:
    num = numbering.label(p)
    css, tabs = _par_style(p, num, default_after_px)
    label = ""
    if num:
        size = _run_css(p.runs[0]) if p.runs else ""
        label = (f'<span class="num" style="min-width:{num[2]:.1f}px;margin-left:-{num[2]:.1f}px;{size}">'
                 f'{E(num[0])}</span>')
    body = _html_runs(p, ctx, images)
    if not body.strip() and not label:
        body = "&nbsp;"
    data = f' data-tabs="{",".join(map(str, tabs))}"' if tabs else ""
    return f'<p class="dp" style="{css}"{data}>{label}{body}</p>'


def _html_field_block(f: dict, value, ctx: Context, model_css: str = "") -> str:
    fid = E(f["id"])
    t = f["type"]
    if t in ("longtext", "text"):
        paras = [s.strip() for s in (value or "").split("\n") if s.strip()]
        inner = "".join(f"<p>{E(s)}</p>" for s in paras)
        return (f'<div class="fld blk{"" if paras else " empty"}" contenteditable="true" data-field="{fid}" '
                f'data-kind="longtext" data-label="{E(f["label"])}" style="{model_css}">{inner}</div>')
    if t == "table":
        cols = list(f.get("columns", []))
        num = bool(f.get("numbered"))
        head = ("<th class='no'>No.</th>" if num else "") + "".join(f"<th>{E(c)}</th>" for c in cols)
        rows = []
        for i, row in enumerate(value or []):
            cells = (f"<td class='no'>{i + 1}</td>" if num else "") + "".join(
                f'<td contenteditable="plaintext-only" data-field="{fid}" data-row="{i}" data-col="{j}">{E(c)}</td>'
                for j, c in enumerate(row))
            rows.append(f'<tr>{cells}<td class="row-x no-print"><button data-del-row="{i}" data-field="{fid}" '
                        f'title="Remove row">×</button></td></tr>')
        return (f'<table class="dtable" data-table="{fid}"><thead><tr>{head}<th class="row-x no-print"></th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>'
                f'<button class="add-row no-print" data-add-row="{fid}">+ Add row</button>')
    if t == "images":
        figs = []
        for i, item in enumerate(value or []):
            prefix = f"Figure {i + 1}. " if f["id"] == "pictures" else ""
            figs.append(
                f'<figure class="dimg"><img src="/api/uploads/{E(item["id"])}" alt="">'
                f'<figcaption>{E(prefix)}<span class="fld cap{"" if item.get("caption") else " empty"}" '
                f'contenteditable="plaintext-only" data-field="{fid}" data-img="{i}" data-label="Add a caption">'
                f'{E(item.get("caption") or "")}</span></figcaption>'
                f'<button class="img-x no-print" data-del-img="{i}" data-field="{fid}" title="Remove image">×</button>'
                f'</figure>')
        return "".join(figs)
    return ""


def render_html(tpl: Template, state: dict, config: dict, upload_dir: Path) -> str:
    ctx = Context(tpl, state, config, upload_dir)
    doc = Document(str(tpl.path))
    numbering = _Numbering(doc)
    sec = doc.sections[0]
    page_css = (f"width:{sec.page_width / 9525:.0f}px;min-height:{sec.page_height / 9525:.0f}px;"
                f"padding:{sec.top_margin / 9525:.0f}px {sec.right_margin / 9525:.0f}px "
                f"{sec.bottom_margin / 9525:.0f}px {sec.left_margin / 9525:.0f}px")
    header = floats = ""
    if sec.header is not None and not sec.header.is_linked_to_previous:
        header = "".join(_html_par(p, ctx, numbering, images=False) for p in sec.header.paragraphs)
        header = f'<div class="dhead" style="top:{(sec.header_distance or 0) / 9525:.0f}px">{header}</div>'
        floats += _html_floats(sec.header, sec, ctx, footer=False)
    if sec.footer is not None and not sec.footer.is_linked_to_previous:
        floats += _html_floats(sec.footer, sec, ctx, footer=True)

    body, annex_html = [], ""
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p"):
            p = Paragraph(el, doc)
            m = TOKEN.fullmatch(p.text.strip())
            if m and m.group(1) == "#":
                name = m.group(2)
                if name == "annexes":
                    annex_html = _html_annexes(ctx, page_css)
                elif tpl.field(name):
                    css, _ = _par_style(p)
                    body.append(_html_field_block(tpl.field(name), ctx.values.get(name), ctx, css))
                continue
            body.append(_html_par(p, ctx, numbering))
        elif el.tag == qn("w:tbl"):
            body.append(_html_table(Table(el, doc), ctx, numbering))

    cls = "page has-floats" if floats else "page"
    return (f'<section class="{cls}" style="{page_css}">{floats}{header}{"".join(body)}</section>{annex_html}')

FOOTER_LINE_PX = 19  # height of the footer paragraph that footer art is anchored to


def _html_floats(part, sec, ctx: Context, footer: bool) -> str:
    """Floating header/footer images (banners, logos, watermark) placed where Word puts them.
    Header art is measured from the top of the page, footer art from the bottom."""
    px = lambda emu: int(emu or 0) / 9525
    page_h, top_m, left_m = px(sec.page_height), px(sec.top_margin), px(sec.left_margin)
    out = []
    for a in part._element.iter(qn("wp:anchor")):
        blip, ext = a.find(".//" + qn("a:blip")), a.find(qn("wp:extent"))
        url = ctx.media_url(part.part, blip.get(qn("r:embed"))) if blip is not None else None
        if not url or ext is None:
            continue
        w, h = px(ext.get("cx")), px(ext.get("cy"))

        def pos(axis):
            el = a.find(qn(f"wp:position{axis}"))
            off = el.find(qn("wp:posOffset")) if el is not None else None
            return (el.get("relativeFrom") if el is not None else "page"), px(off.text if off is not None else 0)

        rel, x = pos("H")
        if rel not in ("page", "leftMargin"):
            x += left_m
        rel, y = pos("V")
        if footer:
            base = {"page": 0, "margin": top_m, "bottomMargin": page_h - px(sec.bottom_margin)}.get(
                rel, page_h - px(sec.footer_distance) - FOOTER_LINE_PX)
            vert = f"bottom:{page_h - (base + y) - h:.0f}px"
        else:
            base = {"page": 0, "margin": top_m, "topMargin": 0}.get(rel, px(sec.header_distance))
            vert = f"top:{base + y:.0f}px"
        behind = " behind" if a.get("behindDoc") == "1" else ""
        out.append(_html_img(url, a, f"dfloat{behind}", f"left:{x:.0f}px;{vert};"))
    return "".join(out)


def _tc_info(tc) -> tuple[int, str | None, str]:
    """(grid span, vMerge state, cell css) of a table cell."""
    pr = tc.tcPr

    def val(tag):
        el = pr.find(qn(tag)) if pr is not None else None
        return el.get(qn("w:val")) if el is not None else None

    span = int(val("w:gridSpan") or 1)
    vmerge = None
    if pr is not None and pr.find(qn("w:vMerge")) is not None:
        vmerge = val("w:vMerge") or "continue"
    css = []
    shd = pr.find(qn("w:shd")) if pr is not None else None
    fill = shd.get(qn("w:fill")) if shd is not None else None
    if fill and fill.lower() not in ("auto", "ffffff"):
        css.append(f"background:#{fill}")
    css.append("vertical-align:" + {"center": "middle", "bottom": "bottom"}.get(val("w:vAlign"), "top"))
    return span, vmerge, ";".join(css)


def _html_table(t: Table, ctx: Context, numbering: _Numbering) -> str:
    """A table of the form itself: column widths, shading and merged cells as in Word. A row holding
    {{rows:field}} becomes the editable rows of that table field (header rows above, totals below)."""
    tbl = t._tbl
    grid = [_twips(g.get(qn("w:w"))) for g in tbl.tblGrid.findall(qn("w:gridCol"))]
    ind = tbl.tblPr.find(qn("w:tblInd")) if tbl.tblPr is not None else None
    style_el = t.style.element if t.style is not None else None
    sp = style_el.find(qn("w:pPr") + "/" + qn("w:spacing")) if style_el is not None else None
    after = _twips(sp.get(qn("w:after"))) if sp is not None and sp.get(qn("w:after")) is not None else None
    bordered = any(x is not None and x.find(".//" + qn("w:tblBorders")) is not None for x in (tbl.tblPr, style_el))

    rows = [[(tc, *_tc_info(tc)) for tc in tr.tc_lst] for tr in tbl.tr_lst]
    starts = []  # grid column where each cell starts, for vertical merges
    for cells in rows:
        col, s = 0, []
        for _, span, _, _ in cells:
            s.append(col)
            col += span
        starts.append(s)

    def rowspan(r, c):
        n = 1
        for rr in range(r + 1, len(rows)):
            if not any(st == starts[r][c] and rows[rr][i][2] == "continue" for i, st in enumerate(starts[rr])):
                break
            n += 1
        return n

    head, data, foot, field = [], [], [], None
    for r, (tr, cells) in enumerate(zip(tbl.tr_lst, rows)):
        m = ROWS.search("".join(x.text or "" for x in tr.iter(qn("w:t"))))
        f = ctx.tpl.field(m.group(1)) if m else None
        if f:
            field = f
            for i, row in enumerate(ctx.values.get(f["id"]) or [None]):
                tds = []
                for k, (tc, span, _, css) in enumerate(cells):
                    p0 = tc.find(qn("w:p"))
                    align = _par_style(Paragraph(p0, t))[0].split(";")[0] if p0 is not None else ""
                    val = row[k] if row and k < len(row) else ""
                    tds.append(f'<td{f" colspan={span}" if span > 1 else ""} style="{css};{align}" '
                               f'class="wcell" contenteditable="plaintext-only" data-field="{E(f["id"])}" '
                               f'data-row="{i}" data-col="{k}">{E(val)}</td>')
                x = (f'<button data-del-row="{i}" data-field="{E(f["id"])}" title="Remove row">×</button>'
                     if row is not None else "")
                data.append(f'<tr>{"".join(tds)}<td class="row-x no-print">{x}</td></tr>')
            continue
        tds = []
        for c, (tc, span, vmerge, css) in enumerate(cells):
            if vmerge == "continue":
                continue
            attrs = (f" colspan={span}" if span > 1 else "") + (
                f" rowspan={rowspan(r, c)}" if vmerge == "restart" else "")
            inner = "".join(_html_par(Paragraph(p, t), ctx, numbering, default_after_px=after)
                            for p in tc.findall(qn("w:p")))
            tds.append(f'<td{attrs} style="{css}">{inner}</td>')
        (foot if field else head).append(f'<tr>{"".join(tds)}</tr>')

    cols = "".join(f'<col style="width:{w:.0f}px">' for w in grid)
    left = _twips(ind.get(qn("w:w"))) if ind is not None else 0
    css = f"width:{sum(grid):.0f}px;margin-left:{left:.0f}px"
    cls = "wtable" + (" grid" if bordered else "")
    if not field:
        return f'<table class="{cls}" style="{css}"><colgroup>{cols}</colgroup><tbody>{"".join(head)}</tbody></table>'
    return (f'<table class="{cls}" style="{css}" data-table="{E(field["id"])}"><colgroup>{cols}</colgroup>'
            f'<thead>{"".join(head)}</thead><tbody>{"".join(data)}</tbody><tfoot>{"".join(foot)}</tfoot></table>'
            f'<button class="add-row no-print" data-add-row="{E(field["id"])}">+ Add row</button>')


def _html_annexes(ctx: Context, page_css: str) -> str:
    out = []
    event = ctx.values.get("event_name") or ""
    for letter, f in ctx.annexes:
        out.append(
            f'<section class="page annex" style="{page_css}">'
            f'<h2 class="annex-h">ANNEX {letter}</h2><h3 class="annex-t">{E(f.get("annex_title", f["label"]).upper())}</h3>'
            f'{f"<p class=annex-e>{E(event)}</p>" if event else ""}'
            f'{_html_field_block(f, ctx.values.get(f["id"]), ctx, "text-align:justify;text-indent:48px")}'
            f'</section>')
    return "".join(out)
