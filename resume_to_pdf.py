#!/usr/bin/env python3
"""Convert a Markdown resume (*resume.md) into an ATS-friendly PDF (*resume.pdf).

Usage:
    python resume_to_pdf.py Jane_Doe_Resume.md          # -> Jane_Doe_Resume.pdf
    python resume_to_pdf.py a_resume.md b_resume.md     # several files
    python resume_to_pdf.py                             # every *resume.md in the current folder
    python resume_to_pdf.py in.md -o out.pdf --show-changes

Nothing personal is stored in this script. Name, contact details, links and all
content are read from the Markdown file. Only the visual design lives here.

Markdown conventions understood (same layout as the reference resume):
    # NAME                         centered name
    plain line after name          subtitle; a later line with " | " is the contact line
    ## SECTION                     section heading with a rule under it
    ### Company — dates            bold entry with right-aligned dates; the next
                                   **Role · Location** line is styled as a role line
    **Item title**                 bold sub-heading (project / achievement)
    **Title** — date               inline "Title | date" when bullets follow,
                                   right-aligned date otherwise (e.g. education)
    Label: value                   "Tech Stack: ..." style line (bold label, italic value)
    - bullet                       bullet point
    - **Label:** text              key/value row without a bullet (skills)
    <!-- comment -->               ignored

Requires: reportlab  (pip install reportlab)
"""
import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Table, TableStyle

# --------------------------------------------------------------------------- #
# Design (visual only, no personal data)
# --------------------------------------------------------------------------- #
ACCENT = "#1F4E79"      # name, section headings, rules
ROLE = "#2E75B6"        # role line
MUTED = "#666666"       # subtitle, dates, locations, tech stack values
LINK = "#0563C1"
TEXT = "#000000"

MARGIN_X = 49
MARGIN_TOP = 40
MARGIN_BOTTOM = 36
DATE_INSET = 46         # distance of right-aligned dates from the right margin
DATE_COL = 150

# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
FONT_FILES = {
    "normal": "LiberationSans-Regular.ttf",
    "bold": "LiberationSans-Bold.ttf",
    "italic": "LiberationSans-Italic.ttf",
    "boldItalic": "LiberationSans-BoldItalic.ttf",
}


def _font_dirs(extra):
    home = Path.home()
    dirs = [extra, os.environ.get("RESUME_FONT_DIR"),
            "/usr/share/fonts", "/usr/local/share/fonts",
            home / ".fonts", home / ".local/share/fonts",
            "/Library/Fonts", "/System/Library/Fonts/Supplemental"]
    return [Path(d) for d in dirs if d and Path(d).is_dir()]


def setup_fonts(extra_dir=None):
    """Register an embedded TrueType family (so text stays selectable and
    machine-readable). Falls back to built-in Helvetica when not found."""
    found = {}
    for d in _font_dirs(extra_dir):
        for style, fname in FONT_FILES.items():
            if style not in found:
                hit = next(d.rglob(fname), None)
                if hit:
                    found[style] = hit
    if len(found) == len(FONT_FILES):
        for style, path in found.items():
            pdfmetrics.registerFont(TTFont(f"Body-{style}", str(path)))
        registerFontFamily("Body", normal="Body-normal", bold="Body-bold",
                           italic="Body-italic", boldItalic="Body-boldItalic")
        return "Body-normal", "Body-bold", "Body-italic"
    print("note: Liberation Sans not found, falling back to Helvetica "
          "(use --font-dir or RESUME_FONT_DIR to point at the fonts).", file=sys.stderr)
    registerFontFamily("Helvetica", normal="Helvetica", bold="Helvetica-Bold",
                       italic="Helvetica-Oblique", boldItalic="Helvetica-BoldOblique")
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


# --------------------------------------------------------------------------- #
# Humanizer
# --------------------------------------------------------------------------- #
# Purpose: remove the typographic and vocabulary habits that make text look
# machine-written, and normalise characters that confuse ATS parsers. It never
# adds claims; it only swaps punctuation and a few stock words.
DEFAULT_WORD_RULES = {
    r"leverag(?:e|es)": "use",
    r"leveraged": "used",
    r"leveraging": "using",
    r"utili[sz]e": "use",
    r"utili[sz]es": "uses",
    r"utili[sz]ed": "used",
    r"utili[sz]ing": "using",
    r"robust": "reliable",
    r"cutting-edge": "modern",
    r"state-of-the-art": "modern",
    r"spearheaded": "led",
    r"pivotal": "key",
    r"seamlessly": "smoothly",
    r"seamless": "smooth",
    r"synergy": "collaboration",
    r"delve into": "look into",
}

_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


class Humanizer:
    def __init__(self, enabled=True, extra_rules=None):
        self.enabled = enabled
        self.changes = []
        rules = dict(DEFAULT_WORD_RULES)
        rules.update(extra_rules or {})
        # Longest patterns first so "leveraging" wins over "leverage".
        self.word_rules = [(re.compile(rf"\b{p}\b", re.I), r)
                           for p, r in sorted(rules.items(), key=lambda kv: -len(kv[0]))]

    @staticmethod
    def _match_case(src, repl):
        if src.isupper() and len(src) > 1:
            return repl.upper()
        if src[:1].isupper():
            return repl[:1].upper() + repl[1:]
        return repl

    def __call__(self, text):
        if not self.enabled or not text:
            return text
        out = text
        # Characters: invisible chars, non-breaking spaces, curly quotes, ellipsis.
        out = _INVISIBLE.sub("", out)
        out = out.replace("\u00a0", " ").replace("\u202f", " ")
        out = out.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2026: "..."})
        # Em dashes read as AI-written; use plain punctuation instead.
        out = re.sub(r"\s+\u2014\s+", " - ", out)
        out = re.sub(r"\s*\u2014\s*", ", ", out)
        # Stock vocabulary.
        for pat, repl in self.word_rules:
            out = pat.sub(lambda m, r=repl: self._match_case(m.group(0), r), out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        if out != text:
            self.changes.append((text, out))
        return out


# --------------------------------------------------------------------------- #
# Inline Markdown -> ReportLab markup
# --------------------------------------------------------------------------- #
TOKEN = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)|\*\*(.+?)\*\*|\*(.+?)\*")


def inline(text, hz, links_as_url=False):
    out, pos = [], 0
    for m in TOKEN.finditer(text):
        out.append(escape(hz(text[pos:m.start()])))
        if m.group(1):
            label, url = m.group(1), m.group(2)
            shown = url if links_as_url else hz(label)
            out.append(f'<a href={quoteattr(url)} color="{LINK}"><u>{escape(shown)}</u></a>')
        elif m.group(3):
            out.append(f"<b>{inline(m.group(3), hz, links_as_url)}</b>")
        else:
            out.append(f"<i>{inline(m.group(4), hz, links_as_url)}</i>")
        pos = m.end()
    out.append(escape(hz(text[pos:])))
    return "".join(out)


def plain(text):
    """Strip Markdown markers; used for document metadata."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[*_`]", "", text).strip()


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #
def make_styles(regular, bold, italic):
    def S(name, **kw):
        base = dict(fontName=regular, fontSize=10, leading=11.5, textColor=HexColor(TEXT))
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "name": S("name", fontName=bold, fontSize=24, leading=27, alignment=TA_CENTER,
                  textColor=HexColor(ACCENT), spaceAfter=2),
        "subtitle": S("subtitle", fontSize=11, leading=13, alignment=TA_CENTER,
                      textColor=HexColor(MUTED), spaceAfter=4),
        "contact": S("contact", fontSize=9.5, leading=11, alignment=TA_CENTER,
                     textColor=HexColor(MUTED)),
        "section": S("section", fontName=bold, fontSize=13, leading=14.5,
                     textColor=HexColor(ACCENT), spaceBefore=15, keepWithNext=1),
        "body": S("body"),
        "entry": S("entry", fontName=bold, fontSize=11, leading=12.5),
        "date": S("date", fontSize=9.5, leading=12.5, alignment=TA_RIGHT,
                  textColor=HexColor(MUTED)),
        "role": S("role", fontName=italic, fontSize=10, textColor=HexColor(ROLE),
                  spaceAfter=4.5, keepWithNext=1),
        "title": S("title", fontName=bold, fontSize=10.5, leading=11.7,
                   spaceBefore=4.5, spaceAfter=2, keepWithNext=1),
        "subtext": S("subtext", textColor=HexColor(MUTED), spaceAfter=2),
        "bullet": S("bullet", leftIndent=36 - 0, bulletIndent=18, spaceAfter=2,
                    bulletFontName=regular, bulletFontSize=10),
        "kv": S("kv", spaceAfter=2),
        "tech": S("tech", fontSize=9.5, leading=11.5, leftIndent=18, spaceAfter=2,
                  textColor=HexColor(MUTED)),
    }


# --------------------------------------------------------------------------- #
# Markdown -> flowables
# --------------------------------------------------------------------------- #
RE_H1 = re.compile(r"^#\s+(.+)$")
RE_H2 = re.compile(r"^##\s+(.+)$")
RE_H3 = re.compile(r"^###\s+(.+)$")
RE_BULLET = re.compile(r"^[-*+]\s+(.+)$")
RE_BOLD_ONLY = re.compile(r"^\*\*(.+)\*\*$")
RE_BOLD_DATE = re.compile(r"^\*\*(.+?)\*\*\s+[\u2014\u2013-]\s+(.+)$")
RE_BOLD_START = re.compile(r"^\*\*.+?\*\*(\s+\*[^*]+\*)?$")
RE_KV_BULLET = re.compile(r"^\*\*[^*]+:\*\*\s*.+$")
RE_LABEL = re.compile(r"^([A-Z][A-Za-z &/]{1,30}):\s+(.+)$")


def split_last(text, seps):
    for sep in seps:
        if sep in text:
            left, right = text.rsplit(sep, 1)
            return left.strip(), right.strip()
    return text.strip(), ""


def date_row(left_html, right_html, st, width, space_before):
    t = Table([[Paragraph(left_html, st["entry"]), Paragraph(right_html, st["date"])]],
              colWidths=[width - DATE_COL, DATE_COL])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (1, 0), (1, 0), DATE_INSET),
    ]))
    t.hAlign = "LEFT"
    t.spaceBefore = space_before
    t.spaceAfter = 2
    t.keepWithNext = 1
    return t


def build_story(md_text, st, hz, width):
    md_text = re.sub(r"<!--.*?-->", "", md_text, flags=re.S)
    lines = [ln.rstrip() for ln in md_text.splitlines()]
    nonblank = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip()]
    story, meta = [], {"name": "", "headline": ""}
    prev = None            # kind of the previous element
    header_done = False
    subtitle_done = False
    I = lambda t, **kw: inline(t, hz, **kw)

    for idx, (_, s) in enumerate(nonblank):
        nxt = nonblank[idx + 1][1] if idx + 1 < len(nonblank) else ""

        m = RE_H1.match(s) if not RE_H2.match(s) else None
        if m:
            meta["name"] = plain(m.group(1))
            story.append(Paragraph(I(m.group(1)), st["name"]))
            prev = "h1"
            continue

        m = RE_H2.match(s)
        if m:
            if not header_done:
                story.append(HRFlowable(width="100%", thickness=1, color=HexColor(ACCENT),
                                        spaceBefore=8, spaceAfter=0))
                header_done = True
            story.append(Paragraph(I(m.group(1).upper() if m.group(1).islower() else m.group(1)),
                                   st["section"]))
            story.append(HRFlowable(width="100%", thickness=1, color=HexColor(ACCENT),
                                    spaceBefore=9, spaceAfter=5))
            prev = "h2"
            continue

        if not header_done and prev in ("h1", "subtitle"):
            if "|" in s or "@" in s:
                html = I(s.replace(" | ", "  |  "), links_as_url=True)
                story.append(Paragraph(html, st["contact"]))
                prev = "contact"
            else:
                if not subtitle_done:
                    meta["headline"] = plain(s)
                    subtitle_done = True
                story.append(Paragraph(I(s).replace(" \u00b7 ", "  \u00b7  "), st["subtitle"]))
                prev = "subtitle"
            continue

        m = RE_H3.match(s)
        if m:
            left, right = split_last(m.group(1), [" \u2014 ", " \u2013 ", " | "])
            story.append(date_row(I(left), I(right), st, width, 9))
            prev = "h3"
            continue

        m = RE_BULLET.match(s)
        if m:
            body = m.group(1)
            if RE_KV_BULLET.match(body):
                story.append(Paragraph(I(body), st["kv"]))
                prev = "kv"
            else:
                story.append(Paragraph(I(body), st["bullet"], bulletText="\u2022"))
                prev = "bullet"
            continue

        if prev == "h3" and RE_BOLD_ONLY.match(s):
            role, place = split_last(RE_BOLD_ONLY.match(s).group(1), [" \u00b7 "])
            html = I(role)
            if place:
                html += f' <font color="{MUTED}" name="{st["body"].fontName}">&nbsp;\u00b7&nbsp; {I(place)}</font>'
            story.append(Paragraph(html, st["role"]))
            prev = "role"
            continue

        m = RE_BOLD_DATE.match(s)
        if m:
            if RE_BULLET.match(nxt):
                html = f'{I("**" + m.group(1) + "**")}  |  {I("**" + m.group(2) + "**")}'
                story.append(Paragraph(html, st["title"]))
                prev = "title"
            else:
                story.append(date_row(I(m.group(1)), I(m.group(2)), st, width, 9))
                prev = "datehead"
            continue

        if RE_BOLD_START.match(s):
            story.append(Paragraph(I(s), st["title"]))
            prev = "title"
            continue

        m = RE_LABEL.match(s)
        if m and prev in ("bullet", "title", "tech", "role"):
            html = f"<b>{escape(hz(m.group(1)))}:</b> <i>{I(m.group(2))}</i>"
            story.append(Paragraph(html, st["tech"]))
            prev = "tech"
            continue

        if prev in ("datehead", "h3", "role") and not RE_BULLET.match(s):
            story.append(Paragraph(I(s).replace(" \u00b7 ", "  \u00b7  "), st["subtext"]))
            prev = "subtext"
            continue

        story.append(Paragraph(I(s), st["body"]))
        prev = "body"

    return story, meta


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def convert(src, dst, humanize=True, rules=None, font_dir=None, show_changes=False):
    regular, bold, italic = setup_fonts(font_dir)
    st = make_styles(regular, bold, italic)
    hz = Humanizer(humanize, rules)
    width = letter[0] - 2 * MARGIN_X

    story, meta = build_story(Path(src).read_text(encoding="utf-8"), st, hz, width)
    if not story:
        raise ValueError(f"{src}: no content found")

    title = f"{meta['name']} - Resume" if meta["name"] else Path(src).stem
    doc = SimpleDocTemplate(
        str(dst), pagesize=letter, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
        title=title, author=meta["name"], subject=meta["headline"], creator="")

    def finish(canvas, _doc):
        info = canvas._doc.info          # neutral metadata: no generator banner
        info.producer = ""
        info.creator = ""

    doc.build(story, onFirstPage=finish, onLaterPages=finish)

    if show_changes:
        for before, after in hz.changes:
            print(f"  - {before}\n  + {after}")
    return len(hz.changes)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert *resume.md to an ATS-friendly *resume.pdf")
    ap.add_argument("inputs", nargs="*", help="Markdown resume file(s). Default: *resume.md in the current folder")
    ap.add_argument("-o", "--output", help="output path (only with a single input)")
    ap.add_argument("--no-humanize", action="store_true", help="disable the humanizer")
    ap.add_argument("--rules", help="JSON file {regex: replacement} to add/override humanizer word rules")
    ap.add_argument("--show-changes", action="store_true", help="print every text change the humanizer made")
    ap.add_argument("--font-dir", help="folder containing LiberationSans TTF files")
    args = ap.parse_args(argv)

    inputs = args.inputs or sorted(p for p in glob.glob("*.md") if p.lower().endswith("resume.md"))
    if not inputs:
        ap.error("no input given and no *resume.md found in the current folder")
    if args.output and len(inputs) > 1:
        ap.error("-o/--output works with a single input only")

    rules = json.loads(Path(args.rules).read_text(encoding="utf-8")) if args.rules else None
    status = 0
    for src in inputs:
        p = Path(src)
        if not p.is_file():
            print(f"error: {src} not found", file=sys.stderr)
            status = 1
            continue
        dst = Path(args.output) if args.output else p.with_suffix(".pdf")
        n = convert(p, dst, not args.no_humanize, rules, args.font_dir, args.show_changes)
        print(f"{p} -> {dst}  (humanizer edits: {n})")
    return status


if __name__ == "__main__":
    sys.exit(main())
