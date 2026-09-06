from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

SUBSCRIPTION_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src 'self' https: data:",
        "font-src data:",
        "connect-src 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]
)

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class SubscriptionSecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, path_prefix: str):
        super().__init__(app)
        self.path_prefix = "/" + path_prefix.strip("/")

    def _in_scope(self, path: str) -> bool:
        return path == self.path_prefix or path.startswith(self.path_prefix + "/")

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if self._in_scope(request.url.path):
            for key, value in BASE_HEADERS.items():
                response.headers.setdefault(key, value)
            response.headers.setdefault("Content-Security-Policy", SUBSCRIPTION_CSP)
        return response
