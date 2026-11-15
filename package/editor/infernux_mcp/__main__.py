"""Run the same service over standard stdio or Streamable HTTP."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


async def run(args):
    from .server import create_server
    from .source_lifetime import run_guarded
    stopping = asyncio.Event()
    mcp = create_server(args.project, stopping=stopping)
    if args.transport == "stdio":
        return await run_guarded(lambda: mcp.run_async(transport="stdio", show_banner=False), stopping)
    import uvicorn
    channel = socket.socket()
    channel.bind(("127.0.0.1", args.port))
    channel.listen()
    port = channel.getsockname()[1]
    path = Path(args.project).resolve() / "Cache" / "Automation" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    async def serve():
        descriptor = {"endpoint": f"http://127.0.0.1:{port}/mcp", "pid": os.getpid()}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(descriptor), encoding="utf-8")
        temporary.replace(path)
        await server.serve(sockets=[channel])
    try:
        server = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp"), log_level="warning",
                                              timeout_graceful_shutdown=2))
        return await run_guarded(serve, stopping, stop=lambda: setattr(server, "should_exit", True))
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                path.unlink(missing_ok=True)
        except (FileNotFoundError, ValueError):
            pass
        channel.close()


if __name__ == "__main__":
    main()
