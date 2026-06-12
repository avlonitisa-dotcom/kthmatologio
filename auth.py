"""
Simple HTTP Basic Auth middleware for the web UI.
Set APP_USERNAME and APP_PASSWORD in environment / .env.
Without these set, access is denied entirely.
"""
import os
import secrets
from base64 import b64decode

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_USERNAME = os.getenv("APP_USERNAME", "")
_PASSWORD = os.getenv("APP_PASSWORD", "")

# Paths that don't require auth (static assets loaded by the HTML itself)
_PUBLIC_PREFIXES = ("/static/",)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for purely static sub-resources
        for prefix in _PUBLIC_PREFIXES:
            if request.url.path.startswith(prefix):
                return await call_next(request)

        if not _USERNAME or not _PASSWORD:
            return Response(
                "APP_USERNAME / APP_PASSWORD not configured.",
                status_code=503,
            )

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = b64decode(auth_header[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                user_ok = secrets.compare_digest(username, _USERNAME)
                pass_ok = secrets.compare_digest(password, _PASSWORD)
                if user_ok and pass_ok:
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            "Απαιτείται πιστοποίηση",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="TEE KAEK App"'},
        )
