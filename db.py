import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = "rag_chatbot"

# Tests replace this with a mongomock client, so get_db() goes through the indirection.
_client: MongoClient | None = None
_indexes_ready = False


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000,  # Atlas cold starts need a few seconds
        )
    return _client


def get_db():
    return get_client()[DB_NAME]


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    db = get_db()
    db.users.create_index("email", unique=True)
    db.documents.create_index("email")
    _indexes_ready = True


def _masked_uri() -> str:
    # Mask the password so the URI is safe to print in errors.
    import re
    return re.sub(r"(://[^:@/]+):([^@]+)@", r"\1:****@", MONGODB_URI)


def ping() -> None:
    try:
        get_client().admin.command("ping")
    except OperationFailure as e:
        raise RuntimeError(
            "MongoDB rejected the login (bad auth). Fix MONGODB_URI in .env:\n"
            "  1. Use the username/password from Atlas -> Database Access.\n"
            "  2. Replace the <password> placeholder in the connection string.\n"
            "  3. If the password contains special characters like @ : / ?, "
            "URL-encode them first — e.g. urllib.parse.quote_plus('p@ss') "
            "becomes 'p%40ss'. Easiest fix: use a password with only letters "
            "and numbers.\n"
            "  4. Restart the server after editing .env."
        ) from e
    except ConnectionFailure as e:
        raise RuntimeError(
            "Cannot reach MongoDB (network problem). If using Atlas, allow "
            "your IP under Atlas -> Network Access (or 0.0.0.0/0 for a demo); "
            "if running locally, start 'mongod'. Current MONGODB_URI: "
            f"{_masked_uri()}. See README.md -> Setup."
        ) from e
