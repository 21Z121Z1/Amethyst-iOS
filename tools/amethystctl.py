#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.amethystd.agent_output import bounded_result
from tools.amethystd.debug_package import build_agent_debug_ipa
from tools.amethystd.preflight import collect_host_preflight
from tools.amethystd.runtime_contract import verify_contract
from tools.amethystd.server import request_daemon
from tools.amethystd.store import StateStore


_SPECIAL_GLFW_KEYS = {
    "SPACE": 32,
    "ESC": 256,
    "ESCAPE": 256,
    "ENTER": 257,
    "TAB": 258,
    "BACKSPACE": 259,
    "INSERT": 260,
    "DELETE": 261,
    "RIGHT": 262,
    "LEFT": 263,
    "DOWN": 264,
    "UP": 265,
    "PAGE_UP": 266,
    "PAGE_DOWN": 267,
    "HOME": 268,
    "END": 269,
    "CAPS_LOCK": 280,
}


def glfw_key(value: str) -> int:
    normalized = value.strip().upper().replace("-", "_")
    if normalized in _SPECIAL_GLFW_KEYS:
        return _SPECIAL_GLFW_KEYS[normalized]
    if len(normalized) == 1 and (normalized.isalpha() or normalized.isdigit()):
        return ord(normalized)
    if normalized.startswith("F") and normalized[1:].isdigit():
        number = int(normalized[1:])
        if 1 <= number <= 25:
            return 289 + number
    try:
        numeric = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"unknown GLFW key {value!r}") from exc
    if not 0 <= numeric <= 512:
        raise argparse.ArgumentTypeError("GLFW key must be in the range 0..512")
    return numeric


def emit(value: dict) -> int:
    store = StateStore()
    value = bounded_result(value, store.root)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    if value.get("ok"):
        return 0
    failure = ((value.get("state") or {}).get("failure") or value.get("failure") or "")
    if failure.startswith("DEVICE_") or failure in {"DEVELOPER_MODE_REQUIRED"}:
        return 3
    if failure.startswith("JIT_") or failure in {"DYLD_VALIDATION_FAILURE", "STALE_DEBUGSERVER"}:
        return 4
    if failure in {"SIGNING_FAILURE", "INSTALL_UNKNOWN"}:
        return 6
    return 5


async def call(method: str, params: dict | None = None) -> dict:
    store = StateStore()
    return await request_daemon(store.socket_path, method, params)


def daemon_healthy(store: StateStore) -> bool:
    if not store.socket_path.exists():
        return False
    try:
        result = asyncio.run(request_daemon(store.socket_path, "ping"))
        return result.get("ok") is True and result.get("daemon") == "amethystd"
    except (ConnectionError, FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError):
        return False


def ensure_daemon(store: StateStore, *, start: bool) -> None:
    if daemon_healthy(store):
        return
    if store.socket_path.exists():
        store.socket_path.unlink(missing_ok=True)
    if not start:
        raise RuntimeError("amethystd is not running; use `amethystctl daemon start`")
    log = store.root / "amethystd.log"
    with log.open("ab") as handle:
        subprocess.Popen(
            [sys.executable, "-m", "tools.amethystd"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=REPO_ROOT,
            env=os.environ.copy(),
        )
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if daemon_healthy(store):
            return
        sleep(0.05)
    raise RuntimeError(f"amethystd did not become healthy; see {log}")


def configure_local(store: StateStore, *, device_udid: str | None, bundle_id: str | None) -> dict:
    current = store.load_config()
    if device_udid:
        current["device_udid"] = device_udid
    if bundle_id:
        current["bundle_id"] = bundle_id
    store.save_config(current)
    if daemon_healthy(store):
        return asyncio.run(call("configure", {"device_udid": device_udid, "bundle_id": bundle_id}))
    return {"ok": True, "configuration": store.load_config(), "daemon_reconfigure_pending": True}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="amethystctl")
    root.add_argument(
        "--max-output-bytes",
        type=int,
        help="Codex-facing stdout budget; oversized full JSON is stored under .amethyst-agent/tool-output",
    )
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--host-only", action="store_true", help="check build/tool prerequisites without a device or daemon")
    sub.add_parser("status")

    configure = sub.add_parser("configure")
    configure.add_argument("--device", dest="device_udid")
    configure.add_argument("--bundle-id")

    daemon = sub.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop", "status"])

    deploy = sub.add_parser("deploy")
    deploy.add_argument("app", help="Development-signed .app directory or .ipa")

    agent_debug = sub.add_parser("agent-debug")
    agent_debug_sub = agent_debug.add_subparsers(dest="agent_debug_action", required=True)
    package = agent_debug_sub.add_parser("package")
    package.add_argument("--base-ipa", required=True)
    package.add_argument("--profile", required=True, help="Apple Development .mobileprovision")
    package.add_argument("--bundle-id")
    package.add_argument("--device", dest="device_udid")
    package.add_argument("--identity", help="Apple Development identity; auto-select only when exactly one exists")
    package.add_argument("--native-binary", help="Use an already-built AngelAuraAmethyst Mach-O")
    package.add_argument("--output", default="artifacts/Amethyst-AgentDebug.ipa")
    package.add_argument("--display-name", default="Amethyst AgentDebug")
    package.add_argument("--jobs", type=int, default=2)
    package.add_argument("--allow-production-bundle", action="store_true")
    package.add_argument("--deploy", action="store_true")

    stop = sub.add_parser("stop")
    stop.add_argument("--force", action="store_true")
    stop.add_argument("--timeout", type=float, default=30)

    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="run_kind", required=True)
    smoke = run_sub.add_parser("smoke")
    smoke.add_argument("--profile", required=True)
    smoke.add_argument("--target", default="WORLD_READY")
    smoke.add_argument("--timeout", type=float, default=180)
    smoke.add_argument("--no-dynamic-dylib", action="store_true")

    payload = sub.add_parser("payload")
    payload_sub = payload.add_subparsers(dest="payload_action", required=True)
    stage = payload_sub.add_parser("stage")
    stage.add_argument("path")
    stage.add_argument("--name", required=True)

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_action", required=True)
    verify = runtime_sub.add_parser("verify")
    verify.add_argument("manifest")

    logs = sub.add_parser("logs")
    logs_sub = logs.add_subparsers(dest="logs_action", required=True)
    query = logs_sub.add_parser("query", help="query only a bounded tail window instead of printing whole device logs")
    query.add_argument("--source", choices=["latest", "app", "lab"], default="latest")
    query.add_argument("--run-id")
    query.add_argument("--contains")
    query.add_argument("--limit", type=int, default=80)
    query.add_argument("--max-bytes", type=int, default=65536)

    input_parser = sub.add_parser("input")
    input_sub = input_parser.add_subparsers(dest="input_action", required=True)
    key = input_sub.add_parser("key", help="send a semantic GLFW key action")
    key.add_argument("key", type=glfw_key, help="name such as W, ESCAPE, F3, or numeric GLFW key")
    key.add_argument("--mode", choices=["tap", "press", "release"], default="tap")
    key.add_argument("--hold-ms", type=int, default=50)
    key.add_argument("--scancode", type=int, default=0)
    key.add_argument("--mods", type=int, default=0)

    collect = sub.add_parser("collect")
    collect.add_argument("run_id")
    collect.add_argument("--crashes", action="store_true")
    collect.add_argument("--no-screenshot", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    if args.max_output_bytes is not None:
        os.environ["AMETHYSTCTL_MAX_OUTPUT_BYTES"] = str(args.max_output_bytes)
    store = StateStore()
    try:
        if args.command == "runtime" and args.runtime_action == "verify":
            return emit(verify_contract(args.manifest))

        if args.command == "doctor" and args.host_only:
            return emit(collect_host_preflight(REPO_ROOT))

        if args.command == "configure":
            if not args.device_udid and not args.bundle_id:
                return emit({"ok": True, "configuration": store.load_config()})
            return emit(configure_local(store, device_udid=args.device_udid, bundle_id=args.bundle_id))

        if args.command == "agent-debug" and args.agent_debug_action == "package":
            config = store.load_config()
            bundle_id = args.bundle_id or os.environ.get("AMETHYST_BUNDLE_ID") or config.get("bundle_id")
            device_udid = args.device_udid or os.environ.get("AMETHYST_DEVICE_UDID") or config.get("device_udid")
            if not bundle_id:
                raise ValueError("AgentDebug packaging requires --bundle-id, AMETHYST_BUNDLE_ID, or saved configuration")
            result = build_agent_debug_ipa(
                repo_root=REPO_ROOT,
                base_ipa=Path(args.base_ipa),
                profile_path=Path(args.profile),
                bundle_id=bundle_id,
                output=Path(args.output),
                identity=args.identity,
                device_udid=device_udid,
                native_binary=Path(args.native_binary) if args.native_binary else None,
                display_name=args.display_name,
                jobs=args.jobs,
                allow_production_bundle=args.allow_production_bundle,
            ).to_dict()
            if not args.deploy:
                return emit(result)
            if not device_udid:
                raise ValueError("--deploy requires --device, AMETHYST_DEVICE_UDID, or saved configuration")
            configure_local(store, device_udid=device_udid, bundle_id=bundle_id)
            ensure_daemon(store, start=True)
            asyncio.run(call("configure", {"device_udid": device_udid, "bundle_id": bundle_id}))
            deployment = asyncio.run(call("deploy", {"app_path": result["output"]}))
            combined = {
                "ok": bool(deployment.get("ok")),
                "package": result,
                "deploy": deployment,
            }
            if not combined["ok"]:
                combined["failure"] = deployment.get("failure", "INSTALL_UNKNOWN")
            return emit(combined)

        if args.command == "daemon":
            if args.action == "start":
                ensure_daemon(store, start=True)
                return emit({"ok": True, "healthy": True, "socket": str(store.socket_path)})
            if args.action == "stop":
                ensure_daemon(store, start=False)
                return emit(asyncio.run(call("shutdown")))
            healthy = daemon_healthy(store)
            return emit({"ok": healthy, "healthy": healthy, "socket": str(store.socket_path)})

        ensure_daemon(store, start=False)
        if args.command == "doctor":
            result = asyncio.run(call("doctor"))
            result["host_preflight"] = collect_host_preflight(REPO_ROOT)
            return emit(result)
        if args.command == "status":
            return emit(asyncio.run(call("status")))
        if args.command == "deploy":
            return emit(asyncio.run(call("deploy", {"app_path": args.app})))
        if args.command == "stop":
            return emit(asyncio.run(call("stop", {"force": args.force, "timeout": args.timeout})))
        if args.command == "run" and args.run_kind == "smoke":
            return emit(
                asyncio.run(
                    call(
                        "run_smoke",
                        {
                            "profile": args.profile,
                            "target": args.target,
                            "timeout": args.timeout,
                            "require_dynamic_library_load": not args.no_dynamic_dylib,
                        },
                    )
                )
            )
        if args.command == "payload" and args.payload_action == "stage":
            return emit(asyncio.run(call("stage_payload", {"local_path": args.path, "name": args.name})))
        if args.command == "logs" and args.logs_action == "query":
            return emit(asyncio.run(call("query_logs", {
                "source": args.source,
                "run_id": args.run_id,
                "contains": args.contains,
                "limit": args.limit,
                "max_bytes": args.max_bytes,
            })))
        if args.command == "input" and args.input_action == "key":
            common = {"key": args.key, "hold_ms": args.hold_ms, "scancode": args.scancode, "mods": args.mods}
            if args.mode != "tap":
                return emit(asyncio.run(call("input_key", {**common, "mode": args.mode})))
            press = asyncio.run(call("input_key", {**common, "mode": "press"}))
            if not press.get("ok"):
                return emit({"ok": False, "phase": "press", "response": press})
            hold_ms = max(1, min(int(args.hold_ms), 5000))
            sleep(hold_ms / 1000.0)
            release = asyncio.run(call("input_key", {**common, "mode": "release"}))
            return emit({
                "ok": bool(release.get("ok")),
                "mode": "tap",
                "key": args.key,
                "hold_ms": hold_ms,
                "press_state": press.get("state"),
                "release_state": release.get("state"),
                "response": release,
            })
        if args.command == "collect":
            return emit(
                asyncio.run(
                    call(
                        "collect",
                        {
                            "run_id": args.run_id,
                            "include_crashes": args.crashes,
                            "screenshot": not args.no_screenshot,
                        },
                    )
                )
            )
        return emit({"ok": False, "error": "unsupported_command"})
    except Exception as exc:
        print(f"amethystctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
