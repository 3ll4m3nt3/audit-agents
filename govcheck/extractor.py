from pathlib import Path


def extract_text(filepath: Path) -> str:
    suffix = filepath.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(filepath)
    elif suffix == ".docx":
        return _extract_docx(filepath)
    elif suffix in (".md", ".txt"):
        return filepath.read_text(encoding="utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type: {suffix!r}")


def _extract_pdf(filepath: Path) -> str:
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError("pymupdf is required to extract PDF text. Install it with: pip install pymupdf")

    doc = fitz.open(filepath)
    pages = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            pages.append(text)
    doc.close()
    return "\n".join(pages)


def _extract_docx(filepath: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("python-docx is required to extract DOCX text. Install it with: pip install python-docx")

    doc = Document(filepath)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)
