import base64, hashlib, hmac, json, secrets, time
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User
bearer = HTTPBearer(auto_error=False)
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 210_000).hex()
    return salt + "$" + digest
def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
        actual = hash_password(password, salt).split("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except ValueError:
        return False
def create_access_token(user: User) -> str:
    payload = {"sub": user.username, "role": user.role, "exp": int(time.time()) + settings.token_hours * 3600}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature
def decode_access_token(token: str) -> dict:
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected): raise ValueError("signature")
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
        if int(payload.get("exp", 0)) < int(time.time()): raise ValueError("expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token") from exc
def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if credentials is None: raise HTTPException(status_code=401, detail="Sign in to access this resource")
    payload = decode_access_token(credentials.credentials)
    user = db.scalar(select(User).where(User.username == payload.get("sub"), User.active.is_(True)))
    if not user: raise HTTPException(status_code=401, detail="User is inactive or no longer exists")
    return user
def require_roles(*roles: str):
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles: raise HTTPException(status_code=403, detail="Your role is not permitted to perform this action")
        return user
    return dependency
