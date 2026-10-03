"""Builds bot/templates/reference.docx from pandoc's default reference doc with sane styles."""
import subprocess
import sys

import docx
from docx.enum.text import WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Inches

out = sys.argv[1] if len(sys.argv) > 1 else "bot/templates/reference.docx"
open(out, "wb").write(subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"], capture_output=True, check=True).stdout)
d = docx.Document(out)


def font(style, name, size=None, bold=None, color=None):
    f = style.font
    f.name = name
    if size:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if color:
        f.color.rgb = RGBColor.from_string(color)
    rpr = style.element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rf)
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rf.set(qn(a), name)
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        if rf.get(qn(a)) is not None:
            del rf.attrib[qn(a)]


S = d.styles
BY = {s.name: s for s in S}  # python-docx's S["Heading 1"] lookup mangles pandoc's literal names


def get(name):
    if name not in BY:
        from docx.enum.style import WD_STYLE_TYPE
        BY[name] = S.add_style(name, WD_STYLE_TYPE.PARAGRAPH if name == "Source Code" else WD_STYLE_TYPE.CHARACTER)
        if name == "Source Code":
            BY[name].base_style = BY["Normal"]
    return BY[name]


for n in ("Normal", "Body Text", "First Paragraph", "Compact"):
    if n in BY:
        font(BY[n], "Calibri", 11)
font(BY["Title"], "Calibri", 22, True, "1F3864")
for n, sz in (("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 12)):
    font(BY[n], "Calibri", sz, True, "1F3864")
for n in ("Source Code", "Verbatim Char"):
    font(get(n), "Consolas", 10)
get("Source Code").paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
for sec in d.sections:
    sec.left_margin = sec.right_margin = Inches(1)
    sec.top_margin = sec.bottom_margin = Inches(0.9)
d.save(out)
print("wrote", out)
