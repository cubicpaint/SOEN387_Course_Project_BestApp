"""Safe HTTP errors matching the authored contract."""

from fastapi.responses import JSONResponse


def error_response(status: int, code: str, message: str, *, retryable: bool = False) -> JSONResponse:
    headers = {}
    if status == 503:
        headers["Retry-After"] = "5"
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
        headers=headers,
    )
