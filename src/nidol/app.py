from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nidol.config import settings
from nidol.models import ErrorBody, NidolError, ResolveRequest, ResolveResponse
from nidol.resolve import resolve


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.required_timeout_s,
        follow_redirects=True,
    ) as client:
        app.state.http = client
        yield


app = FastAPI(title="nidol", version="0.1.0", lifespan=lifespan)

_ERRORS = {code: {"model": ErrorBody} for code in (404, 409, 422, 502, 503)}


@app.exception_handler(NidolError)
async def nidol_error(_: Request, exc: NidolError) -> JSONResponse:
    return JSONResponse(exc.body.model_dump(exclude_none=True), status_code=exc.status)


@app.post("/resolve", responses=_ERRORS)
async def post_resolve(body: ResolveRequest, request: Request) -> ResolveResponse:
    return await resolve(body, request.app.state.http)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
