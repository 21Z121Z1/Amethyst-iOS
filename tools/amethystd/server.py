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
        self.supervisor = self._new_supervisor()
        self.server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()

    def _new_supervisor(self) -> Supervisor:
        config = self.store.load_config()
        return Supervisor(
            self.store,
            device_udid=config.get("device_udid"),
            bundle_id=config.get("bundle_id"),
        )

    def _configure(self, *, device_udid: str | None = None, bundle_id: str | None = None) -> dict[str, Any]:
        current = self.store.load_config()
        if device_udid is not None:
            current["device_udid"] = device_udid
        if bundle_id is not None:
            current["bundle_id"] = bundle_id
        self.store.save_config(current)
        self.supervisor.close()
        self.supervisor = self._new_supervisor()
        return {
            "ok": True,
            "configuration": self.store.load_config(),
            "note": "device identifiers are local-only under .amethyst-agent and are not versioned",
        }

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

    async def _query_logs(
        self,
        *,
        source: str = "latest",
        run_id: str | None = None,
        contains: str | None = None,
        limit: int = 80,
        max_bytes: int = 65536,
    ) -> dict[str, Any]:
        if not self.supervisor.device_udid:
            return {"ok": False, "failure": "DEVICE_NOT_FOUND", "detail": "device UDID is required"}
        state = self.store.load()
        effective_run = run_id or (state.run_id if state else None)
        if source == "latest":
            relative = "latestlog.txt"
        elif source in {"app", "lab"}:
            if not effective_run:
                return {"ok": False, "error": "run_id_required", "detail": f"{source} event query requires a run id"}
            directory = "agent-events" if source == "app" else "agent-lab"
            relative = f"{directory}/{effective_run}.jsonl"
        else:
            return {"ok": False, "error": "invalid_log_source", "detail": "source must be latest, app, or lab"}

        limit = max(1, min(int(limit), 500))
        max_bytes = max(1024, min(int(max_bytes), 65536))
        client = AgentContainerClient(self.supervisor.device_udid, self.supervisor.bundle_id)
        chunk = await client.read_tail(relative, max_bytes=max_bytes)
        if not chunk.get("ok"):
            return chunk
        if chunk.get("exists") is False:
            return {"ok": True, "source": source, "run_id": effective_run, "lines": [], "exists": False}
        text = (chunk.get("data") or b"").decode("utf-8", errors="replace")
        lines = text.splitlines()
        if contains:
            needle = contains.casefold()
            lines = [line for line in lines if needle in line.casefold()]
        lines = lines[-limit:]
        return {
            "ok": True,
            "source": source,
            "run_id": effective_run,
            "contains": contains,
            "lines": lines,
            "cursor": {
                "path": relative,
                "file_size": chunk.get("size"),
                "window_offset": chunk.get("offset"),
                "next_offset": chunk.get("next_offset"),
                "bytes_examined": len(text.encode("utf-8", errors="replace")),
                "line_count": len(lines),
            },
        }

    async def _input_key(
        self,
        *,
        key: int,
        mode: str = "press",
        hold_ms: int = 50,
        scancode: int = 0,
        mods: int = 0,
    ) -> dict[str, Any]:
        state = self.store.load()
        if state is None or not state.process_generation:
            return {"ok": False, "error": "no_active_process", "detail": "launch/reconcile Amethyst before sending input"}
        if not self.supervisor.device_udid:
            return {"ok": False, "failure": "DEVICE_NOT_FOUND", "detail": "device UDID is required"}
        normalized = mode.lower()
        if normalized not in {"press", "release"}:
            return {
                "ok": False,
                "error": "invalid_key_mode",
                "detail": "daemon input_key accepts press/release only; amethystctl expands tap into an ordered pair",
            }
        params: dict[str, Any] = {
            "key": int(key),
            "scancode": int(scancode),
            "mods": int(mods),
            "action": 1 if normalized == "press" else 0,
        }
        client = AgentContainerClient(self.supervisor.device_udid, self.supervisor.bundle_id)
        return await self.supervisor._agent_request(client, state, "input/key", params, timeout=10)

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        params = request.get("params") or {}
        if method == "ping":
            return {"ok": True, "daemon": "amethystd"}
        if method == "configure":
            return self._configure(**params)
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
        if method == "query_logs":
            return await self._query_logs(**params)
        if method == "input_key":
            return await self._input_key(**params)
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
