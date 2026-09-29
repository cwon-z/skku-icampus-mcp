"""Attachment text extraction against small synthetic files built here (no real course files)."""

import io
import json
import zipfile

from icampus.extract import extract_text, image_format


def zipped(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in parts.items():
            zf.writestr(name, text)
    return buf.getvalue()


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF showing `text` in Helvetica, written by hand."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >>"
               b" /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_pdf():
    text, how = extract_text(tiny_pdf("Homework 3 is due Friday"), "hw3.pdf", "application/pdf")
    assert how == "pdf" and text == "--- page 1 ---\nHomework 3 is due Friday"
    blank = tiny_pdf("").replace(b"() Tj", b"")
    assert extract_text(blank, "scan.pdf", None) == ("", "pdf without a text layer (probably scanned)")
    assert extract_text(b"%PDF-1.4 broken", "x.pdf", None)[1].startswith("unreadable: ")


def test_docx():
    doc = ('<w:document><w:body><w:p><w:r><w:t>제출 양식</w:t></w:r></w:p>'
           '<w:p><w:r><w:t xml:space="preserve">Name: </w:t><w:tab/><w:t>&lt;학번&gt;</w:t></w:r></w:p>'
           '<w:p></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
           '</w:body></w:document>')
    assert extract_text(zipped({"word/document.xml": doc}), "form.docx", None) == \
        ("제출 양식\nName: \t<학번>\ncell", "docx")


def test_pptx_slides_in_number_order():
    slide = '<p:sld><a:p><a:r><a:t>{}</a:t></a:r></a:p><a:p><a:r><a:t>point</a:t></a:r></a:p></p:sld>'
    data = zipped({"ppt/slides/slide10.xml": slide.format("Ten"), "ppt/slides/slide2.xml": slide.format("Two")})
    text, how = extract_text(data, "week5.pptx", None)
    assert how == "pptx" and text == "--- slide 2 ---\nTwo\npoint\n\n--- slide 10 ---\nTen\npoint"


def test_xlsx():
    shared = "<sst><si><t>name</t></si><si><t>score</t></si><si><r><t>Kim</t></r></si></sst>"
    sheet = ('<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>9.5</v></c></row>'
             '<row r="3"><c r="B3" t="inlineStr"><is><t>inline</t></is></c><c r="C3" s="1"/></row>'
             '</sheetData></worksheet>')
    data = zipped({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})
    assert extract_text(data, "grades.xlsx", None) == \
        ("--- sheet 1 ---\nname\tscore\nKim\t\t9.5\n\tinline", "xlsx")


def test_hwpx():
    section = ('<hs:sec><hp:p><hp:run><hp:t>성균관 탐방</hp:t></hp:run></hp:p>'
               '<hp:p><hp:run><hp:t>감상문<hp:tab width="4000"/>제출</hp:t></hp:run></hp:p></hs:sec>')
    assert extract_text(zipped({"Contents/section0.xml": section}), "안내.hwpx", None) == \
        ("성균관 탐방\n감상문\t제출", "hwpx")


def test_text_notebook_and_encodings():
    assert extract_text("주차별 과제".encode("cp949"), "readme.txt", None) == ("주차별 과제", "text")
    assert extract_text("a,b\n1,2".encode("utf-8-sig"), "data.csv", "text/csv") == ("a,b\n1,2", "text")
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Lab 1\n", "Load iris"]},
                    {"cell_type": "code", "source": "import pandas as pd", "outputs": [{"text": "noise"}]}]}
    assert extract_text(json.dumps(nb).encode(), "lab1.ipynb", None) == \
        ("--- markdown cell ---\n# Lab 1\nLoad iris\n\n--- code cell ---\nimport pandas as pd", "notebook")
    assert extract_text(b"<p>Hello<br>there</p>", "page.html", None) == ("Hello\nthere", "html")


def test_images_and_unsupported():
    assert image_format("diagram.PNG", None) == "png" and image_format(None, "image/jpeg; q=1") == "jpeg"
    assert image_format("a.pdf", "application/pdf") is None
    assert extract_text(b"\x89PNG", "diagram.png", "image/png") == ("", "image")
    assert extract_text(b"\xd0\xcf\x11\xe0", "old.hwp", None)[1].startswith("unsupported: HWP")
    assert extract_text(b"PK\x03\x04", "archive.zip", "application/zip") == ("", "unsupported: .zip")
    assert extract_text(b"not a zip", "broken.docx", None) == ("", "unsupported: .docx")
    assert extract_text(zipped({"other.xml": "<x/>"}), "empty.docx", None) == ("", "unreadable: KeyError")
