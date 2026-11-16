"""Synchronous boundary only; the execution loop remains asynchronous."""
import asyncio

from .oneshot import run_once


def run_sync(runtime, task, *, engine: str = "turn"):
    if engine != "turn":
        raise ValueError("engine must be turn; the legacy loop has been removed")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run cannot execute inside an active event loop; await run_async instead")
    return asyncio.run(run_once(runtime, task))
