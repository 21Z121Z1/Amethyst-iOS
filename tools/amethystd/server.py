from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from time import monotonic
from typing import Any

from .container_io import AgentContainerClient
from .store import StateStore
from .supervisor import Supervisor


class AgentServer:
    def __init__(self, store: StateStore | None = None) -> None:
        self.store = store or StateStore()
        self.supervisor = Supervisor(self.store)
        self.server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()

    async def _stop_current(self, force: bool = False, timeout: float = 30) -> dict[str, Any]:
        state = self.store.load()
        if state is None:
            return {"ok": True, "observed": True, "state": "no_active_run"}
        if not self.supervisor.device_udid or not self.supervisor.device.pmd3_python_api_available():
            return {"ok": False, "error": "transport_unavailable", "detail": "cannot reach Agent v2 control channel"}

        client = AgentContainerClient(self.supervisor.device_udid, self.supervisor.bundle_id)
        response = await self.supervisor._agent_request(
            client,
            state,
            "terminate",
            {"force": force},
            guard_generation=bool(state.process_generation),
            timeout=min(timeout, 20),
        )
        if not response.get("ok"):
            return {"ok": False, "observed": False, "response": response}
        if force:
            self.supervisor.close()
            return {
                "ok": False,
                "accepted": True,
                "observed": False,
                "detail": "force-exit was accepted; this path intentionally does not claim process exit without a fresh process observation",
                "response": response,
            }

        deadline = monotonic() + timeout
        while monotonic() < deadline:
            probe = await self.supervisor._agent_request(
                client,
                state,
                "status",
                guard_generation=False,
                timeout=min(5, max(1, deadline - monotonic())),
            )
            if probe.get("ok") and probe.get("state") == "launcher":
                self.supervisor._observe_identity(state, probe)
                self.store.save(state)
                self.supervisor.close()
                return {"ok": True, "accepted": True, "observed": True, "response": probe}
            await asyncio.sleep(0.25)
        return {
            "ok": False,
            "accepted": True,
            "observed": False,
            "detail": "terminate request was accepted but launcher state was not observed before timeout",
            "response": response,
        }

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        params = request.get("params") or {}
        if method == "ping":
            return {"ok": True, "daemon": "amethystd"}
        if method == "doctor":
            return self.supervisor.doctor()
        if method == "status":
            return self.supervisor.status()
        if method == "deploy":
            return await self.supervisor.deploy(**params)
        if method == "stop":
            return await self._stop_current(**params)
        if method == "run_smoke":
            if self.supervisor.jit is not None:
                self.supervisor.close()
            result = await self.supervisor.run_smoke(**params)
            state = result.get("state") or {}
            run_id = state.get("run_id")
            if isinstance(run_id, str) and run_id:
                collection = await self.supervisor.collect(
                    run_id,
                    include_crashes=not bool(result.get("ok")),
                    screenshot=True,
                )
                result["artifacts"] = collection
            if not result.get("ok"):
                self.supervisor.close()
            return result
        if method == "stage_payload":
            return await self.supervisor.stage_payload(**params)
        if method == "collect":
            return await self.supervisor.collect(**params)
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
