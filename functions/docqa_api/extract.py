"""Turn an uploaded file into plain text, then cut that text into passages ("chunks")."""
import io
import re

from docx import Document
from pypdf import PdfReader

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

# Each chunk is about this many characters. The Chunks.content column holds up
# to 10,000, so 1,500 leaves plenty of room while staying focused on one topic.
CHUNK_SIZE = 1500
# Neighbouring chunks share this many characters, so a sentence that falls on
# a boundary still appears whole in at least one chunk.
CHUNK_OVERLAP = 200


def get_extension(file_name):
    """'Report.PDF' -> '.pdf'"""
    dot = file_name.rfind(".")
    return file_name[dot:].lower() if dot != -1 else ""


def extract_text(file_name, data):
    """Return the text inside the file. `data` is the file's raw bytes."""
    ext = get_extension(file_name)

    if ext == ".pdf":
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)

    if ext == ".docx":
        doc = Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in doc.paragraphs)

    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")

    raise ValueError(f"Unsupported file type: {ext}")


def clean_text(text):
    """Squash runs of spaces and blank lines that PDFs often produce."""
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text):
    """Split text into overlapping chunks of about CHUNK_SIZE characters,
    preferring to cut at a paragraph or sentence end rather than mid-word."""
    text = clean_text(text)
    chunks = []
    start = 0

    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))

        if end < len(text):
            # Look back from `end` for a natural break in the second half of the chunk.
            window = text[start:end]
            for separator in ("\n\n", ". ", "\n", " "):
                cut = window.rfind(separator)
                if cut > CHUNK_SIZE // 2:
                    end = start + cut + len(separator)
                    break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP

    return chunks
