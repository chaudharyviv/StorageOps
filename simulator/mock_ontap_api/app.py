"""Read-only mock of a subset of the ONTAP REST API, replaying the simulator's metrics file.

Run with `uvicorn simulator.mock_ontap_api.app:create_app --factory`, setting
MOCK_ONTAP_DB to the metrics file and, optionally, MOCK_NOW to the replay time. A request
can override the replay time with `?mock_now=`. Only GET is served. The labels file that
sits beside the metrics is never opened.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from simulator.mock_ontap_api import builders
from simulator.mock_ontap_api.fields import select_fields
from simulator.mock_ontap_api.replay import NoSampleError, Replay, Snapshot

ENV_DB = "MOCK_ONTAP_DB"
ENV_NOW = "MOCK_NOW"

MockNow = Annotated[str | None, Query(description="Replay time (ISO-8601); mock-only parameter.")]
Fields = Annotated[list[str] | None, Query(description="Fields to return; see the ONTAP docs.")]


class ApiError(Exception):
    """An error returned in ONTAP's `error` envelope."""

    def __init__(self, status: int, code: str, message: str, target: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.target = status, code, message, target


def parse_mock_now(text: str) -> datetime:
    """Parse an ISO-8601 replay time; a time without a zone is taken as UTC."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"cannot read {text!r} as an ISO-8601 time") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def error_body(code: str, message: str, target: str | None = None) -> dict[str, Any]:
    """ONTAP's error envelope: {"error": {"code", "message", "target"}}."""
    error: dict[str, str] = {"code": code, "message": message}
    if target:
        error["target"] = target
    return {"error": error}


def _snapshot(request: Request, mock_now: str | None) -> Snapshot:
    replay: Replay = request.app.state.replay
    at: datetime | None = request.app.state.mock_now
    if mock_now:
        try:
            at = parse_mock_now(mock_now)
        except ValueError as err:
            raise ApiError(400, "mock_invalid_time", str(err), "mock_now") from None
    try:
        return replay.snapshot(at)
    except NoSampleError as err:
        raise ApiError(404, "4", f"entry doesn't exist: {err}", "mock_now") from None


def _collection(
    request: Request,
    mock_now: str | None,
    fields: list[str] | None,
    path: str,
    build: Callable[[Snapshot], list[dict[str, Any]]],
    expensive: tuple[str, ...],
) -> JSONResponse:
    records = [
        select_fields(r, fields or [], expensive) for r in build(_snapshot(request, mock_now))
    ]
    return JSONResponse(
        {"_links": {"self": {"href": path}}, "num_records": len(records), "records": records}
    )


router = APIRouter()
_P = builders.COLLECTION_PATHS


@router.get(_P["cluster"])
def get_cluster(request: Request, mock_now: MockNow = None, fields: Fields = None) -> JSONResponse:
    """GET /cluster: the cluster object (not a collection)."""
    record = builders.build_cluster_record(_snapshot(request, mock_now))
    return JSONResponse(select_fields(record, fields or [], builders.NO_EXPENSIVE))


@router.get(_P["aggregates"])
def get_aggregates(
    request: Request, mock_now: MockNow = None, fields: Fields = None
) -> JSONResponse:
    """GET /storage/aggregates."""
    return _collection(
        request, mock_now, fields, _P["aggregates"], builders.build_aggregates,
        builders.AGGREGATE_EXPENSIVE,
    )  # fmt: skip


@router.get(_P["volumes"])
def get_volumes(request: Request, mock_now: MockNow = None, fields: Fields = None) -> JSONResponse:
    """GET /storage/volumes."""
    return _collection(
        request, mock_now, fields, _P["volumes"], builders.build_volumes, builders.VOLUME_EXPENSIVE
    )


@router.get(_P["snapmirror"])
def get_snapmirror(
    request: Request, mock_now: MockNow = None, fields: Fields = None
) -> JSONResponse:
    """GET /snapmirror/relationships."""
    return _collection(
        request, mock_now, fields, _P["snapmirror"], builders.build_snapmirror,
        builders.SNAPMIRROR_EXPENSIVE,
    )  # fmt: skip


@router.get(_P["qos"])
def get_qos_policies(
    request: Request, mock_now: MockNow = None, fields: Fields = None
) -> JSONResponse:
    """GET /storage/qos/policies."""
    return _collection(
        request, mock_now, fields, _P["qos"], builders.build_qos_policies, builders.NO_EXPENSIVE
    )


def create_app(db_path: Path | None = None, mock_now: datetime | str | None = None) -> FastAPI:
    """Build the app over a metrics file; arguments default to MOCK_ONTAP_DB and MOCK_NOW."""
    path = db_path or (Path(os.environ[ENV_DB]) if os.environ.get(ENV_DB) else None)
    if path is None:
        raise RuntimeError(f"set {ENV_DB} to the simulator's metrics file, or pass db_path")
    raw_now = mock_now if mock_now is not None else os.environ.get(ENV_NOW)
    now = parse_mock_now(raw_now) if isinstance(raw_now, str) and raw_now else raw_now
    replay = Replay(path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        replay.close()

    app = FastAPI(title="Mock ONTAP REST API (read-only subset)", lifespan=lifespan)
    app.state.replay = replay
    app.state.mock_now = now if isinstance(now, datetime) else None
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, exc.target), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = (
            "this mock API is read-only; only GET is served"
            if exc.status_code == 405
            else str(exc.detail)
        )
        return JSONResponse(
            error_body(str(exc.status_code), message), status_code=exc.status_code,
            headers=exc.headers,
        )  # fmt: skip

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(error_body("400", str(exc.errors()[0]["msg"])), status_code=400)

    return app
