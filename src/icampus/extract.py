"""Text out of attachment files: PDF, Word, PowerPoint, Excel, HWPX, notebooks and plain text.

Files come from course staff, so treat them as hostile. Office and HWPX files are zips of XML: they are unpacked
within one size budget (no bzip2/LZMA parts, whose inflation can't be bounded) and read with a single linear
tokenizer, so broken markup can't make a regular expression backtrack. files.py runs extract_text in a child
process (`python -m icampus.extract`) with a memory cap and a time limit on top.
"""

import html
import io
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

from .parse import html_to_text

MAX_CHARS = 2_000_000  # extracted text kept per file
MAX_INFLATED = 40 * 2**20  # bytes unpacked from one Office/HWPX zip, all parts together
MAX_PAGES = 300
MAX_ROWS = 2000  # per spreadsheet sheet
MAX_COLUMNS = 200
MEMORY_LIMIT = 768 * 2**20  # address space of the extraction process
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
_IMAGE_EXTS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
_TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".py", ".c", ".h", ".cc", ".cpp", ".hpp", ".java",
              ".js", ".ts", ".sql", ".r", ".m", ".tex", ".log", ".yaml", ".yml", ".sh", ".rs", ".go", ".kt"}
# One tag or one run of text. [^<>] stops every attempt at the next tag, which keeps the scan linear.
_TOKEN = re.compile(r"<(/?)([A-Za-z_][\w:.-]*)([^<>]*)>|([^<]+)")


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
            text, how = zipped[ext](_Zip(data)), ext[1:]
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
    except Exception as exc:  # damaged, hostile or just unusual: report it instead of failing the request
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
    pages, size = [], 0
    for number, page in enumerate(reader.pages, 1):
        if number > MAX_PAGES or size > MAX_CHARS:
            pages.append("[stopped here: the file is too long]")
            break
        pages.append(f"--- page {number} ---\n{(page.extract_text() or '').strip()}")
        size += len(pages[-1])
    return "\n\n".join(pages)


class _Zip:
    """The parts of an Office/HWPX file, unpacked within MAX_INFLATED bytes in all."""

    def __init__(self, data: bytes):
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.left = MAX_INFLATED

    def numbered(self, pattern: str) -> list[str]:
        """Parts matching pattern (with one number group), in number order: slide2 before slide10."""
        found = ((int(m.group(1)), n) for n in self.zip.namelist() if (m := re.fullmatch(pattern, n)))
        return [name for _, name in sorted(found)]

    def has(self, name: str) -> bool:
        return name in self.zip.namelist()

    def read(self, name: str) -> str:
        info = self.zip.getinfo(name)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError(f"{name}: compression that can't be unpacked safely")
        with self.zip.open(info) as part:
            data = part.read(self.left + 1)  # deflate output is produced as it is read: never more than this
        if len(data) > self.left:
            raise ValueError("the file unpacks to too much data")
        self.left -= len(data)
        return data.decode("utf-8", "replace")


class _Lines:
    """Lines of text, collected up to MAX_CHARS."""

    def __init__(self):
        self.lines: list[str] = []
        self.size = 0

    @property
    def full(self) -> bool:
        return self.size >= MAX_CHARS

    def add(self, line: str, keep_indent: bool = False) -> None:
        """keep_indent keeps leading tabs: a row's empty first cells still take their columns."""
        line = line.rstrip() if keep_indent else line.strip()
        if line.strip() and not self.full:
            self.lines.append(line)
            self.size += len(line) + 1

    def text(self) -> str:
        return "\n".join(self.lines)


def _tokens(xml: str):
    """(kind, name, attrs, text) per tag or text run, kind being open, close, empty or text."""
    for m in _TOKEN.finditer(xml):
        closing, name, attrs, text = m.groups()
        if text is not None:
            yield "text", "", "", text
        else:
            yield ("close" if closing else "empty" if attrs.endswith("/") else "open"), name, attrs, ""


def _paragraphs(xml: str, lines: _Lines, para: str, run: str, inline: dict[str, str], skip: str = "") -> None:
    """Adds one line per <para>: the text of its <run> elements, with inline tags (tabs, breaks) put in.
    Everything inside <skip> (e.g. paragraph properties, which list tab stops) is ignored."""
    parts: list[str] = []
    inside = skipping = False
    for kind, name, _, text in _tokens(xml):
        if lines.full:
            return
        if name == skip and skip:
            skipping = kind == "open"
        elif skipping:
            continue
        elif kind == "text":
            if inside:
                parts.append(text)
        elif name == run:
            inside = kind == "open"
        elif name in inline and kind != "close":
            parts.append(inline[name])
        elif name == para and kind == "close":
            lines.add(html.unescape("".join(parts)))
            parts = []
    lines.add(html.unescape("".join(parts)))


def _docx(z: _Zip) -> str:
    lines = _Lines()
    _paragraphs(z.read("word/document.xml"), lines, "w:p", "w:t", {"w:tab": "\t", "w:br": "\n", "w:cr": "\n"},
                skip="w:pPr")
    return lines.text()


def _pptx(z: _Zip) -> str:
    slides = []
    for name in z.numbered(r"ppt/slides/slide(\d+)\.xml"):
        lines = _Lines()
        _paragraphs(z.read(name), lines, "a:p", "a:t", {"a:br": "\n"})
        slides.append(f"--- slide {re.search(r'(\d+)', name.rsplit('/', 1)[1]).group(1)} ---\n{lines.text()}")
    return "\n\n".join(slides)


def _hwpx(z: _Zip) -> str:
    lines = _Lines()
    for name in z.numbered(r"Contents/section(\d+)\.xml"):
        _paragraphs(z.read(name), lines, "hp:p", "hp:t", {"hp:tab": "\t", "hp:lineBreak": "\n", "hp:fwSpace": " ",
                                                          "hp:nbSpace": " ", "hp:hyphen": "-"})
    return lines.text()


def _column(letters: str) -> int:
    """'C' -> 2."""
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _shared_strings(xml: str) -> list[str]:
    strings: list[str] = []
    parts: list[str] = []
    inside = phonetic = False
    for kind, name, _, text in _tokens(xml):
        if kind == "text":
            if inside and not phonetic:
                parts.append(text)
        elif name == "t":
            inside = kind == "open"
        elif name == "rPh":  # phonetic guide runs repeat the text
            phonetic = kind == "open"
        elif name == "si" and kind != "open":
            strings.append(html.unescape("".join(parts)))
            parts = []
    return strings


def _sheet(xml: str, shared: list[str], lines: _Lines) -> None:
    """Adds one tab-separated line per non-empty row, cells placed by their column letters."""
    cells: dict[int, str] = {}
    parts: list[str] = []
    rows, kind_of_cell, column, in_value = 0, "", 0, False
    for kind, name, attrs, text in _tokens(xml):
        if lines.full or rows >= MAX_ROWS:
            return
        if kind == "text":
            if in_value:
                parts.append(text)
        elif name == "c":
            if kind != "close":
                t, r = re.search(r'\bt="(\w+)"', attrs), re.search(r'\br="([A-Z]{1,3})\d+"', attrs)
                kind_of_cell = t.group(1) if t else ""
                column = _column(r.group(1)) if r else max(cells, default=-1) + 1
                parts = []
            if kind != "open":
                value = html.unescape("".join(parts))
                if kind_of_cell == "s":
                    value = shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
                if value and column < MAX_COLUMNS:
                    cells[column] = value
        elif name in ("v", "t"):  # a value, or the text of an inline string
            in_value = kind == "open"
        elif name == "row" and kind != "open":
            if cells:
                lines.add("\t".join(cells.get(i, "") for i in range(max(cells) + 1)), keep_indent=True)
                rows += 1
            cells = {}


def _xlsx(z: _Zip) -> str:
    shared = _shared_strings(z.read("xl/sharedStrings.xml")) if z.has("xl/sharedStrings.xml") else []
    sheets = []
    for name in z.numbered(r"xl/worksheets/sheet(\d+)\.xml"):
        lines = _Lines()
        _sheet(z.read(name), shared, lines)
        sheets.append(f"--- sheet {re.search(r'(\d+)', name.rsplit('/', 1)[1]).group(1)} ---\n{lines.text()}")
    return "\n\n".join(sheets)


def _ipynb(data: bytes) -> str:
    cells, size = [], 0
    for cell in json.loads(_decode(data)).get("cells") or []:
        source = cell.get("source")
        text = "".join(source) if isinstance(source, list) else str(source or "")
        cells.append(f"--- {cell.get('cell_type', 'cell')} cell ---\n{text.strip()}")
        size += len(cells[-1])
        if size > MAX_CHARS:
            break
    return "\n\n".join(cells)


def main() -> None:
    """python -m icampus.extract FILE NAME CONTENT_TYPE prints {"text", "how"} as JSON (ASCII) on stdout.
    files.py runs this as a child process; the memory cap is set before the file is read."""
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    path, name, content_type = sys.argv[1:4]
    text, how = extract_text(Path(path).read_bytes(), name or None, content_type or None)
    sys.stdout.write(json.dumps({"text": text, "how": how}))


if __name__ == "__main__":
    main()
