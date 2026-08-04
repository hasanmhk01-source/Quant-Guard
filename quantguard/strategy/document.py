"""
Document text extraction for strategy uploads.

A trader can upload a file instead of typing a description - this
extracts plain text from it, which then goes through the EXACT SAME
strategy_parser.parse() as if they'd typed that text directly. No
separate code path for "uploaded" strategies - a document is just a
different way of getting to the same plain-English description.

.txt always works (no dependencies). .pdf and .docx need optional
packages, imported lazily so the app still runs without them - same
pattern as ccxt/anthropic/etc elsewhere in this project.
"""

import io


class UnsupportedFileType(ValueError):
    pass


def extract_text(filename: str, content: bytes) -> str:
    """Returns the plain text content of a file, based on its extension.
    Raises UnsupportedFileType for anything not handled, or ImportError
    (with a clear pip-install message) if the file type is supported in
    principle but the required package isn't installed."""
    lower = filename.lower()

    if lower.endswith(".txt") or lower.endswith(".md"):
        return content.decode("utf-8", errors="replace")

    elif lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError("Reading PDF files requires: pip install pypdf")
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    elif lower.endswith(".docx"):
        try:
            import docx
        except ImportError:
            raise ImportError("Reading .docx files requires: pip install python-docx")
        document = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs)

    else:
        raise UnsupportedFileType(
            f"Unsupported file type for '{filename}' - supported: .txt, .md, .pdf, .docx"
        )
