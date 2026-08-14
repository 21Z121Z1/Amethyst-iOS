from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import sys
import zipfile
from pathlib import Path
from time import monotonic, time
from typing import Any
from uuid import uuid4

from .container_io import AgentContainerClient
from .device import DeviceController
from .jit import JITAttachError, JITSession, JITVerificationError, StaleDebugserverError
from .model import FailureClass, RunState, Stage
from .store import StateStore


EVENT_TO_STAGE = {
    "jvm_starting": Stage.JVM_STARTING,
    "jvm_ready": Stage.JVM_READY,
    "minecraft_bootstrap": Stage.MC_BOOTSTRAP,
    "renderer_loading": Stage.RENDERER_LOADING,
    "renderer_ready": Stage.RENDERER_READY,
    "menu_ready": Stage.MENU_READY,
    "world_loading": Stage.WORLD_LOADING,
    "world_ready": Stage.WORLD_READY,
    "chunks_stable": Stage.CHUNKS_STABLE,
    "benchmark_warmup_started": Stage.WARMUP,
    "benchmark_started": Stage.MEASURING,
}

APP_EXEC_MARKERS = (
    "[JIT26] Got JIT mapping",
    "[JIT26] mapping at RW=",
)
APP_DYLD_MARKERS = (
    "[DyldLVBypass] hook dyld_mmap succeed!",
    "[DyldLVBypass] hook dyld_fcntl succeed!",
)


class Supervisor:
    def __init__(
        self,
        store: StateStore,
        *,
        device_udid: str | None = None,
        bundle_id: str | None = None,
    ) -> None:
        self.store = store
        self.device_udid = device_udid or os.environ.get("AMETHYST_DEVICE_UDID")
        self.bundle_id = bundle_id or os.environ.get("AMETHYST_BUNDLE_ID", "org.angelauramc.amethyst")
        self.device = DeviceController(self.device_udid)
        self.jit: JITSession | None = None

    def doctor(self) -> dict[str, Any]:
        device = self.device.doctor()
        pmd3 = device["pymobiledevice3"]
        bundled_processor = Path(__file__).with_name("universal_jit26_processor.py")
        python_supported = (3, 12) <= sys.version_info[:2] < (3, 14)
        return {
            "ok": bool(
                python_supported
                and pmd3["available"]
                and pmd3["python_api_available"]
                and device["devicectl"]["available"]
                and pmd3.get("ok", False)
            ),
            "bundle_id": self.bundle_id,
            "python": {
                "executable": sys.executable,
                "version": sys.version.split()[0],
                "supported": python_supported,
                "supported_range": ">=3.12,<3.14",
            },
            "daemon_runtime": {"root": str(self.store.runtime_root), "socket": str(self.store.socket_path)},
            "device": device,
            "jit_processor": {
                "available": bundled_processor.is_file() or bool(os.environ.get("AMETHYST_JIT_PROCESSOR")),
                "source": "override" if os.environ.get("AMETHYST_JIT_PROCESSOR") else "bundled",
                "path": str(bundled_processor),
            },
            "agent_home": str(self.store.root),
        }

    def status(self) -> dict[str, Any]:
        state = self.store.load()
        return {"ok": True, "state": state.to_dict() if state else None}

    def _new_state(
        self,
        run_id: str | None = None,
        *,
        candidate: dict[str, Any] | None = None,
        attempt_signature: str | None = None,
    ) -> RunState:
        state = RunState(
            run_id=run_id or f"run-{uuid4().hex[:16]}",
            bundle_id=self.bundle_id,
            device_udid=self.device_udid,
            candidate=candidate or {},
            attempt_signature=attempt_signature,
        )
        self.store.save(state)
        self.store.append_event(
            state.run_id,
            "run_created",
            bundle_id=self.bundle_id,
            candidate=state.candidate,
            attempt_signature=attempt_signature,
        )
        return state

    async def _agent_request(
        self,
        client: AgentContainerClient,
        state: RunState,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        guard_generation: bool = True,
        guard_session: bool = True,
        timeout: float = 20,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "protocol": "amethyst-agent/v2",
            "request_id": uuid4().hex,
            "run_id": state.run_id,
            "action": action,
            "params": params or {},
        }
        if guard_generation and state.process_generation:
            request["if_process_generation"] = state.process_generation
        if guard_session and state.game_session_generation:
            request["if_game_session_generation"] = state.game_session_generation
        self.store.append_event(state.run_id, "request_submitted", action=action, request_id=request["request_id"])
        response = await client.request(request, timeout=timeout)
        self.store.append_event(
            state.run_id,
            "response_observed",
            action=action,
            request_id=request["request_id"],
            response_state=response.get("state"),
            ok=response.get("ok"),
        )
        return response

    @staticmethod
    def _observe_identity(state: RunState, response: dict[str, Any]) -> bool:
        pid = response.get("process_id")
        generation = response.get("process_generation")
        if not isinstance(pid, int) or not isinstance(generation, str) or not generation:
            raise RuntimeError("Agent v2 response did not provide process_id/process_generation")
        changed = state.observe_process(pid, generation)
        if state.game_session_generation:
            observed_session = response.get("game_session_generation")
            if observed_session != state.game_session_generation:
                raise RuntimeError(
                    f"Agent game session mismatch: expected {state.game_session_generation}, observed {observed_session!r}"
                )
        return changed

    @staticmethod
    def event_key(event: dict[str, Any]) -> tuple[str, str] | tuple[str, str, int] | None:
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            return ("event_id", event_id)
        generation = event.get("process_generation")
        seq = event.get("seq")
        if isinstance(generation, str) and generation and isinstance(seq, int):
            return ("generation_seq", generation, seq)
        return None

    @staticmethod
    def _local_app_metadata(path: Path) -> dict[str, str]:
        if path.is_dir() and path.suffix == ".app":
            with (path / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
        elif path.is_file() and path.suffix.lower() == ".ipa":
            with zipfile.ZipFile(path) as archive:
                candidates = [
                    name
                    for name in archive.namelist()
                    if name.startswith("Payload/") and name.count("/") == 2 and name.endswith(".app/Info.plist")
                ]
                if len(candidates) != 1:
                    raise ValueError(f"expected exactly one Payload/*.app/Info.plist, found {len(candidates)}")
                info = plistlib.loads(archive.read(candidates[0]))
        else:
            raise ValueError("deploy expects a .app directory or .ipa file")
        return {
            key: str(info[key])
            for key in ("CFBundleIdentifier", "CFBundleVersion", "CFBundleShortVersionString")
            if key in info
        }

    @staticmethod
    def _parse_app_query(stdout: str, bundle_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        value = payload.get(bundle_id) if isinstance(payload, dict) else None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _version_matches(local: dict[str, str], observed: dict[str, Any] | None) -> bool:
        if not observed:
            return False
        for key in ("CFBundleVersion", "CFBundleShortVersionString"):
            expected = local.get(key)
            if expected is not None and str(observed.get(key)) != expected:
                return False
        return True

    async def deploy(self, app_path: str) -> dict[str, Any]:
        path = Path(app_path).expanduser().resolve()
        local = self._local_app_metadata(path)
        if local.get("CFBundleIdentifier") != self.bundle_id:
            return {
                "ok": False,
                "failure": FailureClass.SIGNING_FAILURE.value,
                "detail": f"bundle mismatch: harness targets {self.bundle_id}, payload is {local.get('CFBundleIdentifier')}",
                "local": local,
            }
        if not self.device_udid:
            return {"ok": False, "failure": FailureClass.DEVICE_NOT_FOUND.value, "detail": "device UDID is required"}

        before_result = await asyncio.to_thread(self.device.query_app, self.bundle_id)
        before = self._parse_app_query(before_result.stdout, self.bundle_id) if before_result.ok else None
        before_matches = self._version_matches(local, before)
        install = await asyncio.to_thread(self.device.install_app, path)
        after_result = await asyncio.to_thread(self.device.query_app, self.bundle_id)
        after = self._parse_app_query(after_result.stdout, self.bundle_id) if after_result.ok else None
        after_matches = self._version_matches(local, after)
        evidence = {
            "local": local,
            "before": before,
            "after": after,
            "install": {
                "returncode": install.returncode,
                "timed_out": install.timed_out,
                "stdout": install.stdout,
                "stderr": install.stderr,
            },
        }

        if install.ok and after_matches:
            return {"ok": True, "observed": True, "evidence": evidence}
        if install.timed_out:
            if not before_matches and after_matches:
                return {"ok": True, "observed": True, "reconciled_after_timeout": True, "evidence": evidence}
            return {
                "ok": False,
                "failure": FailureClass.INSTALL_UNKNOWN.value,
                "detail": "install timed out and post-install metadata cannot prove a state change",
                "evidence": evidence,
            }

        signature_text = f"{install.stdout}\n{install.stderr}".lower()
        signature_markers = ("0xe8008014", "code signature", "provision", "applicationverificationfailed", "integrity")
        failure = FailureClass.SIGNING_FAILURE if any(marker in signature_text for marker in signature_markers) else FailureClass.INSTALL_UNKNOWN
        return {
            "ok": False,
            "failure": failure.value,
            "detail": "install command did not produce a verified installed payload",
            "evidence": evidence,
        }

    @staticmethod
    def _new_log_slice(raw: bytes | None, baseline: int) -> str:
        if raw is None:
            return ""
        start = baseline if len(raw) >= baseline else 0
        return raw[start:].decode("utf-8", errors="replace")

    async def _wait_app_jit_proof(
        self,
        client: AgentContainerClient,
        *,
        baseline: int,
        require_dynamic_library_load: bool,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = monotonic() + timeout
        last_text = ""
        while monotonic() < deadline:
            raw = await client.read_optional("latestlog.txt")
            last_text = self._new_log_slice(raw, baseline)
            exec_ready = all(marker in last_text for marker in APP_EXEC_MARKERS)
            dyld_ready = all(marker in last_text for marker in APP_DYLD_MARKERS)
            if exec_ready and (dyld_ready or not require_dynamic_library_load):
                proof_lines = [
                    line
                    for line in last_text.splitlines()
                    if any(marker in line for marker in (*APP_EXEC_MARKERS, *APP_DYLD_MARKERS))
                ]
                return {
                    "exec_ready": exec_ready,
                    "dynamic_library_load_ready": dyld_ready,
                    "markers": list(APP_EXEC_MARKERS)
                    + (list(APP_DYLD_MARKERS) if require_dynamic_library_load else []),
                    "proof_lines": proof_lines[-8:],
                }
            await asyncio.sleep(0.1)
        exec_ready = all(marker in last_text for marker in APP_EXEC_MARKERS)
        if not exec_ready:
            raise JITVerificationError(
                "host handled UniversalJIT26 but Amethyst mapping proof was not observed in new latestlog bytes"
            )
        raise JITVerificationError(
            "Amethyst executable mapping succeeded but required DyldLVBypass hook proof was not observed"
        )

    async def run_smoke(
        self,
        *,
        profile: str,
        target: str = "WORLD_READY",
        timeout: float = 180,
        require_dynamic_library_load: bool = True,
        run_id: str | None = None,
        candidate_name: str = "mithril",
        allow_repeat_failure: bool = False,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        wanted = Stage(target.upper())
        if bool(allow_repeat_failure) != bool(retry_reason):
            raise ValueError("--allow-repeat-failure and --retry-reason must be supplied together so the diagnostic deviation is auditable")
        candidate = self.store.load_candidate(candidate_name) or {
            "name": candidate_name,
            "active": False,
            "digest": "bundled",
            "source": "bundled_or_untracked",
        }
        candidate_digest = str(candidate.get("digest") or "bundled")
        signature = self.store.attempt_signature(
            candidate_digest=candidate_digest,
            profile=profile,
            target=wanted.value,
            require_dynamic_library_load=require_dynamic_library_load,
        )
        if retry_reason:
            self.store.reset_retry_guard(f"explicit retry: {retry_reason}")
        state = self._new_state(run_id, candidate=candidate, attempt_signature=signature)
        # Device-side active payload is authoritative. Do not reject here from
        # potentially stale host candidate metadata; reconcile after Agent v2
        # is reachable, then apply the retry guard before profile/JIT work.
        state.evidence["host_retry_status"] = self.store.retry_status(signature)
        try:
            doctor = self.doctor()
            pmd3 = doctor["device"]["pymobiledevice3"]
            if not pmd3["available"] or not doctor["device"]["devicectl"]["available"]:
                state.fail(FailureClass.DEVICE_NOT_FOUND, "required host device tooling is unavailable", blocked=True)
                return self._finish(state)
            if not pmd3["python_api_available"]:
                state.fail(
                    FailureClass.AGENT_UNREACHABLE,
                    "amethystd Python environment lacks pymobiledevice3; run tools/bootstrap-agent and use tools/amethystctl",
                    blocked=True,
                )
                return self._finish(state)
            if not pmd3.get("ok", False):
                state.fail(FailureClass.DEVICE_NOT_FOUND, "pymobiledevice3 usbmux probe did not succeed", blocked=True)
                return self._finish(state)
            if not self.device_udid:
                state.fail(FailureClass.DEVICE_NOT_FOUND, "explicit device UDID is required for unattended state changes", blocked=True)
                return self._finish(state)
            state.transition(
                Stage.DEVICE_PREFLIGHT_OK,
                doctor=doctor,
                host_retry_status=state.evidence.get("host_retry_status"),
            )
            self.store.save(state)

            launch_app = await asyncio.to_thread(self.device.launch_app, self.bundle_id)
            self.store.append_event(
                state.run_id,
                "app_launch_command",
                returncode=launch_app.returncode,
                stdout=launch_app.stdout,
                stderr=launch_app.stderr,
                timed_out=launch_app.timed_out,
            )
            if not launch_app.ok:
                state.fail(FailureClass.AGENT_UNREACHABLE, f"devicectl launch failed: {launch_app.stderr.strip()}")
                return self._finish(state)
            state.transition(Stage.APP_LAUNCHED)
            self.store.save(state)

            client = AgentContainerClient(self.device_udid, self.bundle_id)
            status = await self._agent_request(client, state, "status", guard_generation=False, guard_session=False, timeout=30)
            if not status.get("ok"):
                state.fail(FailureClass.AGENT_UNREACHABLE, json.dumps(status, sort_keys=True))
                return self._finish(state)
            invalidated = self._observe_identity(state, status)
            state.app_state = status.get("state")
            state.profile = status.get("profile") or {}
            state.transition(Stage.AGENT_READY, process_invalidated=invalidated)

            # Reconcile the candidate against the device before any expensive JIT/game work.
            # The device's active pointer is authoritative when a prior tool or run changed it.
            device_candidate = await client.inspect_payload(candidate_name)
            if device_candidate.get("active"):
                manifest = device_candidate.get("manifest")
                if not isinstance(manifest, dict) or not isinstance(manifest.get("digest"), str):
                    state.fail(
                        FailureClass.HOT_PAYLOAD_PROVENANCE_MISMATCH,
                        f"device reports active payload {candidate_name!r} without a valid manifest/digest",
                    )
                    return self._finish(state)
                actual_candidate = {
                    **state.candidate,
                    "version": 1,
                    "name": candidate_name,
                    "active": True,
                    "digest": manifest["digest"],
                    "files": manifest.get("files", []),
                    "source": state.candidate.get("source", "device_active_pointer"),
                }
            else:
                actual_candidate = {
                    **state.candidate,
                    "name": candidate_name,
                    "active": False,
                    "digest": "bundled",
                    "source": "bundled_observed_on_device",
                }
            state.candidate = actual_candidate
            candidate = actual_candidate
            candidate_digest = str(candidate.get("digest") or "bundled")
            actual_signature = self.store.attempt_signature(
                candidate_digest=candidate_digest,
                profile=profile,
                target=wanted.value,
                require_dynamic_library_load=require_dynamic_library_load,
            )
            state.attempt_signature = actual_signature
            retry = self.store.retry_status(actual_signature)
            state.evidence["candidate_reconciled"] = {"device": device_candidate, "candidate": candidate}
            if retry["blocked"] and not allow_repeat_failure:
                state.fail(
                    FailureClass.RETRY_REQUIRES_CHANGED_EVIDENCE,
                    "device-confirmed candidate/profile produced the same failure twice; change evidence or run an explicit recovery",
                    blocked=True,
                )
                state.evidence["retry_guard"] = retry
                return self._finish(state)
            self.store.save_candidate(candidate_name, candidate)
            self.store.save(state)

            profile_response = await self._agent_request(
                client, state, "profile/set", {"profile": profile}, guard_session=False, timeout=30
            )
            if not profile_response.get("ok"):
                state.fail(FailureClass.RUNTIME_ABI_PRECHECK_FAILED, json.dumps(profile_response, sort_keys=True))
                return self._finish(state)
            if self._observe_identity(state, profile_response):
                state.fail(FailureClass.JIT_VERIFICATION_FAILURE, "process generation changed during profile transaction")
                return self._finish(state)
            state.profile = profile_response.get("profile") or state.profile
            state.transition(Stage.PROFILE_READY)

            game_session_generation = f"session-{uuid4().hex[:20]}"
            state.begin_game_session(game_session_generation)
            state.jit_attach_generation = f"jit-{uuid4().hex[:20]}"
            self.store.save(state)
            await client.prepare_lab_run(state.run_id, game_session_generation)
            self.store.append_event(
                state.run_id,
                "lab_run_prepared",
                game_session_generation=game_session_generation,
                jit_attach_generation=state.jit_attach_generation,
            )
            latest_before = await client.read_optional("latestlog.txt")
            latest_baseline = len(latest_before or b"")

            self.jit = JITSession(self.device, self.store.artifact_dir(state.run_id))
            state.transition(Stage.JIT_ATTACHING)
            self.store.save(state)
            try:
                attach = await asyncio.to_thread(
                    self.jit.start,
                    pid=state.pid,
                    process_generation=state.process_generation,
                    game_session_generation=game_session_generation,
                    run_id=state.run_id,
                    timeout=min(timeout, 30),
                )
            except StaleDebugserverError as exc:
                state.fail(FailureClass.STALE_DEBUGSERVER, str(exc))
                return self._finish(state)
            except (JITAttachError, TimeoutError) as exc:
                state.fail(FailureClass.JIT_ATTACH_FAILURE, str(exc))
                return self._finish(state)
            state.transition(Stage.JIT_HANDSHAKE_OK, jit_attach=attach.to_dict())
            self.store.save(state)

            launch_response = await self._agent_request(
                client,
                state,
                "launch",
                {"game_session_generation": game_session_generation},
                guard_session=False,
                timeout=30,
            )
            if not launch_response.get("ok"):
                state.fail(FailureClass.RUNTIME_ABI_PRECHECK_FAILED, json.dumps(launch_response, sort_keys=True))
                return self._finish(state)
            if self._observe_identity(state, launch_response):
                state.fail(FailureClass.JIT_VERIFICATION_FAILURE, "process generation changed while accepting launch")
                return self._finish(state)
            state.transition(Stage.LAUNCH_ACCEPTED)
            self.store.save(state)

            if wanted == Stage.LAUNCH_ACCEPTED:
                state.transition(Stage.PASS, target=wanted.value)
                return self._finish(state)

            try:
                host_jit = await asyncio.to_thread(
                    self.jit.wait_for_host_exec,
                    require_keep_attached=require_dynamic_library_load,
                    timeout=min(timeout, 90),
                )
                app_jit = await self._wait_app_jit_proof(
                    client,
                    baseline=latest_baseline,
                    require_dynamic_library_load=require_dynamic_library_load,
                    timeout=min(timeout, 30),
                )
            except StaleDebugserverError as exc:
                state.fail(FailureClass.STALE_DEBUGSERVER, str(exc))
                return self._finish(state)
            except JITVerificationError as exc:
                failure = (
                    FailureClass.DYLD_VALIDATION_FAILURE
                    if "DyldLVBypass" in str(exc)
                    else FailureClass.JIT_VERIFICATION_FAILURE
                )
                state.fail(failure, str(exc))
                return self._finish(state)

            identity_probe = await self._agent_request(client, state, "status", timeout=10)
            if not identity_probe.get("ok") or self._observe_identity(state, identity_probe):
                state.fail(
                    FailureClass.JIT_VERIFICATION_FAILURE,
                    "Amethyst process identity changed while establishing JIT proof",
                )
                return self._finish(state)
            state.mark_jit(
                exec_ready=True,
                dynamic_library_load_ready=bool(app_jit["dynamic_library_load_ready"]),
                evidence={"jit_host": host_jit.to_dict(), "jit_app": app_jit},
            )
            self.store.save(state)

            deadline = monotonic() + timeout
            seen_events: set[tuple] = set()
            while monotonic() < deadline:
                app_events = await client.read_events(state.run_id)
                lab_events = await client.read_lab_events(state.run_id, game_session_generation)
                events = [("app", event) for event in app_events] + [("lab", event) for event in lab_events]
                for source, event in events:
                    if source == "lab" and event.get("protocol") != "amethyst-lab/v1":
                        continue
                    key = self.event_key(event)
                    if key is not None and key in seen_events:
                        continue
                    if key is not None:
                        seen_events.add(key)

                    event_generation = event.get("process_generation")
                    event_pid = event.get("process_id")
                    if source == "app" and isinstance(event_generation, str) and isinstance(event_pid, int):
                        if state.observe_process(event_pid, event_generation):
                            self.store.append_event(
                                state.run_id,
                                "process_generation_changed",
                                process_id=event_pid,
                                process_generation=event_generation,
                            )
                            if self.jit:
                                self.jit.close()
                            state.fail(
                                FailureClass.JIT_VERIFICATION_FAILURE,
                                "Amethyst process generation changed after launch; JIT proof was invalidated",
                            )
                            return self._finish(state)

                    event_name = event.get("event") or event.get("stage")
                    mapped = EVENT_TO_STAGE.get(event_name)
                    if mapped and source == "app" and event.get("game_session_generation") != game_session_generation:
                        self.store.append_event(
                            state.run_id,
                            "stale_session_event_ignored",
                            event=event_name,
                            observed_session=event.get("game_session_generation"),
                            expected_session=game_session_generation,
                        )
                        continue
                    if mapped == Stage.RENDERER_READY and candidate.get("active"):
                        provenance_ok = event.get("provenance_ok") is True
                        observed_digest = event.get("candidate_digest")
                        if not provenance_ok or observed_digest != candidate_digest:
                            state.fail(
                                FailureClass.HOT_PAYLOAD_PROVENANCE_MISMATCH,
                                f"renderer_ready provenance did not match active candidate {candidate_digest}: "
                                f"ok={provenance_ok} observed_digest={observed_digest!r}",
                            )
                            state.evidence["renderer_provenance_event"] = event
                            return self._finish(state)
                    if mapped:
                        state.transition(mapped, last_event_source=source, last_app_event=event)
                        self.store.save(state)
                        if mapped == wanted:
                            state.transition(Stage.PASS, target=wanted.value)
                            return self._finish(state)
                    if event_name in {"crashed", "failed"}:
                        failure_name = event.get("failure_class")
                        try:
                            classified = FailureClass(failure_name) if failure_name else FailureClass.AMETHYST_CRASH
                        except ValueError:
                            classified = FailureClass.AMETHYST_CRASH
                        state.fail(classified, json.dumps(event, sort_keys=True))
                        return self._finish(state)
                await asyncio.sleep(0.25)
            timeout_class = (
                FailureClass.WORLD_LOAD_TIMEOUT
                if wanted in {Stage.WORLD_READY, Stage.CHUNKS_STABLE}
                else FailureClass.MC_READY_TIMEOUT
            )
            state.fail(timeout_class, f"target {wanted.value} was not observed before timeout")
            return self._finish(state)
        except TimeoutError as exc:
            state.fail(FailureClass.AGENT_UNREACHABLE, str(exc))
            return self._finish(state)
        except Exception as exc:
            state.fail(FailureClass.UNKNOWN, f"{type(exc).__name__}: {exc}")
            return self._finish(state)

    async def stage_payload(self, local_path: str, name: str) -> dict[str, Any]:
        if not self.device_udid:
            return {"ok": False, "failure": FailureClass.DEVICE_NOT_FOUND.value, "detail": "device UDID is required"}
        if not self.device.pmd3_python_api_available():
            return {
                "ok": False,
                "failure": FailureClass.AGENT_UNREACHABLE.value,
                "detail": "amethystd Python environment lacks pymobiledevice3",
            }
        path = Path(local_path).expanduser().resolve()
        client = AgentContainerClient(self.device_udid, self.bundle_id)
        manifest = await client.stage_payload(path, name)
        embedded_commit = None
        if path.is_file():
            try:
                match = re.search(rb"Build commit:\s*([0-9a-fA-F]{7,40})", path.read_bytes())
                if match:
                    embedded_commit = match.group(1).decode().lower()
            except OSError:
                pass
        candidate = {
            "version": 1,
            "name": name,
            "active": True,
            "digest": manifest["digest"],
            "files": manifest["files"],
            "source_path": str(path),
            "embedded_build_commit": embedded_commit,
            "staged_at": time(),
        }
        self.store.save_candidate(name, candidate)
        self.store.reset_retry_guard(f"payload {name} changed to {manifest['digest']}")
        return {"ok": True, "manifest": manifest, "candidate": candidate}

    async def inspect_payload(self, name: str) -> dict[str, Any]:
        if not self.device_udid:
            return {"ok": False, "failure": FailureClass.DEVICE_NOT_FOUND.value, "detail": "device UDID is required"}
        client = AgentContainerClient(self.device_udid, self.bundle_id)
        device = await client.inspect_payload(name)
        host = self.store.load_candidate(name)
        if device.get("active") and isinstance(device.get("manifest"), dict):
            digest = device["manifest"].get("digest")
            if host is None or host.get("digest") != digest or not host.get("active"):
                host = {
                    "version": 1,
                    "name": name,
                    "active": True,
                    "digest": digest,
                    "files": device["manifest"].get("files", []),
                    "source": "reconciled_from_device",
                    "staged_at": None,
                }
                self.store.save_candidate(name, host)
        return {"ok": True, "name": name, "device": device, "host": host}

    async def clear_payload(self, name: str) -> dict[str, Any]:
        if not self.device_udid:
            return {"ok": False, "failure": FailureClass.DEVICE_NOT_FOUND.value, "detail": "device UDID is required"}
        client = AgentContainerClient(self.device_udid, self.bundle_id)
        result = await client.clear_payload(name)
        host = self.store.load_candidate(name) or {"version": 1, "name": name}
        host.update({"active": False, "cleared_at": time()})
        self.store.save_candidate(name, host)
        self.store.reset_retry_guard(f"payload {name} cleared")
        result["host"] = host
        return result

    async def collect(self, run_id: str, *, include_crashes: bool = False, screenshot: bool = True) -> dict[str, Any]:
        artifact_dir = self.store.artifact_dir(run_id)
        result: dict[str, Any] = {"ok": True, "run_id": run_id, "artifact_dir": str(artifact_dir), "items": {}}
        state = self.store.load()
        if state and state.run_id == run_id:
            (artifact_dir / "manifest.json").write_text(
                json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            result["items"]["manifest"] = "manifest.json"

        if not self.device_udid or not self.device.pmd3_python_api_available():
            result["ok"] = False
            result["detail"] = "device/container transport unavailable; host artifacts remain available"
            return result

        client = AgentContainerClient(self.device_udid, self.bundle_id)
        for remote, local_name in (
            (f"agent-events/{run_id}.jsonl", "agent-events.jsonl"),
            (f"agent-lab/{run_id}.jsonl", "lab-events.jsonl"),
            ("latestlog.txt", "amethyst-latestlog.txt"),
        ):
            try:
                data = await client.read_optional(remote)
                if data is not None:
                    (artifact_dir / local_name).write_bytes(data)
                    result["items"][local_name] = {"bytes": len(data)}
            except Exception as exc:
                result["items"][local_name] = {"error": f"{type(exc).__name__}: {exc}"}

        if screenshot:
            try:
                shot = await asyncio.to_thread(self.device.screenshot, artifact_dir / "screen.png")
                result["items"]["screen.png"] = {
                    "ok": shot.ok,
                    "returncode": shot.returncode,
                    "stderr": shot.stderr,
                }
            except Exception as exc:
                result["items"]["screen.png"] = {"error": f"{type(exc).__name__}: {exc}"}

        if include_crashes:
            try:
                crash = await asyncio.to_thread(self.device.pull_crashes, artifact_dir / "crash")
                result["items"]["crash"] = {
                    "ok": crash.ok,
                    "returncode": crash.returncode,
                    "stderr": crash.stderr,
                }
            except Exception as exc:
                result["items"]["crash"] = {"error": f"{type(exc).__name__}: {exc}"}
        return result

    def _finish(self, state: RunState) -> dict[str, Any]:
        if (
            state.attempt_signature
            and state.failure
            and state.failure_fingerprint
            and state.failure not in {FailureClass.RETRY_REQUIRES_CHANGED_EVIDENCE}
            and state.stage != Stage.BLOCKED
        ):
            self.store.record_failure(
                state.attempt_signature,
                failure=state.failure.value,
                fingerprint=state.failure_fingerprint,
            )
        self.store.save(state)
        self.store.append_event(
            state.run_id,
            "run_finished",
            stage=state.stage.value,
            failure=state.failure.value if state.failure else None,
            failure_fingerprint=state.failure_fingerprint,
            game_session_generation=state.game_session_generation,
            candidate=state.candidate,
        )
        return {"ok": state.stage == Stage.PASS, "state": state.to_dict()}

    def close(self) -> None:
        if self.jit:
            self.jit.close()
            self.jit = None
