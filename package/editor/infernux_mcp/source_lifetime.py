"""Stop this independent service when its current Python source changes."""

import asyncio
from pathlib import Path
import sys

from watchfiles import awatch


def source_roots():
    import infernux_automation
    return tuple(dict.fromkeys((Path(__file__).resolve().parent,
                                Path(infernux_automation.__file__).resolve().parent)))


async def _source_change(stopping):
    try:
        async for changes in awatch(*source_roots(), watch_filter=lambda _event, path: Path(path).suffix.casefold() == ".py",
                                   debounce=50, step=10, rust_timeout=250, force_polling=False):
            stopping.set()
            paths = ", ".join(sorted(path for _event, path in changes))
            print(f"Infernux MCP source changed: {paths}. Service stopped; restart to load current code.",
                  file=sys.stderr, flush=True)
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        stopping.set()
        print(f"Infernux MCP source monitoring failed: {exc}. Service stopped; restart is required.",
              file=sys.stderr, flush=True)
        raise


async def run_guarded(run, stopping, *, stop=None):
    """Compose filesystem events with the transport's existing shutdown path."""
    watch = asyncio.create_task(_source_change(stopping))
    # awatch installs its native filesystem listener before its first await.
    # Register it before the transport can accept a client or publish readiness.
    await asyncio.sleep(0)
    service = asyncio.create_task(run())
    try:
        done, _pending = await asyncio.wait((watch, service), return_when=asyncio.FIRST_COMPLETED)
        if watch in done:
            stopping.set()
            if stop is None:
                service.cancel()
            else:
                stop()
            await asyncio.gather(service, return_exceptions=True)
            await watch
            return 1
        await service
        return 0
    finally:
        watch.cancel()
        if not service.done():
            service.cancel()
        await asyncio.gather(watch, service, return_exceptions=True)
