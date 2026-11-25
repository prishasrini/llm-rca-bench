"""A simulated e-commerce service whose failures we control precisely.

It is a real FastAPI app (real routing, middleware, status codes and logging),
driven in-process. Delays use a *virtual clock*: instead of sleeping, a request
adds simulated seconds to its own elapsed time. That makes a 10-minute incident
run in well under a second, and every trial exactly reproducible from its seed.
"""

from __future__ import annotations

import contextvars
import random
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# A mutable box per request: the endpoint runs in a child task that sees a copy
# of the context, so we mutate a shared list instead of re-setting the variable.
_virtual: contextvars.ContextVar[list] = contextvars.ContextVar("virtual_seconds")


def delay(seconds: float) -> None:
    """Simulated wait: costs no real time, but counts toward request latency."""
    box = _virtual.get(None)
    if box is not None:
        box[0] += seconds


PRODUCTS = {1: 4999, 2: 1899, 3: 8999, 4: 1299}


class Checkout(BaseModel):
    product_id: int
    quantity: int = Field(1, ge=1, le=10)


@dataclass
class RequestRecord:
    t: float           # virtual timestamp (seconds since trial start)
    method: str
    path: str
    status: int
    latency: float


@dataclass
class LogRecord:
    t: float
    level: str
    msg: str
    fields: dict = field(default_factory=dict)


@dataclass
class ShopState:
    rng: random.Random
    faults: set[str] = field(default_factory=set)
    version: str = "1.3.2"
    cpu: float = 0.35
    now: float = 0.0
    requests: list[RequestRecord] = field(default_factory=list)
    logs: list[LogRecord] = field(default_factory=list)

    def log(self, level: str, msg: str, **fields) -> None:
        self.logs.append(LogRecord(self.now, level, msg, fields))


def create_app(state: ShopState) -> FastAPI:
    app = FastAPI()
    rng = state.rng

    @app.middleware("http")
    async def observe(request: Request, call_next):
        box = [0.0]
        token = _virtual.set(box)
        # baseline service time, plus a uniform slowdown when the CPU is saturated
        delay(rng.uniform(0.004, 0.02))
        if "resource_saturation" in state.faults:
            delay(rng.uniform(0.3, 1.2))
            if rng.random() < 0.05:
                state.log("WARNING", f"event loop lag {rng.randint(300, 900)}ms", component="shop-api")
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:  # pragma: no cover - endpoints convert errors themselves
            state.log("ERROR", f"unhandled exception: {exc}", component="shop-api")
            response, status = JSONResponse({"detail": "error"}, status_code=500), 500
        latency = box[0]  # virtual time only, so trials are exactly reproducible
        _virtual.reset(token)
        route = request.scope.get("route")
        state.requests.append(
            RequestRecord(state.now, request.method, route.path if route else "unmatched", status, latency)
        )
        return response

    def db_query(endpoint: str) -> None:
        if "database_outage" in state.faults:
            delay(rng.uniform(1.0, 3.0))
            state.log("ERROR", "database connection timeout", component="database", endpoint=endpoint)
            raise HTTPException(503, "database unavailable")
        if "database_slow" in state.faults:
            took = rng.uniform(0.5, 2.0)
            delay(took)
            if rng.random() < 0.2:
                state.log("WARNING", f"slow query took {int(took * 1000)}ms", component="database", endpoint=endpoint)
        else:
            delay(rng.uniform(0.003, 0.02))

    def cache_get(endpoint: str) -> bool:
        if "cache_failure" in state.faults:
            delay(rng.uniform(0.002, 0.01))  # connection refused quickly
            state.log("WARNING", "cache connection refused, falling back to database", component="cache", endpoint=endpoint)
            return False
        delay(rng.uniform(0.0005, 0.002))
        return rng.random() < 0.8  # 80% hit rate normally

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": state.version}

    @app.get("/products")
    async def products():
        if not cache_get("/products"):
            db_query("/products")
            if "cache_failure" in state.faults:
                delay(rng.uniform(0.08, 0.25))  # extra DB load from cache misses
        return [{"id": k, "price": v} for k, v in PRODUCTS.items()]

    @app.get("/products/{pid}")
    async def product(pid: int):
        if not cache_get("/products/{pid}"):
            db_query("/products/{pid}")
            if "cache_failure" in state.faults:
                delay(rng.uniform(0.05, 0.2))
        if pid not in PRODUCTS:
            state.log("WARNING", "product not found", product_id=pid)
            raise HTTPException(404, "not found")
        return {"id": pid, "price": PRODUCTS[pid]}

    @app.post("/checkout")
    async def checkout(body: Checkout):
        db_query("/checkout")
        if "bad_deployment" in state.faults and rng.random() < 0.6:
            state.log("ERROR", "unhandled exception in create_order: KeyError: 'currency'",
                      component="checkout-service", version=state.version)
            raise HTTPException(500, "internal error")

        # payment provider call
        if "upstream_payment_slow" in state.faults:
            took = rng.uniform(1.5, 4.0)
            delay(min(took, 3.5))
            if took > 3.5:
                state.log("ERROR", "payment provider timeout after 3500ms", component="payment-provider")
                raise HTTPException(504, "payment timeout")
            if rng.random() < 0.3:
                state.log("WARNING", f"payment provider response slow ({took:.1f}s)", component="payment-provider")
        else:
            delay(rng.uniform(0.03, 0.12))
        if "upstream_payment_errors" in state.faults and rng.random() < 0.4:
            state.log("ERROR", "payment provider returned 502 bad gateway", component="payment-provider")
            raise HTTPException(502, "payment provider unavailable")
        if rng.random() < 0.03:
            state.log("WARNING", "card declined", component="payment-provider")
            raise HTTPException(402, "card declined")

        # write the receipt
        if "disk_full" in state.faults and rng.random() < 0.5:
            state.log("ERROR", "failed to write receipt: OSError: [Errno 28] No space left on device",
                      component="storage", path="/var/receipts")
            raise HTTPException(500, "internal error")
        delay(rng.uniform(0.002, 0.01))
        return {"status": "confirmed", "total": PRODUCTS.get(body.product_id, 0) * body.quantity}

    return app
