"""Turn an uploaded lease into numbered segments, and check quotes against them.

Segments are the unit of citation: the agent must point every value at the
segment it came from, and `locate_quote` verifies the quoted words are really
there. A value whose quote cannot be found is shown to the reviewer as unverified.
"""
import io
import re
import zipfile
from html import unescape

_QUOTE_MARKS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_SEGMENT_LINE = re.compile(r"^\[(s\d+)\] (.*)$", re.M)


def extract_segments(filename: str, data: bytes) -> list[dict]:
    """Split a lease into [{"id": "s1", "page": 1 or None, "text": "..."}].

    Raises ValueError for unsupported formats and documents with no text layer.
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension == "pdf":
        blocks = _pdf_blocks(data)
    elif extension == "docx":
        blocks = [(None, text) for text in _docx_paragraphs(data)]
    elif extension in ("txt", "md"):
        blocks = [(None, text) for text in re.split(r"\n\s*\n", data.decode("utf-8", errors="replace"))]
    else:
        raise ValueError("Unsupported lease format. Upload a PDF, DOCX or TXT file.")
    cleaned = [(page, " ".join(text.split())) for page, text in blocks]
    segments = [{"id": f"s{i}", "page": page, "text": text} for i, (page, text) in enumerate(((p, t) for p, t in cleaned if t), 1)]
    if not segments:
        raise ValueError("No text found in the document. Scanned leases need OCR, which this build does not do.")
    return segments


def _pdf_blocks(data: bytes) -> list[tuple[int, str]]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
    except PyPdfError as exc:
        raise ValueError(f"Could not read the PDF: {exc}") from None
    return [(number, block) for number, text in enumerate(pages, 1) for block in re.split(r"\n\s*\n", text)]


def _docx_paragraphs(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("Could not read the DOCX file.") from None
    paragraphs = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    return [unescape("".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p, re.S))) for p in paragraphs]


def format_segments(segments: list[dict]) -> str:
    """Render segments for the model prompt, one `[id] text` line each."""
    return "\n".join(f"[{s['id']}] {s['text']}" for s in segments)


def parse_segments(prompt: str) -> list[dict]:
    """Inverse of `format_segments`. Only the stub model needs it."""
    return [{"id": seg_id, "text": text} for seg_id, text in _SEGMENT_LINE.findall(prompt)]


def _normalise(text: str) -> str:
    return " ".join(text.translate(_QUOTE_MARKS).split()).casefold()


def locate_quote(segments: list[dict], quote: str | None, cited_segment: str | None = None) -> str | None:
    """Return the id of a segment that contains `quote`, preferring the cited one.

    None means the words are not in the document. Matching ignores case,
    whitespace and curly-versus-straight quote marks, nothing else.
    """
    needle = _normalise(quote or "")
    if not needle:
        return None
    candidates = sorted(segments, key=lambda s: s["id"] != cited_segment)
    return next((s["id"] for s in candidates if needle in _normalise(s["text"])), None)
