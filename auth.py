import os
import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Header, HTTPException

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set. Add it to .env "
        "(any long random string, e.g. `python -c \"import secrets;"
        "print(secrets.token_hex(32))\"`)."
    )

ALGORITHM = "HS256"
AUTH_TOKEN_EXPIRE_DAYS = 7

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False  # malformed hash in DB -> treat as failed login


def validate_credentials(email: str, password: str) -> str:
    email = (email or "").strip().lower()
    password = password or ""
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    return email


def create_token(email: str) -> str:
    payload = {
        "sub": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=AUTH_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token.")
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid session token.")
    return email


def get_current_user(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not logged in.")
    email = decode_token(authorization.removeprefix("Bearer ").strip())

    from db import get_db  # local import avoids a circular import at load time
    if not get_db().users.find_one({"email": email}):
        raise HTTPException(status_code=401, detail="Account no longer exists.")
    return email
