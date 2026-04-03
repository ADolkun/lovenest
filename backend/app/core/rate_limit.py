import time

from fastapi import HTTPException, Request

from app.core.redis import get_redis


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"rate_limit:{request.url.path}:{client_ip}"

        r = await get_redis()
        now = time.time()
        window_start = now - self.window_seconds

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, self.window_seconds)
        results = await pipe.execute()

        request_count = results[1]

        if request_count >= self.max_requests:
            retry_after = self.window_seconds
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(retry_after)},
            )


login_rate_limit = RateLimiter(max_requests=5, window_seconds=60)
register_rate_limit = RateLimiter(max_requests=3, window_seconds=3600)
password_reset_rate_limit = RateLimiter(max_requests=3, window_seconds=3600)
