from typing import Any

from fastapi import HTTPException


def error_code(exc: HTTPException) -> Any:
    # Services raise with a dict detail; starlette types `detail` as `str`.
    detail: Any = exc.detail
    return detail["code"]
