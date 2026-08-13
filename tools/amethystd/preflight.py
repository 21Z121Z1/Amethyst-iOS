from __future__ import annotations

import importlib.metadata
import shutil
import sys
from pathlib import Path
from typing import Any


def collect_host_preflight(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    try:
        pmd3_version = importlib.metadata.version("pymobiledevice3")
    except importlib.metadata.PackageNotFoundError:
        pmd3_version = None

    tools = {name: {"available": shutil.which(name) is not None} for name in ("xcodebuild", "javac", "make", "git")}
    required_files = {
        "requirements_agent": (repo_root / "requirements-agent.txt").is_file(),
        "jit_processor": (repo_root / "tools" / "amethystd" / "universal_jit26_processor.py").is_file(),
        "agent_protocol": (repo_root / "agent-protocol" / "v2.schema.json").is_file(),
    }
    build_ready = all(item["available"] for item in tools.values()) and all(required_files.values())
    remediation = []
    if pmd3_version is None:
        remediation.append("install requirements-agent.txt in the Python used by amethystd")
    missing_tools = [name for name, state in tools.items() if not state["available"]]
    if missing_tools:
        remediation.append("provide required host tools: " + ", ".join(missing_tools))
    return {
        "ok": build_ready,
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "pymobiledevice3_available": pmd3_version is not None,
            "pymobiledevice3_version": pmd3_version,
        },
        "tools": tools,
        "required_files": required_files,
        "remediation": remediation,
    }
