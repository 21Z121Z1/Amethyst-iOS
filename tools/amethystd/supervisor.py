from __future__ import annotations

import asyncio
import json
import os
from time import monotonic
from typing import Any
from uuid import uuid4

from .container_io import AgentContainerClient
from .device import DeviceController
from .jit import (
    DyldVerificationError,
    JITProcessorUnconfigured,
    JITSession,
    JITVerificationError,
    StaleDebugserverError,
)
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
    "benchmark_warmup_started": Stage.WARMUP,
    "benchmark_started": Stage.MEASURING,
}


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
        return {
            "ok": bool(
                pmd3["available"]
                and pmd3["python_api_available"]
                and device["devicectl"]["available"]
                and pmd3.get("ok", False)
            ),
            "bundle_id": self.bundle_id,
            "device": device,
            "jit_processor_configured": bool(os.environ.get("AMETHYST_JIT_PROCESSOR")),
            "agent_home": str(self.store.root),
        }

    def status(self) -> dict[str, Any]:
        state = self.store.load()
        return {"ok": True, "state": state.to_dict() if state else None}

    def _new_state(self, run_id: str | None = None) -> RunState:
        state = RunState(
            run_id=run_id or f"run-{uuid4().hex[:16]}",
            bundle_id=self.bundle_id,
            device_udid=self.device_udid,
        )
        self.store.save(state)
        self.store.append_event(state.run_id, "run_created", bundle_id=self.bundle_id)
        return state

    async def _agent_request(
        self,
        client: AgentContainerClient,
        state: RunState,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        guard_generation: bool = True,
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
        return state.observe_process(pid, generation)

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

    async def run_smoke(
        self,
        *,
        profile: str,
        target: str = "WORLD_READY",
        timeout: float = 180,
        require_dynamic_library_load: bool = True,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._new_state(run_id)
        try:
            doctor = self.doctor()
            pmd3 = doctor["device"]["pymobiledevice3"]
            if not pmd3["available"] or not doctor["device"]["devicectl"]["available"]:
                state.fail(FailureClass.DEVICE_NOT_FOUND, "required host device tooling is unavailable", blocked=True)
                return self._finish(state)
            if not pmd3["python_api_available"]:
                state.fail(
                    FailureClass.AGENT_UNREACHABLE,
                    "amethystd Python environment lacks pymobiledevice3; install requirements-agent.txt in the daemon environment",
                    blocked=True,
                )
                return self._finish(state)
            if not pmd3.get("ok", False):
                state.fail(FailureClass.DEVICE_NOT_FOUND, "pymobiledevice3 usbmux probe did not succeed", blocked=True)
                return self._finish(state)
            if not self.device_udid:
                state.fail(FailureClass.DEVICE_NOT_FOUND, "explicit device UDID is required for unattended state changes", blocked=True)
                return self._finish(state)
            state.transition(Stage.DEVICE_PREFLIGHT_OK, doctor=doctor)
            self.store.save(state)

            launch = await asyncio.to_thread(self.device.launch_app, self.bundle_id)
            self.store.append_event(
                state.run_id,
                "app_launch_command",
                returncode=launch.returncode,
                stdout=launch.stdout,
                stderr=launch.stderr,
                timed_out=launch.timed_out,
            )
            if not launch.ok:
                state.fail(FailureClass.AGENT_UNREACHABLE, f"devicectl launch failed: {launch.stderr.strip()}")
                return self._finish(state)
            state.transition(Stage.APP_LAUNCHED)
            self.store.save(state)

            client = AgentContainerClient(self.device_udid, self.bundle_id)
            status = await self._agent_request(client, state, "status", guard_generation=False, timeout=30)
            if not status.get("ok"):
                state.fail(FailureClass.AGENT_UNREACHABLE, json.dumps(status, sort_keys=True))
                return self._finish(state)
            invalidated = self._observe_identity(state, status)
            state.app_state = status.get("state")
            state.profile = status.get("profile") or {}
            state.transition(Stage.AGENT_READY, process_invalidated=invalidated)
            self.store.save(state)

            self.jit = JITSession(self.device, self.store.artifact_dir(state.run_id))
            state.transition(Stage.JIT_ATTACHING)
            self.store.save(state)
            try:
                jit_evidence = await asyncio.to_thread(
                    self.jit.ensure,
                    pid=state.pid,
                    process_generation=state.process_generation,
                    run_id=state.run_id,
                    require_dynamic_library_load=require_dynamic_library_load,
                    timeout=min(timeout, 60),
                )
            except JITProcessorUnconfigured as exc:
                state.fail(FailureClass.JIT_PROCESSOR_UNCONFIGURED, str(exc), blocked=True)
                return self._finish(state)
            except StaleDebugserverError as exc:
                state.fail(FailureClass.STALE_DEBUGSERVER, str(exc))
                return self._finish(state)
            except DyldVerificationError as exc:
                state.fail(FailureClass.DYLD_VALIDATION_FAILURE, str(exc))
                return self._finish(state)
            except JITVerificationError as exc:
                state.fail(FailureClass.JIT_VERIFICATION_FAILURE, str(exc))
                return self._finish(state)
            state.mark_jit(
                exec_ready=jit_evidence.exec_ready,
                dynamic_library_load_ready=jit_evidence.dynamic_library_load_ready,
                evidence={"jit_session": jit_evidence.to_dict()},
            )
            self.store.save(state)

            profile_response = await self._agent_request(
                client, state, "profile/set", {"profile": profile}, timeout=30
            )
            if not profile_response.get("ok"):
                state.fail(FailureClass.RUNTIME_ABI_PRECHECK_FAILED, json.dumps(profile_response, sort_keys=True))
                return self._finish(state)
            if self._observe_identity(state, profile_response):
                state.fail(FailureClass.JIT_VERIFICATION_FAILURE, "process generation changed during profile transaction")
                return self._finish(state)
            state.profile = profile_response.get("profile") or state.profile
            state.transition(Stage.PROFILE_READY)
            self.store.save(state)

            launch_response = await self._agent_request(client, state, "launch", {}, timeout=30)
            if not launch_response.get("ok"):
                state.fail(FailureClass.RUNTIME_ABI_PRECHECK_FAILED, json.dumps(launch_response, sort_keys=True))
                return self._finish(state)
            if self._observe_identity(state, launch_response):
                state.fail(FailureClass.JIT_VERIFICATION_FAILURE, "process generation changed while accepting launch")
                return self._finish(state)
            state.transition(Stage.LAUNCH_ACCEPTED)
            self.store.save(state)

            wanted = Stage(target.upper())
            if wanted == Stage.LAUNCH_ACCEPTED:
                state.transition(Stage.PASS, target=wanted.value)
                return self._finish(state)
            deadline = monotonic() + timeout
            seen_events: set[tuple] = set()
            while monotonic() < deadline:
                events = await client.read_events(state.run_id)
                for event in events:
                    key = self.event_key(event)
                    if key is not None and key in seen_events:
                        continue
                    if key is not None:
                        seen_events.add(key)

                    event_generation = event.get("process_generation")
                    event_pid = event.get("process_id")
                    if isinstance(event_generation, str) and isinstance(event_pid, int):
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
                    if mapped:
                        state.transition(mapped, last_app_event=event)
                        self.store.save(state)
                        if mapped == wanted:
                            state.transition(Stage.PASS, target=wanted.value)
                            return self._finish(state)
                    if event_name in {"crashed", "failed"}:
                        state.fail(FailureClass.AMETHYST_CRASH, json.dumps(event, sort_keys=True))
                        return self._finish(state)
                await asyncio.sleep(0.25)
            timeout_class = FailureClass.WORLD_LOAD_TIMEOUT if wanted == Stage.WORLD_READY else FailureClass.MC_READY_TIMEOUT
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
        client = AgentContainerClient(self.device_udid, self.bundle_id)
        manifest = await client.stage_payload(local_path, name)
        return {"ok": True, "manifest": manifest}

    def _finish(self, state: RunState) -> dict[str, Any]:
        self.store.save(state)
        self.store.append_event(
            state.run_id,
            "run_finished",
            stage=state.stage.value,
            failure=state.failure.value if state.failure else None,
        )
        return {"ok": state.stage == Stage.PASS, "state": state.to_dict()}

    def close(self) -> None:
        if self.jit:
            self.jit.close()
            self.jit = None
