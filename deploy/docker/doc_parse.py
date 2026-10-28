"""Turn an uploaded document into markdown.

PDF uses pypdf. DOCX uses python-docx. XLSX uses openpyxl.
HTML, ODT, and RTF are read in-process. Legacy .doc gets a best-effort text
scrape (see _doc). Legacy .xls is rejected.
"""

from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser
from typing import Optional
from xml.etree import ElementTree

MAX_PARSE_BYTES = 8 * 1024 * 1024

_UNSUPPORTED = {".xls"}


class ParseError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_document(filename: str, data: bytes) -> str:
    """Return markdown for a supported document."""
    if len(data) > MAX_PARSE_BYTES:
        raise ParseError(413, "File is too large")
    suffix = _suffix(filename)
    if suffix in _UNSUPPORTED:
        raise ParseError(400, f"{suffix} is not supported. Convert it to docx or xlsx first.")
    if suffix == ".pdf":
        return _pdf(data)
    if suffix == ".doc":
        return _doc(data)
    if suffix == ".docx":
        return _docx(data)
    if suffix in {".xlsx", ".xlsm"}:
        return _xlsx(data)
    if suffix in {".html", ".htm", ".xhtml"}:
        return html_to_markdown(data.decode("utf-8", errors="replace"))
    if suffix == ".odt":
        return _odt(data)
    if suffix == ".rtf":
        return _rtf(data)
    raise ParseError(400, f"Unsupported file type {suffix or '(none)'}")


def _suffix(filename: str) -> str:
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n\n".join(part.strip() for part in parts if part.strip())


_DOC_RUN_RE = re.compile(r"[\t\n\r\x20-\x7e\xa0-\xff]{4,}")


def _doc(data: bytes) -> str:
    """Best-effort text scrape for legacy OLE2 (.doc, Word 97-2003) files.

    This is not a real Word binary-format parser (no FIB / piece-table
    handling), just a scan for readable text runs. Word 97+ stores text
    either all-cp1252 or all-UTF16LE depending on a FIB flag we don't read,
    so both decodings are tried and whichever one is right contributes the
    real runs while the other mostly dissolves into sub-4-char noise that
    gets dropped. Good enough to recover body text from a file curl can
    fetch but a browser can't navigate to; never raises on malformed input.
    """
    stream = data
    try:
        import olefile

        ole = olefile.OleFileIO(io.BytesIO(data))
        try:
            if ole.exists("WordDocument"):
                stream = ole.openstream("WordDocument").read()
        finally:
            ole.close()
    except Exception:
        stream = data

    runs = _DOC_RUN_RE.findall(stream.decode("utf-16-le", errors="ignore"))
    runs += _DOC_RUN_RE.findall(stream.decode("cp1252", errors="ignore"))
    lines = [re.sub(r"[ \t]+", " ", run).strip() for run in runs]
    lines = [line for line in lines if len(line) >= 4]
    return "\n\n".join(lines)


def _docx(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(lines)


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    blocks = []
    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if value is None else str(value) for value in row]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        if rows:
            blocks.append(f"# {sheet.title}\n\n" + "\n".join(rows))
    workbook.close()
    return "\n\n".join(blocks)


def _odt(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        xml = archive.read("content.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ParseError(400, "ODT file is not readable") from exc
    root = ElementTree.fromstring(xml)
    lines = []
    for node in root.iter():
        if node.tag.endswith("}p") or node.tag.endswith("}h"):
            text = "".join(node.itertext()).strip()
            if text:
                lines.append(text)
    return "\n\n".join(lines)


def _rtf(data: bytes) -> str:
    text = data.decode("latin-1", errors="replace")
    text = re.sub(r"\\'[0-9a-fA-F]{2}", lambda match: bytes.fromhex(match.group(0)[2:]).decode("latin-1"), text)
    text = text.replace("\\par", "\n").replace("\\line", "\n")
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return text.strip()


class _HTMLToMarkdown(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.href: Optional[str] = None
        self.link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
            return
        if self.skip:
            return
        attr = {key: value for key, value in attrs}
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self.parts.append("\n\n" + ("#" * level) + " ")
        elif tag == "p":
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self.href = attr.get("href")
            self.link_text = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "a" and self.href is not None:
            label = "".join(self.link_text).strip() or self.href
            self.parts.append(f"[{label}]({self.href})")
            self.href = None
            self.link_text = []

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
            return
        if data.strip():
            self.parts.append(data.strip())


def html_to_markdown(html: str) -> str:
    parser = _HTMLToMarkdown()
    parser.feed(html or "")
    text = "".join(parser.parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
