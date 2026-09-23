import json
import sys
import uuid
from pathlib import Path

import faiss
import numpy as np
import pdfplumber
from fastembed import TextEmbedding

CHUNK_WORDS = 500
OVERLAP_WORDS = 50
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md"}

VECTORS_ROOT = Path("data") / "vectors"


# Returns (page_number, text) tuples; .txt/.md count as page 1.
def extract_pages(path: Path) -> list[tuple[int, str]]:
    if path.suffix.lower() == ".pdf":
        pages = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):   # 1-indexed pages
                text = (page.extract_text() or "").strip()  # None if blank page
                if text:
                    pages.append((i, text))
        return pages

    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return [(1, text)] if text else []


def split_into_chunks(text: str,
                      chunk_words: int = CHUNK_WORDS,
                      overlap: int = OVERLAP_WORDS) -> list[str]:
    words = text.split()
    if not words:
        return []

    stride = chunk_words - overlap  # words advanced per chunk
    chunks = []
    for start in range(0, len(words), stride):
        chunk = " ".join(words[start:start + chunk_words])
        chunks.append(chunk)
        if start + chunk_words >= len(words):
            break
    return chunks


def ingest_file(path: str | Path,
                out_dir: str | Path,
                model: TextEmbedding | None = None,
                quiet: bool = False,
                display_name: str | None = None) -> dict:
    doc_file = Path(path)
    if not doc_file.exists():
        raise FileNotFoundError(f"file not found: {doc_file}")
    if doc_file.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"unsupported file type '{doc_file.suffix}' — supported: "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    log = (lambda *a: None) if quiet else print
    shown_name = display_name or doc_file.name  # users see/cite the original name

    log(f"[1/4] Extracting text from {shown_name} ...")
    pages = extract_pages(doc_file)
    if not pages:
        raise ValueError(
            "no extractable text found. Is this a scanned/image-only PDF?"
        )
    log(f"   got {len(pages)} pages of text")

    log(f"[2/4] Chunking ({CHUNK_WORDS} words, {OVERLAP_WORDS}-word overlap) ...")
    all_chunks: list[dict] = []
    for page_num, text in pages:
        for chunk in split_into_chunks(text):
            all_chunks.append({
                "text": chunk,
                "page": page_num,
                "filename": shown_name,
            })
    log(f"   built {len(all_chunks)} chunks")

    # Reuse the server's loaded model when provided; CLI loads its own.
    if model is None:
        log(f"[3/4] Loading embedding model '{EMBED_MODEL}' ...")
        model = TextEmbedding(EMBED_MODEL, threads=1)

    log("[3/4] Computing embeddings ...")
    embeddings = model.embed(
        [c["text"] for c in all_chunks],
    )
    embeddings = np.asarray(list(embeddings), dtype="float32")
    # Unit length -> FAISS inner product == cosine (idempotent if already normalized)
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(out / "index.faiss"))
    (out / "chunks.json").write_text(
        json.dumps(all_chunks, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    log(f"[4/4] Saved FAISS index ({index.ntotal} vectors, dim={dim}) -> {out}/")
    return {"filename": shown_name, "pages": len(pages), "chunks": len(all_chunks)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python ingest.py <path/to/document."
                 f"{'|'.join(sorted(e.lstrip('.') for e in SUPPORTED_EXTENSIONS))}>")
    doc_id = uuid.uuid4().hex
    out_dir = VECTORS_ROOT / doc_id
    summary = ingest_file(sys.argv[1], out_dir)
    print(f"Done: {summary['filename']} -> {summary['chunks']} chunks "
          f"from {summary['pages']} page(s).")
    print(f"Vector folder: {out_dir}")
    print("(CLI ingest bypasses MongoDB — use the UI upload button for the "
          "full per-user flow.)")
