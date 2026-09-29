"""Text out of attachment files: PDF, Word, PowerPoint, Excel, HWPX, notebooks and plain text. Pure functions.

Office and HWPX files are zips of XML; their text is picked out with regular expressions (no XML parser, so no
entity expansion), and every part read is size-capped. Nothing here runs code from the file.
"""

import html
import io
import json
import re
import zipfile
from pathlib import PurePosixPath

from .parse import html_to_text

MAX_CHARS = 2_000_000  # extracted text kept per file
MAX_PART = 30 * 1024 * 1024  # largest XML part read out of a zip
MAX_PAGES = 300
MAX_ROWS = 2000  # per spreadsheet sheet
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
_IMAGE_EXTS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
_TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".py", ".c", ".h", ".cc", ".cpp", ".hpp", ".java",
              ".js", ".ts", ".sql", ".r", ".m", ".tex", ".log", ".yaml", ".yml", ".sh", ".rs", ".go", ".kt"}


def image_format(name: str | None, content_type: str | None) -> str | None:
    """'png', 'jpeg', ... for images an assistant can look at, else None."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    return IMAGE_TYPES.get(ctype) or _IMAGE_EXTS.get(PurePosixPath((name or "").lower()).suffix)


def extract_text(data: bytes, name: str | None, content_type: str | None) -> tuple[str, str]:
    """(text, how): how names the reader used (pdf, docx, text, ...) or why there is no text
    (image, unsupported: ..., unreadable: ...)."""
    ext = PurePosixPath((name or "").lower()).suffix
    ctype = (content_type or "").split(";")[0].strip().lower()
    if image_format(name, content_type):
        return "", "image"
    zipped = {".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx, ".hwpx": _hwpx}
    try:
        if ext == ".pdf" or ctype == "application/pdf":
            text, how = _pdf(data), "pdf"
            if not re.sub(r"--- page \d+ ---", "", text).strip():
                return "", "pdf without a text layer (probably scanned)"
        elif ext in zipped and zipfile.is_zipfile(io.BytesIO(data)):
            text, how = zipped[ext](data), ext[1:]
        elif ext == ".ipynb":
            text, how = _ipynb(data), "notebook"
        elif ext in (".html", ".htm") or ctype == "text/html":
            text, how = html_to_text(_decode(data)), "html"
        elif ext in _TEXT_EXTS or ctype.startswith("text/") or ctype == "application/json":
            text, how = _decode(data), "text"
        elif ext == ".hwp":
            return "", "unsupported: HWP (binary Hangul); an HWPX or PDF copy can be read"
        else:
            return "", f"unsupported: {ext or ctype or 'unknown type'}"
    except Exception as exc:  # a damaged or unusual file: report it instead of failing the request
        return "", f"unreadable: {type(exc).__name__}"
    return text[:MAX_CHARS], how


def _decode(data: bytes) -> str:
    """UTF-8, UTF-16 with a BOM, or CP949 (Korean Windows), in that order."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("password-protected PDF")
    pages = []
    for number, page in enumerate(reader.pages, 1):
        if number > MAX_PAGES:
            pages.append(f"[stopped after {MAX_PAGES} pages]")
            break
        pages.append(f"--- page {number} ---\n{(page.extract_text() or '').strip()}")
    return "\n\n".join(pages)


def _part(zf: zipfile.ZipFile, name: str) -> str:
    if zf.getinfo(name).file_size > MAX_PART:
        raise ValueError(f"{name} is too large")
    return zf.read(name).decode("utf-8", "replace")


def _numbered(zf: zipfile.ZipFile, pattern: str) -> list[tuple[int, str]]:
    """Zip members matching pattern (with one number group), in number order: slide2 before slide10."""
    return sorted((int(m.group(1)), n) for n in zf.namelist() if (m := re.fullmatch(pattern, n)))


def _paragraphs(xml: str, para_end: str, text_tag: str) -> list[str]:
    """The text runs of each paragraph, joined; empty paragraphs dropped."""
    lines = []
    for chunk in xml.split(para_end):
        line = html.unescape("".join(re.findall(rf"<{text_tag}(?:\s[^>]*)?>([^<]*)</{text_tag}>", chunk))).strip()
        if line:
            lines.append(line)
    return lines


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = _part(zf, "word/document.xml")
    xml = re.sub(r"<w:tab/>", "<w:t>\t</w:t>", xml)
    xml = re.sub(r"<w:(?:br|cr)\b[^>]*/>", "<w:t>\n</w:t>", xml)
    return "\n".join(_paragraphs(xml, "</w:p>", "w:t"))


def _pptx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        slides = [(number, _paragraphs(_part(zf, name), "</a:p>", "a:t"))
                  for number, name in _numbered(zf, r"ppt/slides/slide(\d+)\.xml")]
    return "\n\n".join(f"--- slide {number} ---\n" + "\n".join(lines) for number, lines in slides)


def _column(ref: str) -> int:
    """'C7' -> 2."""
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return max(n - 1, 0)


def _xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            shared = [html.unescape("".join(re.findall(r"<t(?:\s[^>]*)?>([^<]*)</t>", si)))
                      for si in re.findall(r"<si>(.*?)</si>", _part(zf, "xl/sharedStrings.xml"), re.S)]
        out = []
        for number, name in _numbered(zf, r"xl/worksheets/sheet(\d+)\.xml"):
            rows = []
            for row in re.findall(r"<row\b[^>]*>(.*?)</row>", _part(zf, name), re.S)[:MAX_ROWS]:
                cells: dict[int, str] = {}
                for attrs, body in re.findall(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
                    kind = re.search(r'\bt="(\w+)"', attrs)
                    value = re.search(r"<v>([^<]*)</v>", body)
                    if kind and kind.group(1) == "inlineStr":
                        text = "".join(re.findall(r"<t(?:\s[^>]*)?>([^<]*)</t>", body))
                    elif value and kind and kind.group(1) == "s":
                        text = shared[int(value.group(1))] if int(value.group(1)) < len(shared) else ""
                    else:
                        text = value.group(1) if value else ""
                    ref = re.search(r'\br="([A-Z]+)\d+"', attrs)
                    column = _column(ref.group(1)) if ref else max(cells, default=-1) + 1
                    if text:
                        cells[column] = html.unescape(text)
                if cells:
                    rows.append("\t".join(cells.get(i, "") for i in range(min(max(cells) + 1, 200))))
            out.append(f"--- sheet {number} ---\n" + "\n".join(rows))
    return "\n\n".join(out)


def _hwpx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        sections = [_part(zf, name) for _, name in _numbered(zf, r"Contents/section(\d+)\.xml")]
    lines = []
    for xml in sections:
        xml = re.sub(r"<hp:tab\b[^>]*/>", "\t", xml)
        xml = re.sub(r"<hp:lineBreak\b[^>]*/>", "\n", xml)
        xml = re.sub(r"<hp:(?:fwSpace|nbSpace|hyphen)\b[^>]*/>", " ", xml)
        lines += _paragraphs(xml, "</hp:p>", "hp:t")
    return "\n".join(lines)


def _ipynb(data: bytes) -> str:
    cells = []
    for cell in json.loads(_decode(data)).get("cells") or []:
        source = cell.get("source")
        text = "".join(source) if isinstance(source, list) else str(source or "")
        cells.append(f"--- {cell.get('cell_type', 'cell')} cell ---\n{text.strip()}")
    return "\n\n".join(cells)
