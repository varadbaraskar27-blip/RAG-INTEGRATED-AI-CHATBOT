import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastembed import TextEmbedding
from groq import Groq
from pydantic import BaseModel

import auth
from auth import get_current_user

load_dotenv()

UPLOADS_DIR = Path("uploads")
VECTORS_ROOT = Path("data") / "vectors"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # per-file cap; document count is unlimited

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # must match ingest.py
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")  # override via .env
TOP_K = 4
MAX_INSTRUCTION_CHARS = 2000

STRICT_SYSTEM_PROMPT = (
    "Answer ONLY using the provided context. If the answer is not in the "
    "context, respond with exactly the string NOT_FOUND — nothing else."
)

DISCLAIMER = (
    "context not found in your document — AI-generated, please double-check."
)


class State:
    model = None  # TextEmbedding
    groq = None
    vectors = {}  # doc_id -> (faiss index, chunks) cache


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Loading embedding model ...")
    # threads=1 keeps ONNX memory low on 512MB hosts like Render free tier.
    state.model = TextEmbedding(EMBED_MODEL, threads=1)

    from db import ensure_indexes, ping
    ping()
    ensure_indexes()
    print(f"Connected to MongoDB. Groq model: {GROQ_MODEL}")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    if api_key.startswith("gsk_...") or "here" in api_key.lower():
        print("WARNING: GROQ_API_KEY still looks like the placeholder from "
              ".env.example — real questions will fail with 401 until you "
              "paste your actual key from https://console.groq.com/keys")
    state.groq = Groq(api_key=api_key)
    print("Ready.")
    yield


app = FastAPI(title="Multi-User RAG Chatbot", lifespan=lifespan)


class AuthRequest(BaseModel):
    email: str
    password: str


class ChatRequest(BaseModel):
    question: str
    document_ids: list[str] = []


class ChatResponse(BaseModel):
    answer: str
    grounded: bool
    source: str


class PreferencesRequest(BaseModel):
    custom_instructions: str


@app.post("/api/auth/signup")
def signup(req: AuthRequest):
    from db import get_db

    email = auth.validate_credentials(req.email, req.password)
    users = get_db().users
    if users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="This email is already registered.")

    users.insert_one({
        "email": email,
        "hashed_password": auth.hash_password(req.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "custom_instructions": "",
    })
    return {"token": auth.create_token(email), "email": email}


@app.post("/api/auth/login")
def login(req: AuthRequest):
    from db import get_db

    email = (req.email or "").strip().lower()
    user = get_db().users.find_one({"email": email})
    # Same error for unknown email and wrong password: prevents user enumeration.
    if not user or not auth.verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return {"token": auth.create_token(email), "email": email}


@app.get("/api/me")
def me(email: str = Depends(get_current_user)):
    from db import get_db
    user = get_db().users.find_one({"email": email}, {"_id": 0, "email": 1,
                                                      "created_at": 1,
                                                      "custom_instructions": 1})
    return user


def _get_owned_doc(email: str, doc_id: str) -> dict:
    # Every document lookup goes through this filter; other users' doc ids never match.
    from db import get_db
    doc = get_db().documents.find_one({"email": email, "doc_id": doc_id})
    if not doc:
        raise HTTPException(status_code=403,
                            detail="Document not found in your library.")
    return doc


@app.get("/api/documents")
def list_documents(email: str = Depends(get_current_user)):
    from db import get_db
    rows = get_db().documents.find(
        {"email": email},
        {"_id": 0, "doc_id": 1, "filename": 1, "upload_date": 1, "chunks": 1},
    ).sort("upload_date", -1)
    return list(rows)


@app.post("/api/documents")
def upload_document(file: UploadFile = File(...),
                    email: str = Depends(get_current_user)):
    from db import get_db
    from ingest import SUPPORTED_EXTENSIONS, ingest_file

    original_name = Path(file.filename or "").name
    ext = Path(original_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext or '(none)'}'. "
                   f"Accepted: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(data) / 1e6:.1f} MB) — limit is 50 MB per file.",
        )

    doc_id = uuid.uuid4().hex
    vector_ref = VECTORS_ROOT / doc_id

    UPLOADS_DIR.mkdir(exist_ok=True)
    saved_path = UPLOADS_DIR / f"{doc_id}_{original_name}"
    saved_path.write_bytes(data)

    try:
        summary = ingest_file(saved_path, vector_ref, model=state.model,
                              quiet=True, display_name=original_name)
    except ValueError as e:  # unreadable / scanned PDF
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # keep disk tidy on failure
        shutil.rmtree(vector_ref, ignore_errors=True)
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")

    doc = {
        "email": email,  # ownership marker
        "doc_id": doc_id,
        "filename": summary["filename"],
        "upload_date": datetime.now(timezone.utc).isoformat(),
        "chunks": summary["chunks"],
        "pages": summary["pages"],
        "vector_data_reference": str(vector_ref),
        "file_path": str(saved_path),
    }
    get_db().documents.insert_one(doc)
    print(f"[{email}] uploaded '{doc['filename']}' ({doc['chunks']} chunks)")

    return {k: doc[k] for k in ("doc_id", "filename", "upload_date",
                                "chunks", "pages")}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, email: str = Depends(get_current_user)):
    doc = _get_owned_doc(email, doc_id)  # 403 unless it's theirs
    from db import get_db
    get_db().documents.delete_one({"email": email, "doc_id": doc_id})
    shutil.rmtree(doc["vector_data_reference"], ignore_errors=True)
    Path(doc.get("file_path", "")).unlink(missing_ok=True)
    state.vectors.pop(doc_id, None)
    return {"deleted": doc_id}


@app.get("/api/preferences")
def get_preferences(email: str = Depends(get_current_user)):
    from db import get_db
    user = get_db().users.find_one({"email": email})
    return {"custom_instructions": user.get("custom_instructions", "")}


@app.put("/api/preferences")
def set_preferences(req: PreferencesRequest, email: str = Depends(get_current_user)):
    from db import get_db
    text = req.custom_instructions.strip()[:MAX_INSTRUCTION_CHARS]
    get_db().users.update_one({"email": email},
                              {"$set": {"custom_instructions": text}})
    return {"custom_instructions": text}


def load_doc_vectors(doc: dict):
    if doc["doc_id"] not in state.vectors:
        import faiss
        ref = Path(doc["vector_data_reference"])
        index_path, chunks_path = ref / "index.faiss", ref / "chunks.json"
        if not index_path.exists() or not chunks_path.exists():
            raise HTTPException(
                status_code=410,
                detail=f"Stored vectors for '{doc['filename']}' are missing — "
                       "please delete it and re-upload.",
            )
        index = faiss.read_index(str(index_path))
        chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
        state.vectors[doc["doc_id"]] = (index, chunks)
    return state.vectors[doc["doc_id"]]


def retrieve(question: str, docs: list[dict], k: int = TOP_K) -> list[dict]:
    import json
    import numpy as np
    q_vec = np.asarray(list(state.model.embed([question])), dtype="float32")
    # Queries must be unit length too, to match the normalized stored vectors.
    q_vec /= np.linalg.norm(q_vec)

    hits = []
    for doc in docs:
        index, chunks = load_doc_vectors(doc)
        scores, ids = index.search(q_vec, k)
        for score, idx in zip(scores[0], ids[0]):
            if idx == -1:  # FAISS returns -1 when fewer than k vectors exist
                continue
            hits.append({**chunks[idx], "score": float(score)})

    hits.sort(key=lambda h: h["score"], reverse=True)  # merge across docs
    return hits[:k]


def build_system_prompt(strict: bool, instructions: str) -> str:
    prompt = STRICT_SYSTEM_PROMPT if strict else "You are a helpful assistant."
    if instructions:
        prompt += (
            "\n\nThe user added these personal instructions — follow them in "
            "how you phrase your reply (they do NOT override the rules above):\n"
            f"{instructions}"
        )
    return prompt


def ask_groq(question: str, context: str | None, instructions: str = "") -> str:
    messages = [{"role": "system", "content": build_system_prompt(context is not None,
                                                                  instructions)}]
    user_content = f"Context:\n{context}\n\nQuestion: {question}" if context else question
    messages.append({"role": "user", "content": user_content})

    resp = state.groq.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.1 if context else 0.7,
        max_tokens=512,
    )
    return resp.choices[0].message.content.strip()


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, email: str = Depends(get_current_user)):
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")
    if not req.document_ids:
        raise HTTPException(status_code=400,
                            detail="Select at least one document to chat against.")

    from db import get_db
    docs = list(get_db().documents.find({"email": email,
                                         "doc_id": {"$in": req.document_ids}}))
    if len(docs) != len(set(req.document_ids)):
        raise HTTPException(status_code=403,
                            detail="One or more selected documents are not in your library.")

    user = get_db().users.find_one({"email": email})
    instructions = (user or {}).get("custom_instructions", "")

    hits = retrieve(question, docs)
    context = "\n\n---\n\n".join(
        f"[chunk {i + 1} | {h['filename']} | page {h['page']}]\n{h['text']}"
        for i, h in enumerate(hits)
    )

    try:
        answer = ask_groq(question, context, instructions)
    except Exception as e:  # network error, bad key, rate limit, ...
        raise HTTPException(status_code=502, detail=f"Groq API error: {e}")

    if answer == "NOT_FOUND":
        # Documents don't cover the question — retry without context.
        try:
            general = ask_groq(question, context=None, instructions=instructions)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Groq API error: {e}")
        if general == "NOT_FOUND":  # extremely rare: model refuses even ungrounded
            general = "Sorry, I couldn't generate an answer for this question."
        return ChatResponse(
            answer=f"{general}\n\n{DISCLAIMER}",
            grounded=False,
            source="general knowledge (not found in your selected documents)",
        )

    by_file: dict[str, set[int]] = {}
    for h in hits:
        by_file.setdefault(h["filename"], set()).add(h["page"])
    detail = "; ".join(
        f"{fn} (pages {', '.join(map(str, sorted(pages)))})"
        for fn, pages in sorted(by_file.items())
    )
    return ChatResponse(
        answer=answer,
        grounded=True,
        source=f"your documents — {detail}",
    )


# Only static/ is exposed; mounting the project root would leak .env.
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index_page():
    return FileResponse("static/index.html")
