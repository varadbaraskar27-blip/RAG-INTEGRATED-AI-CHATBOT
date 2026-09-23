# 📚 Multi-User RAG Chatbot

A retrieval-augmented generation chatbot where every user **uploads their own
documents** (PDF/TXT/MD, unlimited count) and chats against them. Answers come
strictly from the selected documents; anything else falls back to general
knowledge behind a clearly-marked warning. Users can log in from any browser,
see their previous uploads, and give the bot **custom instructions**
("reply in Hinglish", "explain thoroughly for exam prep") that shape every reply.

| Layer | What it does |
|---|---|
| **Auth** | Email + password signup/login, bcrypt-hashed passwords, JWT sessions |
| **Storage** | MongoDB Atlas: users + document metadata + custom instructions, all tied to the user's email |
| **Vectors** | One FAISS folder per uploaded document on disk (`data/vectors/<doc_id>/`) |
| **Chat** | Retrieves the top-4 chunks across the documents you select, then asks Groq to answer only from them |
| **UI** | Plain HTML/CSS/JS — animated login page, sidebar document library, settings slide-over. No npm, no build step |

## Architecture

```
        ┌──────────── AUTH ────────────┐
 signup │ email + password             │
 ─────> │ bcrypt hash → users (Mongo)  │ ──> JWT signed with JWT_SECRET
 login  │ verify hash                  │     (browser stores it, sends
 ─────> └──────────────────────────────┘      "Authorization: Bearer …")

        ┌──────────── UPLOAD (POST /api/documents) ─────────────────┐
 file ─>│ ingest.py: extract → 500-word chunks (50 overlap)         │
        │ → sentence-transformers embeddings (local, free)          │
        │ → data/vectors/<doc_id>/index.faiss + chunks.json         │
        │ → documents (Mongo): { email, doc_id, filename,           │
        │        upload_date, chunks, vector_data_reference }       │
        └───────────────────────────────────────────────────────────┘
          every row/filter is scoped by EMAIL → per-user isolation

        ┌──────────── CHAT (POST /chat) ────────────────────────────┐
        │ question + selected doc_ids (must belong to the user)     │
        │  → embed → FAISS top-4 chunks merged across selected docs │
        │  → Groq (openai/gpt-oss-20b) with:                        │
        │      strict grounding prompt + user's custom instructions │
        │      ├─ answer found  → grounded ✅ + file/page sources   │
        │      └─ "NOT_FOUND"   → 2nd call, no context,             │
        │                         + compact ⚠ disclaimer banner    │
        └───────────────────────────────────────────────────────────┘
```

## Setup

```bash
# 1. Create & activate a virtual environment
python -m venv venv
venv\Scripts\activate            # Windows   (source venv/bin/activate on Mac/Linux)

# 2. Install dependencies
pip install -r requirements.txt

# 3. Secrets — copy the template and fill in your values
copy .env.example .env
#    GROQ_API_KEY  → free key from https://console.groq.com/keys
#    JWT_SECRET    → any long random string:
#                    python -c "import secrets; print(secrets.token_hex(32))"
#    MONGODB_URI   → your Atlas connection string (or mongodb://localhost:27017)

# 4. Make sure MongoDB is reachable — pick ONE:
#    a) Atlas (used here): cluster reachable + your IP allowed under
#       Atlas -> Network Access
#    b) Local: install "MongoDB Community Server" (mongod runs as a service)

# 5. Quick sanity check of the Mongo credentials
venv\Scripts\python -c "from db import ping; ping(); print('Mongo OK')"

# 6. Start the server
python run.py                    # works from any folder (or: uvicorn main:app --reload)
```

Open **http://127.0.0.1:8000**, sign up, click **＋ Upload document**, tick it
in the sidebar, and chat. The **⚙️ gear** opens custom instructions.

> `python ingest.py your_file.pdf` still ingests from the CLI (writes a vector
> folder without touching Mongo) — handy for quick experiments.

## Where is what stored?

| Data | Where | Why |
|---|---|---|
| Users (`email`, `hashed_password`, `created_at`, `custom_instructions`) | Mongo `users` | Auth + per-user preferences |
| Document metadata (`email`, `doc_id`, `filename`, `upload_date`, `chunks`, `vector_data_reference`) | Mongo `documents` | "My library" listing + ownership checks |
| FAISS index + chunk texts | `data/vectors/<doc_id>/` | Heavy vector data belongs on disk; Mongo keeps only a pointer |
| Uploaded raw files | `uploads/<doc_id>_<filename>` | Reproducibility / re-ingestion |
| JWT secret, Mongo URI, Groq key | `.env` | Secrets never live in source or git |

## Interview cheat-sheet: why each piece?

| Piece | Why |
|---|---|
| **bcrypt for passwords** | Passwords are never stored in readable form; bcrypt is deliberately slow + salted, so a leaked database doesn't leak logins. |
| **JWT sessions** | The server stays stateless: a signed token carries the identity + expiry, verified by signature — no session table needed. |
| **Email-scoped queries** | Isolation isn't a UI feature: EVERY Mongo lookup filters by the logged-in email, so another user's doc_id simply doesn't exist for you. |
| **Metadata vs data split** | Mongo stores a pointer (`vector_data_reference`), not the vectors or the PDF: tiny fast queries in Mongo, heavy math in FAISS, big blobs on disk — the right tool for each job. |
| **Custom instructions in the system prompt** | Same idea as ChatGPT/Claude: user style rules are injected into every LLM call, but worded so they can never override the grounding rule. |
| **Chunking (500w / 50 overlap)** | LLM context is limited; overlap keeps ideas that straddle chunk boundaries intact. |
| **all-MiniLM-L6-v2** | Small, fast, CPU-friendly, free — maps text to 384-dim vectors where similar meanings sit close together. |
| **FAISS (IndexFlatIP)** | Exact nearest-neighbour search; with normalized vectors inner product = cosine similarity. |
| **Strict system prompt + NOT_FOUND** | Makes "I don't know" machine-detectable so the backend can branch to general knowledge. |
| **.env for all secrets** | API key, JWT secret, Mongo URI never touch source code or git history. |
| **static/ served only** | Only the UI folder is exposed — mounting the project root would leak `.env` through the static mount! |

## Troubleshooting

| Error | Cause & fix |
|---|---|
| `Cannot reach MongoDB (network problem)` | Server unreachable: Atlas → Network Access must allow your IP (or 0.0.0.0/0 for a demo); locally, `mongod` must be running. |
| `MongoDB rejected the login (bad auth)` | Username/password in `MONGODB_URI` don't match Atlas → Database Access. Edit the user, set a new letters-and-numbers password, paste it into `.env`, restart. |
| `JWT_SECRET is not set` | Add it to `.env` (see Setup step 3). |
| `Error loading ASGI app. Could not import module "main"` | Run from the project folder, or just `python run.py`. |
| `400 ... Unsupported file type` | Only `.pdf`, `.txt`, `.md`. Scanned/image-only PDFs (no text layer) are rejected. |
| `502 ... Invalid API Key (401)` | Groq key in `.env` is wrong/revoked — get a fresh one at console.groq.com/keys. |
| UI looks unstyled / stuck | Hard-refresh (Ctrl+F5) — old cached assets. |
| Logged out suddenly | JWT expired (7 days) — just log in again. |

| File | Purpose |
|---|---|
| `main.py` | FastAPI: auth, documents, preferences, `/chat`, static serving |
| `auth.py` | bcrypt hashing, JWT create/verify, `get_current_user` dependency |
| `db.py` | MongoDB connection, collections, indexes, health check |
| `ingest.py` | Document → chunks → embeddings → per-document FAISS folder |
| `run.py` | Launcher that works from any folder |
| `static/index.html`, `style.css`, `script.js` | Plain HTML/CSS/JS UI (auth page, app, settings) |
| `data/vectors/<doc_id>/` | Generated vector DB, one folder per document (auto-created on upload) |
| `uploads/` | Raw uploaded files (auto-created on upload) |
| `.env` | Your secrets — never committed (see `.env.example`) |
