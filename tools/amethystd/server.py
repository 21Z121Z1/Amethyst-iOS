from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from .store import StateStore
from .supervisor import Supervisor


class AgentServer:
    def __init__(self, store: StateStore | None = None) -> None:
        self.store = store or StateStore()
        self.supervisor = Supervisor(self.store)
        self.server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        params = request.get("params") or {}
        if method == "doctor":
            return self.supervisor.doctor()
        if method == "status":
            return self.supervisor.status()
        if method == "run_smoke":
            return await self.supervisor.run_smoke(**params)
        if method == "stage_payload":
            return await self.supervisor.stage_payload(**params)
        if method == "shutdown":
            self._stop.set()
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": "unknown_method", "method": method}

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            result = await self.dispatch(request)
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)}
        writer.write((json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve(self) -> None:
        socket_path = self.store.socket_path
        if socket_path.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                writer.close()
                await writer.wait_closed()
                raise RuntimeError(f"amethystd already owns {socket_path}")
            except (ConnectionRefusedError, FileNotFoundError):
                socket_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._client, path=str(socket_path))
        os.chmod(socket_path, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass
        async with self.server:
            await self._stop.wait()
        self.supervisor.close()
        socket_path.unlink(missing_ok=True)


async def request_daemon(socket_path: Path, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write((json.dumps({"method": method, "params": params or {}}, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    if not raw:
        raise RuntimeError("amethystd returned no response")
    return json.loads(raw.decode("utf-8"))
