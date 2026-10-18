import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api import exceptions, imports, investigation, tasks
from app.clock import now
from app.errors import DomainError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="OPSiB pilot", version="1.0.0")
app.include_router(imports.router)
app.include_router(exceptions.router)
app.include_router(tasks.router)
app.include_router(investigation.router)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    # one error shape everywhere: {"error": "...", "detail": "..."}
    return JSONResponse(status_code=exc.status_code,
                        content={"error": f"http_{exc.status_code}", "detail": exc.detail})


@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "evaluation_time": now().isoformat()}