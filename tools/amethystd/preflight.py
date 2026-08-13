from __future__ import annotations

import configparser
import importlib.metadata
import shutil
import sys
from pathlib import Path
from typing import Any


def _tool_state(*names: str) -> dict[str, Any]:
    for name in names:
        path = shutil.which(name)
        if path:
            return {"available": True, "command": name, "path": path}
    return {"available": False, "command": names[0] if names else None}


def _submodule_state(repo_root: Path) -> dict[str, Any]:
    gitmodules = repo_root / ".gitmodules"
    if not gitmodules.is_file():
        return {"ok": True, "entries": []}
    parser = configparser.ConfigParser()
    parser.read(gitmodules, encoding="utf-8")
    entries: list[dict[str, Any]] = []
    for section in parser.sections():
        relative = parser.get(section, "path", fallback="").strip()
        if not relative:
            continue
        path = repo_root / relative
        initialized = path.is_dir() and any(path.iterdir())
        entries.append({"path": relative, "initialized": initialized})
    return {
        "ok": all(entry["initialized"] for entry in entries),
        "entries": entries,
        "missing": [entry["path"] for entry in entries if not entry["initialized"]],
    }


def collect_host_preflight(repo_root: Path) -> dict[str, Any]:
    """Return one compact prerequisite snapshot before a physical-device iteration."""
    repo_root = repo_root.resolve()
    try:
        pmd3_version = importlib.metadata.version("pymobiledevice3")
    except importlib.metadata.PackageNotFoundError:
        pmd3_version = None

    tools = {
        "xcodebuild": _tool_state("xcodebuild"),
        "xcrun": _tool_state("xcrun"),
        "javac": _tool_state("javac"),
        "make": _tool_state("gmake", "make"),
        "cmake": _tool_state("cmake"),
        "wget": _tool_state("wget"),
        "ldid": _tool_state("ldid"),
        "codesign": _tool_state("codesign"),
        "security": _tool_state("security"),
        "git": _tool_state("git"),
        "pymobiledevice3_cli": _tool_state("pymobiledevice3"),
    }
    required_files = {
        "requirements_agent": (repo_root / "requirements-agent.txt").is_file(),
        "jit_processor": (repo_root / "tools" / "amethystd" / "universal_jit26_processor.py").is_file(),
        "agent_protocol": (repo_root / "agent-protocol" / "v2.schema.json").is_file(),
        "makefile": (repo_root / "Makefile").is_file(),
    }
    submodules = _submodule_state(repo_root)

    device_harness_ready = (
        pmd3_version is not None
        and tools["pymobiledevice3_cli"]["available"]
        and tools["xcrun"]["available"]
        and all(required_files[key] for key in ("requirements_agent", "jit_processor", "agent_protocol"))
    )
    native_build_ready = (
        all(tools[key]["available"] for key in ("xcodebuild", "javac", "make", "cmake", "wget", "ldid", "git"))
        and required_files["makefile"]
        and submodules["ok"]
    )

    remediation: list[str] = []
    if pmd3_version is None or not tools["pymobiledevice3_cli"]["available"]:
        remediation.append("install requirements-agent.txt in the same Python environment used by amethystd")
    if not tools["xcrun"]["available"]:
        remediation.append("select a usable Xcode toolchain before physical-device work")
    if not submodules["ok"]:
        remediation.append("initialize repository submodules before native builds: " + ", ".join(submodules["missing"]))
    missing_native = [
        key for key in ("xcodebuild", "javac", "make", "cmake", "wget", "ldid", "git")
        if not tools[key]["available"]
    ]
    if missing_native:
        remediation.append("provide native build tools: " + ", ".join(missing_native))
    missing_files = [key for key, present in required_files.items() if not present]
    if missing_files:
        remediation.append("restore required repository files: " + ", ".join(missing_files))

    return {
        "ok": device_harness_ready,
        "device_harness_ready": device_harness_ready,
        "native_build_ready": native_build_ready,
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "pymobiledevice3_available": pmd3_version is not None,
            "pymobiledevice3_version": pmd3_version,
        },
        "tools": tools,
        "submodules": submodules,
        "required_files": required_files,
        "remediation": remediation,
    }
