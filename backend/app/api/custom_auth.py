import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app.core.auth import get_jwt_strategy, get_user_manager
from app.core.redis import get_redis

router = APIRouter()

TEMP_TOKEN_TTL = 300  # 5 minutes


@router.post("/login")
async def login(
    credentials: OAuth2PasswordRequestForm = Depends(),
    user_manager=Depends(get_user_manager),
):
    user = await user_manager.authenticate(credentials)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="LOGIN_BAD_CREDENTIALS")

    if user.is_2fa_enabled and user.totp_secret:
        # Store temp token in Redis, return 2FA challenge
        r = await get_redis()
        temp_token = secrets.token_urlsafe(32)
        await r.set(f"2fa_temp:{temp_token}", str(user.id), ex=TEMP_TOKEN_TTL)
        return {"requires_2fa": True, "temp_token": temp_token}

    # Normal login — generate JWT
    strategy = get_jwt_strategy()
    token = await strategy.write_token(user)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
async def logout():
    return {"detail": "Logged out"}
