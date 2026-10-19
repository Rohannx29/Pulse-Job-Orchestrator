import hashlib
import json
import base64
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from jose import jwt, JWTError
except ImportError:
    class JWTError(Exception):
        pass

    class _FallbackJWT:
        @staticmethod
        def encode(payload: dict, key: str, algorithm: str = "HS256") -> str:
            header = {"alg": "HS256", "typ": "JWT"}
            p = {}
            for k, v in payload.items():
                if isinstance(v, datetime):
                    if v.tzinfo is None:
                        v = v.replace(tzinfo=timezone.utc)
                    p[k] = int(v.timestamp())
                else:
                    p[k] = v
            header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
            payload_b64 = base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
            signing_input = f"{header_b64}.{payload_b64}".encode()
            sig = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
            sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
            return f"{header_b64}.{payload_b64}.{sig_b64}"

        @staticmethod
        def decode(token: str, key: str, algorithms: list = None) -> dict:
            parts = token.split(".")
            if len(parts) != 3:
                raise JWTError("Invalid token format")
            header_b64, payload_b64, sig_b64 = parts
            signing_input = f"{header_b64}.{payload_b64}".encode()
            expected_sig = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
            padded_sig = sig_b64 + "=" * (-len(sig_b64) % 4)
            try:
                actual_sig = base64.urlsafe_b64decode(padded_sig.encode())
            except Exception:
                raise JWTError("Malformed base64 signature")
            if not hmac.compare_digest(expected_sig, actual_sig):
                raise JWTError("Signature mismatch")
            padded_payload = payload_b64 + "=" * (-len(payload_b64) % 4)
            try:
                payload_data = json.loads(base64.urlsafe_b64decode(padded_payload.encode()).decode())
            except Exception:
                raise JWTError("Malformed base64 payload")
            if "exp" in payload_data:
                now_ts = int(datetime.now(timezone.utc).timestamp())
                if payload_data["exp"] < now_ts:
                    raise JWTError("Token expired")
            return payload_data

    jwt = _FallbackJWT()

from app.config import settings
from app.timeutils import utcnow

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def hash_password(password: str) -> str:
        return pwd_context.hash(password)

    def verify_password(plain: str, hashed: str) -> bool:
        return pwd_context.verify(plain, hashed)
except ImportError:
    def hash_password(password: str) -> str:
        salt = secrets.token_hex(8)
        digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        return f"sha256${salt}${digest}"

    def verify_password(plain: str, hashed: str) -> bool:
        if hashed.startswith("sha256$"):
            parts = hashed.split("$")
            if len(parts) == 3:
                _, salt, digest = parts
                expected = hashlib.sha256(f"{salt}:{plain}".encode()).hexdigest()
                return hmac.compare_digest(expected, digest)
        return False



def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {"sub": subject, "exp": expire, "type": "access"}
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def create_refresh_token() -> str:
    """Return a cryptographically random, opaque refresh token.

    The value is only ever sent in an httpOnly cookie. The database stores a
    SHA-256 digest, so a database leak cannot be used directly as a session.
    """
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
