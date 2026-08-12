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

from tools.amethystd.server import request_daemon
from tools.amethystd.store import StateStore


def emit(value: dict) -> int:
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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="amethystctl")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")

    daemon = sub.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop", "status"])

    deploy = sub.add_parser("deploy")
    deploy.add_argument("app", help="Development-signed .app directory or .ipa")

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

    collect = sub.add_parser("collect")
    collect.add_argument("run_id")
    collect.add_argument("--crashes", action="store_true")
    collect.add_argument("--no-screenshot", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    store = StateStore()
    try:
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
            return emit(asyncio.run(call("doctor")))
        if args.command == "status":
            return emit(asyncio.run(call("status")))
        if args.command == "deploy":
            return emit(asyncio.run(call("deploy", {"app_path": args.app})))
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
