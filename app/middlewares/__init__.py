from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.utils.logger import get_logger
from config import cors_settings, server_settings, subscription_env_settings

from .gzip import PanelGZipMiddleware
from .request_logging import RequestProcessTimeLoggingMiddleware
from .security_headers import SubscriptionSecurityHeadersMiddleware


def setup_middleware(app: FastAPI):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_settings.allowed_origins,
        allow_credentials="*" not in cors_settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if server_settings.gzip_enabled:
        # Level 6 is close to 9 in size at a fraction of the CPU. SSE (text/event-stream) is never compressed.
        app.add_middleware(PanelGZipMiddleware, minimum_size=server_settings.gzip_minimum_size, compresslevel=6)
    if server_settings.proxy_headers:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=server_settings.forwarded_allow_ips)
    app.add_middleware(RequestProcessTimeLoggingMiddleware, access_logger=get_logger("uvicorn.access"))
    app.add_middleware(
        SubscriptionSecurityHeadersMiddleware,
        path_prefix=subscription_env_settings.path,
    )
