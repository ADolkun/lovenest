import time

from fastapi import HTTPException, Request

from app.core.redis import get_redis


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int, *, per_client: bool = True):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # A per-client bucket limits what one caller can ask for. Some routes
        # need the opposite: a cap on what the deployment as a whole spends
        # against a shared upstream, where ten callers on ten addresses do ten
        # times the damage one does.
        self.per_client = per_client

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        scope = client_ip if self.per_client else "all"
        key = f"rate_limit:{request.url.path}:{scope}"

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
# A trace fans out into hundreds of requests against a public chain node whose
# quota belongs to the deployment, not to the caller. The bucket is therefore
# deployment-wide: a per-client one would let N callers spend N times the
# quota, which is the starvation it exists to prevent.
onchain_trace_rate_limit = RateLimiter(max_requests=10, window_seconds=60, per_client=False)
