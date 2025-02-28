from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.utils.logger import get_logger
from config import ALLOWED_ORIGINS, DOCS, XRAY_SUBSCRIPTION_PATH

__version__ = "0.8.4"

startup_functions = []
shutdown_functions = []


def on_startup(func):
    startup_functions.append(func)
    return func


def on_shutdown(func):
    shutdown_functions.append(func)
    return func


@asynccontextmanager
async def lifespan(app: FastAPI):
    for func in startup_functions:
        if callable(func):
            if hasattr(func, "__await__"):
                if "app" in func.__code__.co_varnames:
                    await func(app)
                else:
                    await func()
            else:
                if "app" in func.__code__.co_varnames:
                    func(app)
                else:
                    func()

    yield

    for func in shutdown_functions:
        print("Running ", func.__name__)
        if callable(func):
            if hasattr(func, "__await__"):
                if "app" in func.__code__.co_varnames:
                    await func(app)
                else:
                    await func()
            else:
                if "app" in func.__code__.co_varnames:
                    func(app)
                else:
                    func()


app = FastAPI(
    title="MarzbanAPI",
    description="Unified GUI Censorship Resistant Solution Powered by Xray",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if DOCS else None,
    redoc_url="/redoc" if DOCS else None,
)

scheduler = BackgroundScheduler({"apscheduler.job_defaults.max_instances": 20}, timezone="UTC")
logger = get_logger()


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from app import dashboard, jobs, routers, telegram  # noqa
from app.routers import api_router  # noqa

app.include_router(api_router)


def use_route_names_as_operation_ids(app: FastAPI) -> None:
    for route in app.routes:
        if isinstance(route, APIRoute):
            route.operation_id = route.name


use_route_names_as_operation_ids(app)


@on_startup
def validate_paths():
    paths = [f"{r.path}/" for r in app.routes]
    paths.append("/api/")
    if f"/{XRAY_SUBSCRIPTION_PATH}/" in paths:
        raise ValueError(f"you can't use /{XRAY_SUBSCRIPTION_PATH}/ as subscription path it reserved for {app.title}")


on_startup(scheduler.start)
on_shutdown(scheduler.shutdown)


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = {}
    for error in exc.errors():
        details[error["loc"][-1]] = error.get("msg")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": details}),
    )
