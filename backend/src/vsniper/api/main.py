import logging
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from vsniper.api.routes import ai_models, candidates, data_management, errors, health, operations, searches, settings, stats, storage, taste, telegram
from vsniper.core.config import get_settings
from vsniper.core.database import configure_app_logging
from vsniper.core.state import get_state

configure_app_logging()
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    started = perf_counter()
    logger.info("app state initialization started")
    get_state()
    logger.info("app state initialization finished in %.1fs", perf_counter() - started)
    yield


app = FastAPI(title="vsniper API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(stats.router, prefix="/api")
app.include_router(searches.router, prefix="/api")
app.include_router(taste.router, prefix="/api")
app.include_router(candidates.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(ai_models.router, prefix="/api")
app.include_router(telegram.router, prefix="/api")
app.include_router(data_management.router, prefix="/api")
app.include_router(operations.router, prefix="/api")
app.include_router(storage.router, prefix="/api")
app.include_router(errors.router, prefix="/api")

_ERROR_RECORDED_HEADER = "X-Vsniper-Error-Recorded"


@app.exception_handler(HTTPException)
async def record_server_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    headers = dict(exc.headers or {})
    if exc.status_code >= 500:
        cause = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
        get_state().errors.record(
            source="api",
            operation=f"{request.method} {request.url.path}",
            summary=f"API request returned {exc.status_code}",
            message=str(exc.detail),
            exception=cause,
            details={
                "method": request.method,
                "path": request.url.path,
                "status_code": exc.status_code,
                "detail": exc.detail,
            },
        )
        headers[_ERROR_RECORDED_HEADER] = "1"
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": jsonable_encoder(exc.detail)},
        headers=headers,
    )


@app.middleware("http")
async def log_api_requests(request: Request, call_next):
    started = perf_counter()
    should_log = request.url.path != "/healthz"
    if should_log:
        logger.info("request started %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception("request failed %s %s after %.1fs", request.method, request.url.path, perf_counter() - started)
        try:
            get_state().errors.record(
                source="api",
                operation=f"{request.method} {request.url.path}",
                summary="API request failed",
                exception=exc,
                details={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_seconds": perf_counter() - started,
                },
            )
        except Exception:
            logger.exception("could not record failed API request")
        raise
    already_recorded = response.headers.get(_ERROR_RECORDED_HEADER) == "1"
    if already_recorded:
        del response.headers[_ERROR_RECORDED_HEADER]
    if response.status_code >= 500 and not already_recorded:
        get_state().errors.record(
            source="api",
            operation=f"{request.method} {request.url.path}",
            summary=f"API request returned {response.status_code}",
            message=f"{request.method} {request.url.path} returned HTTP {response.status_code}.",
            details={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_seconds": perf_counter() - started,
            },
        )
    if should_log:
        logger.info(
            "request finished %s %s status=%d duration=%.1fs",
            request.method,
            request.url.path,
            response.status_code,
            perf_counter() - started,
        )
    return response


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "vsniper-api", "status": "ok"}
