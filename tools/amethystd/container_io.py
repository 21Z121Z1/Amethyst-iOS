from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, AsyncIterator
from uuid import uuid4

_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
_REMOTE_DOCUMENTS = "/Documents"


def safe_component(value: str, label: str) -> str:
    if not _SAFE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def documents_path(relative: str) -> str:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid Documents-relative path: {relative!r}")
    return str(PurePosixPath(_REMOTE_DOCUMENTS) / path)


class AgentContainerClient:
    """Documents-relative Agent v2 transport using current pymobiledevice3 async House Arrest APIs."""

    def __init__(self, udid: str, bundle_id: str) -> None:
        if not udid:
            raise ValueError("device UDID is required")
        self.udid = udid
        self.bundle_id = bundle_id

    @asynccontextmanager
    async def _afc(self) -> AsyncIterator[Any]:
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.house_arrest import HouseArrestService
        except ImportError as exc:
            raise RuntimeError(
                "pymobiledevice3 Python package is required by amethystd; install requirements-agent.txt"
            ) from exc

        async with await create_using_usbmux(
            serial=self.udid, autopair=False, connection_type="USB"
        ) as lockdown:
            async with await HouseArrestService.create(
                lockdown, self.bundle_id, documents_only=True
            ) as afc:
                yield afc

    async def read_optional(self, relative: str) -> bytes | None:
        try:
            from pymobiledevice3.exceptions import AfcFileNotFoundError
        except ImportError as exc:
            raise RuntimeError("pymobiledevice3 Python package is required by amethystd") from exc
        async with self._afc() as afc:
            try:
                return await afc.get_file_contents(documents_path(relative))
            except AfcFileNotFoundError:
                return None

    async def submit(self, request: dict[str, Any]) -> str:
        request_id = safe_component(str(request["request_id"]), "request_id")
        run_id = safe_component(str(request["run_id"]), "run_id")
        if request.get("protocol") != "amethyst-agent/v2":
            raise ValueError("only amethyst-agent/v2 requests are accepted by this transport")
        request["request_id"] = request_id
        request["run_id"] = run_id
        data = (json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n").encode()
        final_path = documents_path(f"agent-requests/{request_id}.json")
        temp_path = documents_path(f"agent-requests/.{request_id}.{uuid4().hex}.tmp")
        async with self._afc() as afc:
            await afc.makedirs(documents_path("agent-requests"))
            await afc.set_file_contents(temp_path, data)
            await afc.rename(temp_path, final_path)
        return request_id

    async def response(self, request_id: str) -> dict[str, Any] | None:
        request_id = safe_component(request_id, "request_id")
        raw = await self.read_optional(f"agent-responses/{request_id}.json")
        if raw is None:
            return None
        value = json.loads(raw.decode("utf-8"))
        if value.get("request_id") != request_id:
            raise RuntimeError("response/request_id mismatch")
        return value

    async def request(self, request: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        request_id = await self.submit(request)
        deadline = monotonic() + timeout
        delay = 0.05
        while monotonic() < deadline:
            response = await self.response(request_id)
            if response is not None:
                return response
            await asyncio.sleep(delay)
            delay = min(delay * 1.4, 0.5)
        raise TimeoutError(f"agent response timeout for {request_id}")

    @staticmethod
    def _jsonl(raw: bytes | None) -> list[dict[str, Any]]:
        if raw is None:
            return []
        events: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        return events

    async def read_events(self, run_id: str) -> list[dict[str, Any]]:
        run_id = safe_component(run_id, "run_id")
        return self._jsonl(await self.read_optional(f"agent-events/{run_id}.jsonl"))

    async def prepare_lab_run(self, run_id: str) -> None:
        """Publish the active run without requiring an Objective-C -> Java bridge.

        Test-only Java/Fabric instrumentation can read `${user.home}/agent-lab/current-run-id`,
        then append its own events to `agent-lab/<run_id>.jsonl`.
        """
        run_id = safe_component(run_id, "run_id")
        root = documents_path("agent-lab")
        temp = documents_path(f"agent-lab/.current-run-{uuid4().hex}.tmp")
        final = documents_path("agent-lab/current-run-id")
        async with self._afc() as afc:
            await afc.makedirs(root)
            await afc.set_file_contents(temp, (run_id + "\n").encode())
            try:
                await afc.rm(final)
            except Exception:
                pass
            await afc.rename(temp, final)

    async def read_lab_events(self, run_id: str) -> list[dict[str, Any]]:
        run_id = safe_component(run_id, "run_id")
        events = self._jsonl(await self.read_optional(f"agent-lab/{run_id}.jsonl"))
        for event in events:
            if event.get("run_id") != run_id:
                raise RuntimeError("lab event/run_id mismatch")
        return events

    async def stage_payload(self, local_path: Path | str, payload_name: str) -> dict[str, Any]:
        payload_name = safe_component(payload_name, "payload_name")
        root = Path(local_path).resolve()
        if not root.exists():
            raise FileNotFoundError(root)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        manifest_files: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        for path in files:
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            if ".." in PurePosixPath(relative).parts:
                raise ValueError("payload path escapes root")
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            manifest_files.append({"path": relative, "size": len(data), "sha256": sha})
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha))
        deployment_digest = digest.hexdigest()
        stage_relative = f"agent-payloads/.staging/{deployment_digest}"
        stage_root = documents_path(stage_relative)
        manifest = {
            "version": 1,
            "name": payload_name,
            "digest": deployment_digest,
            "files": manifest_files,
        }
        async with self._afc() as afc:
            await afc.makedirs(stage_root)
            for local, entry in zip(files, manifest_files, strict=True):
                remote = str(PurePosixPath(stage_root) / entry["path"])
                await afc.makedirs(str(PurePosixPath(remote).parent))
                data = local.read_bytes()
                await afc.set_file_contents(remote, data)
                observed = await afc.get_file_contents(remote)
                if len(observed) != entry["size"] or hashlib.sha256(observed).hexdigest() != entry["sha256"]:
                    raise IOError(f"payload verification failed for {entry['path']}")
            manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            await afc.set_file_contents(str(PurePosixPath(stage_root) / "manifest.json"), manifest_data)
            active_root = documents_path("agent-payloads/active")
            await afc.makedirs(active_root)
            pointer = json.dumps(
                {"name": payload_name, "digest": deployment_digest, "stage": stage_relative},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            temp = str(PurePosixPath(active_root) / f".{payload_name}.{uuid4().hex}.tmp")
            final = str(PurePosixPath(active_root) / f"{payload_name}.json")
            await afc.set_file_contents(temp, pointer)
            try:
                await afc.rm(final)
            except Exception:
                pass
            await afc.rename(temp, final)
        return manifest
