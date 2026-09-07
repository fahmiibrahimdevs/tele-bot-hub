from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from app.database import get_user_by_username, hash_password

SECRET_KEY = "tele_hub_super_secret_jwt_cookie_key_2026"
COOKIE_NAME = "tele_hub_session"

serializer = URLSafeTimedSerializer(SECRET_KEY)


def create_session_token(username: str) -> str:
    return serializer.dumps({"username": username})


def verify_session_token(token: str) -> str | None:
    try:
        data = serializer.loads(token, max_age=86400 * 7)  # 7 days
        return data.get("username")
    except (BadSignature, SignatureExpired):
        return None


async def get_current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    username = verify_session_token(token)
    if not username:
        return None
    return await get_user_by_username(username)


async def require_auth(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )
    return user
