from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from time import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .agent_output import bounded_result
from .container_io import AgentContainerClient, documents_path, safe_component
from .store import StateStore

MITHRIL_PAYLOAD = "mithril"
MITHRIL_LIBRARY = "libmithril.dylib"
HOT_SWAP_TARGETS = ("RENDERER_READY", "MENU_READY", "WORLD_READY", "CHUNKS_STABLE")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

RequestFn = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]]


def _active_digest(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "bundled"
    device = payload.get("device") if isinstance(payload.get("device"), dict) else payload
    if not isinstance(device, dict) or not device.get("active"):
        return "bundled"
    manifest = device.get("manifest")
    digest = manifest.get("digest") if isinstance(manifest, dict) else None
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest.lower()):
        raise RuntimeError("active Mithril payload does not expose a valid SHA-256 digest")
    return digest.lower()


def _manifest_digest(manifest: dict[str, Any]) -> str:
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest.lower()):
        raise RuntimeError("Mithril payload manifest has an invalid digest")
    return digest.lower()


def _validate_mithril_manifest(manifest: dict[str, Any], expected_digest: str | None = None) -> str:
    digest = _manifest_digest(manifest)
    if expected_digest is not None and digest != expected_digest.lower():
        raise RuntimeError(f"Mithril payload digest mismatch: expected {expected_digest}, observed {digest}")
    if manifest.get("version") != 1 or manifest.get("name") != MITHRIL_PAYLOAD:
        raise RuntimeError("Mithril payload manifest identity is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("Mithril payload manifest has no files")
    library = None
    for entry in files:
        if isinstance(entry, dict) and entry.get("path") == MITHRIL_LIBRARY:
            library = entry
            break
    if library is None:
        raise RuntimeError(f"Mithril payload is missing {MITHRIL_LIBRARY}")
    signature = library.get("code_signature")
    if not isinstance(signature, dict) or signature.get("verified") is not True:
        raise RuntimeError("Mithril payload cannot be activated because its staged dylib lacks verified code-signature provenance")
    return digest


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(value, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


class HotSwapLedger:
    def __init__(self, store: StateStore) -> None:
        self.path = store.root / "mithril-hot-swap.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "transactions": []}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("transactions"), list):
            raise RuntimeError("Mithril hot-swap ledger is corrupt")
        return value

    def append(self, transaction: dict[str, Any]) -> dict[str, Any]:
        value = self.load()
        transactions = [item for item in value["transactions"] if isinstance(item, dict)]
        transactions.append(transaction)
        value["transactions"] = transactions[-32:]
        value["last_transaction"] = transaction
        if transaction.get("status") == "passed":
            value["last_known_good_digest"] = transaction.get("after_digest")
        _atomic_json(self.path, value)
        return value

    def update_last(self, transaction: dict[str, Any]) -> dict[str, Any]:
        value = self.load()
        transactions = [item for item in value["transactions"] if isinstance(item, dict)]
        if not transactions or transactions[-1].get("id") != transaction.get("id"):
            transactions.append(transaction)
        else:
            transactions[-1] = transaction
        value["transactions"] = transactions[-32:]
        value["last_transaction"] = transaction
        if transaction.get("status") == "passed":
            value["last_known_good_digest"] = transaction.get("after_digest")
        _atomic_json(self.path, value)
        return value

    def rollback_target(self, current_digest: str) -> str:
        value = self.load()
        for transaction in reversed(value.get("transactions", [])):
            if not isinstance(transaction, dict):
                continue
            if transaction.get("after_digest") == current_digest:
                before = transaction.get("before_digest")
                if before == "bundled" or (isinstance(before, str) and _DIGEST.fullmatch(before)):
                    return before
        known_good = value.get("last_known_good_digest")
        if known_good == "bundled" or (isinstance(known_good, str) and _DIGEST.fullmatch(known_good)):
            if known_good != current_digest:
                return known_good
        raise RuntimeError("no previous Mithril digest is recorded for the currently active candidate; pass --digest explicitly")


async def verify_staged_mithril(client: AgentContainerClient, digest: str) -> dict[str, Any]:
    digest = digest.lower()
    if not _DIGEST.fullmatch(digest):
        raise ValueError("Mithril digest must be a 64-character SHA-256 hex string")
    stage = f"agent-payloads/.staging/{digest}"
    raw = await client.read_optional(f"{stage}/manifest.json")
    if raw is None:
        raise FileNotFoundError(f"staged Mithril manifest is missing for {digest}")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("staged Mithril manifest is not a JSON object")
    _validate_mithril_manifest(manifest, digest)

    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise RuntimeError("staged Mithril manifest contains a non-object file entry")
        relative = entry.get("path")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        if not isinstance(relative, str) or not relative or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise RuntimeError("staged Mithril manifest contains an unsafe path")
        if not isinstance(size, int) or size < 0 or not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256.lower()):
            raise RuntimeError(f"staged Mithril manifest metadata is invalid for {relative!r}")
        data = await client.read_optional(f"{stage}/{relative}")
        if data is None:
            raise FileNotFoundError(f"staged Mithril file is missing: {relative}")
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256.lower():
            raise RuntimeError(f"staged Mithril file failed SHA-256/size verification: {relative}")
    return manifest


async def activate_staged_mithril(client: AgentContainerClient, digest: str) -> dict[str, Any]:
    digest = digest.lower()
    manifest = await verify_staged_mithril(client, digest)
    active_root = documents_path("agent-payloads/active")
    final = documents_path(f"agent-payloads/active/{MITHRIL_PAYLOAD}.json")
    pointer = {
        "name": MITHRIL_PAYLOAD,
        "digest": digest,
        "stage": f"agent-payloads/.staging/{digest}",
    }
    data = json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode()
    temp = str(PurePosixPath(active_root) / f".{MITHRIL_PAYLOAD}.{uuid4().hex}.tmp")
    reconciled_after_error = False
    try:
        async with client._afc() as afc:  # package-internal state-changing transaction
            await afc.makedirs(active_root)
            await afc.set_file_contents(temp, data)
            try:
                await afc.rm(final)
            except Exception:
                pass
            await afc.rename(temp, final)
    except Exception:
        observed = await client.inspect_payload(MITHRIL_PAYLOAD)
        observed_digest = _active_digest(observed)
        if observed_digest != digest:
            raise
        reconciled_after_error = True

    observed = await client.inspect_payload(MITHRIL_PAYLOAD)
    if _active_digest(observed) != digest:
        raise RuntimeError("Mithril active pointer did not reconcile to the requested staged digest")
    return {
        "ok": True,
        "digest": digest,
        "manifest": manifest,
        "pointer": pointer,
        "reconciled_after_transport_error": reconciled_after_error,
    }


class MithrilHotSwapManager:
    def __init__(
        self,
        store: StateStore,
        request: RequestFn,
        client: AgentContainerClient,
    ) -> None:
        self.store = store
        self.request = request
        self.client = client
        self.ledger = HotSwapLedger(store)

    async def _inspect(self) -> dict[str, Any]:
        result = await self.request("inspect_payload", {"name": MITHRIL_PAYLOAD})
        if result.get("ok") is not True:
            raise RuntimeError(f"cannot inspect active Mithril payload: {json.dumps(result, sort_keys=True)}")
        return result

    async def _status(self) -> dict[str, Any]:
        result = await self.request("status", None)
        return result if isinstance(result, dict) else {"ok": False}

    async def _smoke(
        self,
        *,
        profile: str,
        target: str,
        timeout: float,
        allow_repeat_failure: bool,
        retry_reason: str | None,
    ) -> dict[str, Any]:
        return await self.request(
            "run_smoke",
            {
                "profile": profile,
                "target": target,
                "timeout": timeout,
                "require_dynamic_library_load": True,
                "candidate_name": MITHRIL_PAYLOAD,
                "allow_repeat_failure": allow_repeat_failure,
                "retry_reason": retry_reason,
            },
        )

    async def _verify_consumer(self, smoke: dict[str, Any], expected_digest: str) -> dict[str, Any]:
        state = smoke.get("state")
        if not isinstance(state, dict):
            raise RuntimeError("smoke result omitted state")
        candidate = state.get("candidate")
        if not isinstance(candidate, dict) or str(candidate.get("digest") or "bundled").lower() != expected_digest:
            raise RuntimeError("smoke state candidate digest does not match the hot-swap target")
        run_id = state.get("run_id")
        session = state.get("game_session_generation")
        if not isinstance(run_id, str) or not isinstance(session, str):
            raise RuntimeError("smoke result omitted run/session identity required for renderer provenance")
        events = await self.client.read_lab_events(run_id, session)
        renderer_events = [event for event in events if event.get("event") == "renderer_ready"]
        if not renderer_events:
            raise RuntimeError("smoke passed without an authoritative renderer_ready consumer event")
        event = renderer_events[-1]
        observed_digest = str(event.get("candidate_digest") or "bundled").lower()
        if event.get("provenance_ok") is not True or observed_digest != expected_digest:
            raise RuntimeError(
                f"LWJGL consumer provenance mismatch: expected {expected_digest}, observed {observed_digest}, "
                f"provenance_ok={event.get('provenance_ok')!r}"
            )
        if expected_digest != "bundled":
            loaded = str(event.get("loaded_library_path") or "")
            expected_fragment = f"/agent-payloads/.staging/{expected_digest}/{MITHRIL_LIBRARY}"
            if expected_fragment not in loaded:
                raise RuntimeError(f"LWJGL loaded library path is not the staged Mithril candidate: {loaded!r}")
        for symbol in ("glCompileShader", "glDrawElements"):
            if str(event.get(symbol) or "0") == "0":
                raise RuntimeError(f"LWJGL consumer event reported a null {symbol} address")
        return event

    def _transaction(
        self,
        *,
        operation: str,
        before_digest: str,
        after_digest: str,
        profile: str,
        target: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "id": f"mithril-{uuid4().hex[:20]}",
            "operation": operation,
            "status": "switching",
            "started_at": time(),
            "before_digest": before_digest,
            "after_digest": after_digest,
            "profile": profile,
            "target": target,
        }

    async def swap(
        self,
        local_path: str,
        *,
        profile: str,
        target: str = "RENDERER_READY",
        timeout: float = 180,
        allow_repeat_failure: bool = False,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        path = Path(local_path).expanduser().resolve()
        if not path.is_file() or path.name != MITHRIL_LIBRARY:
            raise ValueError(f"swap expects a regular file named {MITHRIL_LIBRARY}")
        if target not in HOT_SWAP_TARGETS:
            raise ValueError(f"hot-swap target must be one of {', '.join(HOT_SWAP_TARGETS)}")
        before_status = await self._status()
        before = await self._inspect()
        before_digest = _active_digest(before)

        staged = await self.request("stage_payload", {"local_path": str(path), "name": MITHRIL_PAYLOAD})
        if staged.get("ok") is not True:
            return {"ok": False, "operation": "swap", "stage": staged, "before_digest": before_digest}
        manifest = staged.get("manifest")
        if not isinstance(manifest, dict):
            raise RuntimeError("stage_payload omitted the Mithril manifest")
        after_digest = _validate_mithril_manifest(manifest)
        after = await self._inspect()
        if _active_digest(after) != after_digest:
            raise RuntimeError("device active pointer does not match the newly staged Mithril digest")

        transaction = self._transaction(
            operation="swap",
            before_digest=before_digest,
            after_digest=after_digest,
            profile=profile,
            target=target,
        )
        self.ledger.append(transaction)
        smoke = await self._smoke(
            profile=profile,
            target=target,
            timeout=timeout,
            allow_repeat_failure=allow_repeat_failure,
            retry_reason=retry_reason,
        )
        state = smoke.get("state") if isinstance(smoke.get("state"), dict) else {}
        transaction["run_id"] = state.get("run_id")
        transaction["process_generation"] = state.get("process_generation")
        old_generation = ((before_status.get("state") or {}).get("process_generation") if isinstance(before_status, dict) else None)
        if old_generation and transaction["process_generation"] == old_generation:
            smoke = {
                "ok": False,
                "failure": "JIT_VERIFICATION_FAILURE",
                "detail": "cold-restart invariant failed: process_generation did not change",
                "state": state,
            }
        consumer = None
        if smoke.get("ok") is True:
            consumer = await self._verify_consumer(smoke, after_digest)
        transaction["consumer"] = consumer
        transaction["finished_at"] = time()
        transaction["status"] = "passed" if smoke.get("ok") is True else "failed"
        transaction["failure"] = ((smoke.get("state") or {}).get("failure") if isinstance(smoke.get("state"), dict) else smoke.get("failure"))
        self.ledger.update_last(transaction)
        return {
            "ok": smoke.get("ok") is True,
            "operation": "swap",
            "before_digest": before_digest,
            "after_digest": after_digest,
            "transaction": transaction,
            "stage": staged,
            "smoke": smoke,
            "rollback": {
                "available": before_digest != after_digest,
                "digest": before_digest,
                "command": f"./tools/amethystctl mithril rollback --profile {profile} --target {target}",
            },
        }

    async def rollback(
        self,
        *,
        profile: str,
        target: str = "RENDERER_READY",
        timeout: float = 180,
        digest: str | None = None,
        allow_repeat_failure: bool = False,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        if target not in HOT_SWAP_TARGETS:
            raise ValueError(f"hot-swap target must be one of {', '.join(HOT_SWAP_TARGETS)}")
        before_status = await self._status()
        before = await self._inspect()
        before_digest = _active_digest(before)
        target_digest = (digest or self.ledger.rollback_target(before_digest)).lower()
        if target_digest != "bundled" and not _DIGEST.fullmatch(target_digest):
            raise ValueError("rollback digest must be 'bundled' or a 64-character SHA-256 digest")
        if target_digest == before_digest:
            raise ValueError("rollback target is already active")

        if target_digest == "bundled":
            switched = await self.request("clear_payload", {"name": MITHRIL_PAYLOAD})
            if switched.get("ok") is not True:
                return {"ok": False, "operation": "rollback", "switch": switched}
        else:
            switched = await activate_staged_mithril(self.client, target_digest)
            host_candidate = {
                "version": 1,
                "name": MITHRIL_PAYLOAD,
                "active": True,
                "digest": target_digest,
                "files": switched["manifest"].get("files", []),
                "source": "reactivated_existing_stage",
                "staged_at": None,
            }
            self.store.save_candidate(MITHRIL_PAYLOAD, host_candidate)
            self.store.reset_retry_guard(f"Mithril rollback activated {target_digest}")

        after = await self._inspect()
        if _active_digest(after) != target_digest:
            raise RuntimeError("rollback did not produce the requested device-active Mithril digest")
        transaction = self._transaction(
            operation="rollback",
            before_digest=before_digest,
            after_digest=target_digest,
            profile=profile,
            target=target,
        )
        self.ledger.append(transaction)
        smoke = await self._smoke(
            profile=profile,
            target=target,
            timeout=timeout,
            allow_repeat_failure=allow_repeat_failure,
            retry_reason=retry_reason,
        )
        state = smoke.get("state") if isinstance(smoke.get("state"), dict) else {}
        transaction["run_id"] = state.get("run_id")
        transaction["process_generation"] = state.get("process_generation")
        old_generation = ((before_status.get("state") or {}).get("process_generation") if isinstance(before_status, dict) else None)
        if old_generation and transaction["process_generation"] == old_generation:
            smoke = {
                "ok": False,
                "failure": "JIT_VERIFICATION_FAILURE",
                "detail": "cold-restart invariant failed: process_generation did not change",
                "state": state,
            }
        consumer = None
        if smoke.get("ok") is True:
            consumer = await self._verify_consumer(smoke, target_digest)
        transaction["consumer"] = consumer
        transaction["finished_at"] = time()
        transaction["status"] = "passed" if smoke.get("ok") is True else "failed"
        transaction["failure"] = ((smoke.get("state") or {}).get("failure") if isinstance(smoke.get("state"), dict) else smoke.get("failure"))
        self.ledger.update_last(transaction)
        return {
            "ok": smoke.get("ok") is True,
            "operation": "rollback",
            "before_digest": before_digest,
            "after_digest": target_digest,
            "transaction": transaction,
            "switch": switched,
            "smoke": smoke,
        }

    async def status(self) -> dict[str, Any]:
        active = await self._inspect()
        return {
            "ok": True,
            "operation": "status",
            "active_digest": _active_digest(active),
            "active": active,
            "ledger": self.ledger.load(),
        }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="amethystctl mithril",
        description="Transactional Mithril dylib hot deployment with Amethyst cold restart and LWJGL consumer proof.",
    )
    sub = root.add_subparsers(dest="action", required=True)
    swap = sub.add_parser("swap", help="stage a signed libmithril.dylib, cold-restart Amethyst and run an authoritative smoke gate")
    swap.add_argument("path")
    swap.add_argument("--profile", required=True)
    swap.add_argument("--target", choices=HOT_SWAP_TARGETS, default="RENDERER_READY")
    swap.add_argument("--timeout", type=float, default=180)
    swap.add_argument("--allow-repeat-failure", action="store_true")
    swap.add_argument("--retry-reason")

    rollback = sub.add_parser("rollback", help="reactivate a previous staged digest (or bundled Mithril), cold-restart and revalidate")
    rollback.add_argument("--profile", required=True)
    rollback.add_argument("--target", choices=HOT_SWAP_TARGETS, default="RENDERER_READY")
    rollback.add_argument("--timeout", type=float, default=180)
    rollback.add_argument("--digest", help="explicit staged SHA-256 digest or 'bundled'; default is the previous digest in the ledger")
    rollback.add_argument("--allow-repeat-failure", action="store_true")
    rollback.add_argument("--retry-reason")
    sub.add_parser("status", help="show the device-active Mithril digest and bounded hot-swap ledger")
    return root


def _exit(value: dict[str, Any], store: StateStore) -> int:
    value = bounded_result(value, store.root)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0 if value.get("ok") else 5


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = StateStore()
    try:
        # Reuse the canonical daemon lifecycle instead of creating a second
        # Supervisor/JIT owner. Import lazily to avoid a module cycle at startup.
        from tools.amethystctl import call, ensure_daemon

        ensure_daemon(store, start=True)
        config = store.load_config()
        device_udid = os.environ.get("AMETHYST_DEVICE_UDID") or config.get("device_udid")
        bundle_id = os.environ.get("AMETHYST_BUNDLE_ID") or config.get("bundle_id") or "org.angelauramc.amethyst"
        if not device_udid:
            return _exit({"ok": False, "failure": "DEVICE_NOT_FOUND", "detail": "configure a device before Mithril hot swap"}, store)
        client = AgentContainerClient(device_udid, bundle_id)
        manager = MithrilHotSwapManager(store, call, client)
        if args.action == "status":
            return _exit(asyncio.run(manager.status()), store)
        if bool(args.allow_repeat_failure) != bool(args.retry_reason):
            raise ValueError("--allow-repeat-failure and --retry-reason must be supplied together")
        common = {
            "profile": args.profile,
            "target": args.target,
            "timeout": args.timeout,
            "allow_repeat_failure": args.allow_repeat_failure,
            "retry_reason": args.retry_reason,
        }
        if args.action == "swap":
            return _exit(asyncio.run(manager.swap(args.path, **common)), store)
        if args.action == "rollback":
            return _exit(asyncio.run(manager.rollback(digest=args.digest, **common)), store)
        return _exit({"ok": False, "error": "unsupported_action"}, store)
    except Exception as exc:
        print(f"amethystctl mithril: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _exit({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, store)


if __name__ == "__main__":
    raise SystemExit(main())
