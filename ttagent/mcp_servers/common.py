"""
Shared plumbing for the MCP servers: one service instance per process, stderr
logging (stdout belongs to the stdio JSON-RPC stream!), and an `audited`
decorator that records every tool call -- arguments, result summary, latency --
in the SQLite audit table so the full Agent -> MCP -> Server -> Response chain
can be replayed in a viva or a paper.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import sys
import time
import warnings
from typing import Any, Callable

from ttagent.engine.service import ServiceError, TimetableService

warnings.filterwarnings("ignore")  # keep stderr readable (pydantic-settings noise from FastMCP)
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

_service: TimetableService | None = None


def service() -> TimetableService:
    global _service
    if _service is None:
        _service = TimetableService()
    return _service


def _summary(result: Any) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) < 400 else s[:400] + f"... ({len(s)} chars)"


def audited(server: str) -> Callable:
    log = logging.getLogger(f"mcp.{server}")

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.time()
            bound = inspect.signature(fn).bind_partial(*args, **kwargs)
            call_args = dict(bound.arguments)
            log.info("-> %s %s", fn.__name__, json.dumps(call_args, default=str))
            ok = True
            try:
                result = fn(*args, **kwargs)
            except ServiceError as e:
                ok = False
                result = {"error": str(e)}
            except Exception as e:  # never crash the server; report to the agent
                ok = False
                log.exception("tool %s failed", fn.__name__)
                result = {"error": f"{type(e).__name__}: {e}"}
            ms = (time.time() - t0) * 1000
            log.info("<- %s %.0f ms %s", fn.__name__, ms, "ok" if ok else "ERROR")
            try:
                service().store.audit(server, fn.__name__, call_args, _summary(result), ms, ok)
            except Exception:  # audit must never break a tool
                pass
            if isinstance(result, list):
                return {"items": result, "count": len(result)}
            return result
        return wrapper
    return deco
