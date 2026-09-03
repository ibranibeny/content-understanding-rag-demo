from fastapi import Request

from app.core.errors import AppError


async def require_expected_origin(request: Request) -> None:
    """Reject a mutation unless it has exactly one configured Origin value."""
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] != request.app.state.settings.frontend_origin:
        raise AppError(
            "invalid_origin",
            403,
            "The request origin is not allowed.",
            False,
        )