"""Safe HTTP errors matching the authored contract."""

from fastapi.responses import JSONResponse


def error_response(status: int, code: str, message: str, *, retryable: bool = False) -> JSONResponse:
    headers = {"Retry-After": "5"} if status == 503 else None
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
        headers=headers,
    )
