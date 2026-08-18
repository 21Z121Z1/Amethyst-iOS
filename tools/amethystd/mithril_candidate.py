from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

MITHRIL_LIBRARY = "libmithril.dylib"
MITHRIL_IOS_DEPLOYMENT_TARGET = "16.0"

# Natives/ctxbridges/gl_bridge.m resolves these symbols unconditionally before
# creating the EGL display/context/surface. Keep this list synchronized with the
# launcher bridge rather than accepting a library that will fail only after an
# iPad cold start.
REQUIRED_EGL_EXPORTS = frozenset(
    {
        "eglBindAPI",
        "eglChooseConfig",
        "eglCreateContext",
        "eglCreateWindowSurface",
        "eglDestroyContext",
        "eglDestroySurface",
        "eglGetConfigAttrib",
        "eglGetCurrentContext",
        "eglGetCurrentSurface",
        "eglGetDisplay",
        "eglGetError",
        "eglGetPlatformDisplay",
        "eglInitialize",
        "eglMakeCurrent",
        "eglReleaseThread",
        "eglSwapBuffers",
        "eglSwapInterval",
        "eglTerminate",
        # Not part of the bridge's mandatory 18-entry table, but retained as a
        # required public fallback for EGL/GL consumers.
        "eglGetProcAddress",
    }
)

# Representative symbols prove that this is the desktop-GL compatibility
# provider expected by LWJGL, not merely an EGL shim with an unrelated payload.
# glCompileShader/glDrawElements are also used by the physical-device
# renderer_ready provenance gate.
REQUIRED_GL_EXPORTS = frozenset(
    {
        "glGetString",
        "glGetStringi",
        "glGetError",
        "glGetIntegerv",
        "glClear",
        "glViewport",
        "glCompileShader",
        "glLinkProgram",
        "glUseProgram",
        "glDrawArrays",
        "glDrawElements",
        "glBindBuffer",
        "glBufferData",
        "glBindTexture",
        "glTexImage2D",
        "glBindFramebuffer",
        "glCheckFramebufferStatus",
        "glReadPixels",
    }
)

_REQUIRED_TOOLS = ("lipo", "vtool", "otool", "nm")
_FORBIDDEN_DEPENDENCY_MARKERS = ("vulkan", "moltenvk")


class MithrilCandidateError(RuntimeError):
    pass


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ToolFinder = Callable[[str], str | None]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
    )


def _checked_run(
    tool: str,
    arguments: Sequence[str],
    *,
    tools: dict[str, str],
    runner: CommandRunner,
) -> str:
    command = [tools[tool], *arguments]
    try:
        result = runner(command)
    except OSError as exc:
        raise MithrilCandidateError(
            f"{tool} could not be executed while validating Mithril candidate: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        raise MithrilCandidateError(
            f"{tool} failed while validating Mithril candidate"
            + (f": {detail}" if detail else "")
        )
    return result.stdout


def _parse_build_version(output: str) -> tuple[str, str]:
    platform_match = re.search(r"(?m)^\s*platform\s+(\S+)\s*$", output)
    minos_match = re.search(r"(?m)^\s*minos\s+(\S+)\s*$", output)
    if not platform_match or not minos_match:
        raise MithrilCandidateError(
            "Mithril candidate has no parseable LC_BUILD_VERSION platform/minos"
        )
    return platform_match.group(1).upper(), minos_match.group(1)


def _parse_install_name(output: str, path: Path) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    # `otool -D` prints the inspected path on line one followed by the dylib id.
    if len(lines) < 2:
        raise MithrilCandidateError(
            f"Mithril candidate has no dylib install name: {path}"
        )
    return lines[-1]


def _parse_dependencies(output: str) -> list[str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    dependencies: list[str] = []
    for line in lines[1:]:
        dependencies.append(line.split(" (", 1)[0].strip())
    return dependencies


def _parse_exports(output: str) -> set[str]:
    exports: set[str] = set()
    for raw in output.splitlines():
        symbol = raw.strip()
        if not symbol:
            continue
        # Mach-O `nm` convention exposes C symbols with a leading underscore.
        if symbol.startswith("_"):
            symbol = symbol[1:]
        exports.add(symbol)
    return exports


def inspect_mithril_candidate(
    local_path: str | Path,
    *,
    runner: CommandRunner = _run,
    tool_finder: ToolFinder = shutil.which,
) -> dict[str, Any]:
    """Validate the host-observable iPhoneOS shipping contract before AFC writes.

    Code-signature validity is deliberately left to container_io's generic
    Mach-O staging contract. This function rejects the other high-value false
    candidates first: macOS/fat/wrong-id dylibs, a Vulkan/MoltenVK payload, or a
    library that cannot satisfy Amethyst/LWJGL's public ABI.
    """

    path = Path(local_path).expanduser().resolve()
    if not path.is_file() or path.name != MITHRIL_LIBRARY:
        raise MithrilCandidateError(
            f"Mithril staging requires a regular file named {MITHRIL_LIBRARY}"
        )

    tools: dict[str, str] = {}
    for name in _REQUIRED_TOOLS:
        resolved = tool_finder(name)
        if not resolved:
            raise MithrilCandidateError(
                f"host tool {name!r} is required to validate an iPhoneOS Mithril candidate"
            )
        tools[name] = resolved

    arch_output = _checked_run(
        "lipo", ["-archs", str(path)], tools=tools, runner=runner
    )
    archs = tuple(arch_output.split())
    if archs != ("arm64",):
        raise MithrilCandidateError(
            "Mithril candidate must be a thin arm64 iPhoneOS dylib; "
            f"observed architectures: {', '.join(archs) if archs else '<none>'}"
        )

    build_output = _checked_run(
        "vtool", ["-show-build", str(path)], tools=tools, runner=runner
    )
    platform, minos = _parse_build_version(build_output)
    if platform != "IOS":
        raise MithrilCandidateError(
            f"Mithril candidate must target iPhoneOS; LC_BUILD_VERSION platform is {platform}"
        )
    if minos != MITHRIL_IOS_DEPLOYMENT_TARGET:
        raise MithrilCandidateError(
            "Mithril candidate has the wrong iPhoneOS deployment target: "
            f"{minos!r}; expected {MITHRIL_IOS_DEPLOYMENT_TARGET!r}"
        )

    install_output = _checked_run(
        "otool", ["-D", str(path)], tools=tools, runner=runner
    )
    install_name = _parse_install_name(install_output, path)
    if install_name != f"@rpath/{MITHRIL_LIBRARY}":
        raise MithrilCandidateError(
            "Mithril candidate has the wrong install name: "
            f"{install_name!r}; expected '@rpath/{MITHRIL_LIBRARY}'"
        )

    dependency_output = _checked_run(
        "otool", ["-L", str(path)], tools=tools, runner=runner
    )
    dependencies = _parse_dependencies(dependency_output)
    forbidden = [
        dependency
        for dependency in dependencies
        if any(marker in dependency.casefold() for marker in _FORBIDDEN_DEPENDENCY_MARKERS)
    ]
    if forbidden:
        raise MithrilCandidateError(
            "Mithril candidate is not a Vulkan-free DirectMetal artifact; forbidden "
            f"dependencies: {', '.join(forbidden)}"
        )

    export_output = _checked_run(
        "nm", ["-gUj", str(path)], tools=tools, runner=runner
    )
    exports = _parse_exports(export_output)
    required = REQUIRED_EGL_EXPORTS | REQUIRED_GL_EXPORTS
    missing = sorted(required - exports)
    if missing:
        raise MithrilCandidateError(
            "Mithril candidate is missing required Amethyst/LWJGL exports: "
            + ", ".join(missing)
        )

    return {
        "version": 1,
        "kind": "mithril-directmetal-ios",
        "path": str(path),
        "architectures": list(archs),
        "platform": platform,
        "minos": minos,
        "install_name": install_name,
        "dependencies": dependencies,
        "vulkan_free_dynamic_dependencies": True,
        "required_export_count": len(required),
        "required_exports_verified": True,
    }
