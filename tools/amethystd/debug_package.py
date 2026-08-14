from __future__ import annotations

import os
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
MH_EXECUTE = 0x2


class AgentDebugBuildError(RuntimeError):
    pass


@dataclass
class PackageResult:
    output: Path
    bundle_id: str
    identity: str
    profile_name: str
    profile_uuid: str | None
    native_binary: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "output": str(self.output),
            "bundle_id": self.bundle_id,
            "identity": self.identity,
            "profile_name": self.profile_name,
            "profile_uuid": self.profile_uuid,
            "native_binary": str(self.native_binary),
        }


def _run(argv: list[str], *, cwd: Path | None = None, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentDebugBuildError(f"command timed out: {' '.join(argv)}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AgentDebugBuildError(f"command failed ({result.returncode}): {' '.join(argv)}\n{detail}")
    return result


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for info in archive.infolist():
        target = (destination / info.filename).resolve()
        if not target.is_relative_to(root):
            raise AgentDebugBuildError(f"IPA contains escaping path: {info.filename}")
    archive.extractall(destination)


def is_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def _thin_macho_filetype(raw: bytes, offset: int = 0) -> int | None:
    if len(raw) < offset + 16:
        return None
    magic = raw[offset : offset + 4]
    if magic in {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}:
        endian = "<"
    elif magic in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"}:
        endian = ">"
    else:
        return None
    return struct.unpack_from(endian + "I", raw, offset + 12)[0]


def macho_filetypes(path: Path) -> set[int] | None:
    """Return Mach-O file types for all slices, or None for malformed input."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    thin = _thin_macho_filetype(raw)
    if thin is not None:
        return {thin}
    if len(raw) < 8:
        return None

    magic = raw[:4]
    if magic == b"\xca\xfe\xba\xbe":
        endian, arch_size, is_64 = ">", 20, False
    elif magic == b"\xbe\xba\xfe\xca":
        endian, arch_size, is_64 = "<", 20, False
    elif magic == b"\xca\xfe\xba\xbf":
        endian, arch_size, is_64 = ">", 32, True
    elif magic == b"\xbf\xba\xfe\xca":
        endian, arch_size, is_64 = "<", 32, True
    else:
        return None

    count = struct.unpack_from(endian + "I", raw, 4)[0]
    if count <= 0 or count > 64 or len(raw) < 8 + count * arch_size:
        return None
    filetypes: set[int] = set()
    for index in range(count):
        base = 8 + index * arch_size
        if is_64:
            slice_offset, slice_size = struct.unpack_from(endian + "QQ", raw, base + 8)
        else:
            slice_offset, slice_size = struct.unpack_from(endian + "II", raw, base + 8)
        if slice_size < 16 or slice_offset + 16 > len(raw):
            return None
        filetype = _thin_macho_filetype(raw, int(slice_offset))
        if filetype is None:
            return None
        filetypes.add(filetype)
    return filetypes


def require_macho_executable(path: Path) -> None:
    filetypes = macho_filetypes(path)
    if not filetypes:
        raise AgentDebugBuildError(f"native executable is missing, malformed, or not Mach-O: {path}")
    if filetypes != {MH_EXECUTE}:
        rendered = ", ".join(f"0x{value:x}" for value in sorted(filetypes))
        raise AgentDebugBuildError(
            f"AgentDebug main binary must be Mach-O MH_EXECUTE (0x2); observed file type(s): {rendered}"
        )


def profile_allows_bundle(profile: dict[str, Any], bundle_id: str) -> bool:
    entitlements = profile.get("Entitlements") or {}
    application_id = str(entitlements.get("application-identifier", ""))
    if "." not in application_id:
        return False
    _, suffix = application_id.split(".", 1)
    if suffix == "*":
        return True
    if suffix.endswith("*"):
        return bundle_id.startswith(suffix[:-1])
    return suffix == bundle_id


def decode_profile(path: Path) -> dict[str, Any]:
    if shutil.which("security") is None:
        raise AgentDebugBuildError("macOS security tool is required to decode provisioning profiles")
    result = _run(["security", "cms", "-D", "-i", str(path)], timeout=30)
    try:
        value = plistlib.loads(result.stdout.encode())
    except Exception as exc:
        raise AgentDebugBuildError(f"failed to parse provisioning profile {path}") from exc
    if not isinstance(value, dict):
        raise AgentDebugBuildError("provisioning profile did not decode to a dictionary")
    return value


def validate_profile(profile: dict[str, Any], *, bundle_id: str, device_udid: str | None = None) -> None:
    if not profile_allows_bundle(profile, bundle_id):
        app_id = (profile.get("Entitlements") or {}).get("application-identifier")
        raise AgentDebugBuildError(f"provisioning profile application-identifier {app_id!r} does not allow {bundle_id}")
    entitlements = profile.get("Entitlements") or {}
    if entitlements.get("get-task-allow") is not True:
        raise AgentDebugBuildError("AgentDebug requires a development profile with get-task-allow=true")
    expiration = profile.get("ExpirationDate")
    if isinstance(expiration, datetime):
        expiry = expiration if expiration.tzinfo else expiration.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            raise AgentDebugBuildError("provisioning profile is expired")
    if device_udid:
        devices = profile.get("ProvisionedDevices") or []
        if device_udid not in devices:
            raise AgentDebugBuildError("target device is not present in ProvisionedDevices")


def discover_identity(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if shutil.which("security") is None:
        raise AgentDebugBuildError("macOS security tool is required to discover signing identities")
    result = _run(["security", "find-identity", "-v", "-p", "codesigning"], timeout=30)
    identities: list[str] = []
    for line in result.stdout.splitlines():
        match = re.search(r'\)\s+[0-9A-Fa-f]+\s+"([^"]+)"', line)
        if match and match.group(1).startswith("Apple Development:"):
            identities.append(match.group(1))
    identities = list(dict.fromkeys(identities))
    if not identities:
        raise AgentDebugBuildError("no valid Apple Development signing identity was found")
    if len(identities) != 1:
        raise AgentDebugBuildError(
            "multiple Apple Development identities are available; pass --identity explicitly instead of guessing"
        )
    return identities[0]


def build_native(repo_root: Path, *, jobs: int = 2) -> Path:
    make = os.environ.get("AMETHYST_MAKE") or shutil.which("gmake") or shutil.which("make")
    if not make:
        raise AgentDebugBuildError("make/gmake is unavailable")
    _run(
        [make, "native", "PLATFORM=2", f"MAKEFLAGS=-j{max(1, jobs)}"],
        cwd=repo_root,
        timeout=float(os.environ.get("AMETHYST_NATIVE_BUILD_TIMEOUT", "1800")),
    )
    candidates = [
        repo_root / "Natives" / "build" / "AngelAuraAmethyst.app" / "AngelAuraAmethyst",
        repo_root / "Natives" / "build-agent-control" / "AngelAuraAmethyst.app" / "AngelAuraAmethyst",
    ]
    for candidate in candidates:
        if candidate.is_file():
            require_macho_executable(candidate)
            return candidate.resolve()
    raise AgentDebugBuildError("native build completed but AngelAuraAmethyst executable was not found")


def _sign(path: Path, identity: str, entitlements: Path | None = None) -> None:
    command = [
        "codesign", "--force", "--sign", identity, "--timestamp=none", "--generate-entitlement-der",
    ]
    if entitlements is not None:
        command += ["--entitlements", str(entitlements)]
    command.append(str(path))
    _run(command, timeout=120)


def _sign_app(app: Path, identity: str, entitlements: dict[str, Any]) -> None:
    for signature in app.rglob("_CodeSignature"):
        if signature.is_dir():
            shutil.rmtree(signature, ignore_errors=True)

    with (app / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    executable_name = info.get("CFBundleExecutable")
    main_executable = app / str(executable_name) if executable_name else None

    macho_files = [path for path in app.rglob("*") if is_macho(path) and path != main_executable]
    for path in sorted(macho_files, key=lambda value: len(value.parts), reverse=True):
        _sign(path, identity)

    bundles = [
        path for path in app.rglob("*")
        if path.is_dir() and path.suffix in {".framework", ".appex", ".xpc"}
    ]
    for bundle in sorted(bundles, key=lambda value: len(value.parts), reverse=True):
        _sign(bundle, identity)

    with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as handle:
        plistlib.dump(entitlements, handle)
        entitlement_path = Path(handle.name)
    try:
        _sign(app, identity, entitlement_path)
        _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], timeout=120)
    finally:
        entitlement_path.unlink(missing_ok=True)


def build_agent_debug_ipa(
    *,
    repo_root: Path,
    base_ipa: Path,
    profile_path: Path,
    bundle_id: str,
    output: Path,
    identity: str | None = None,
    device_udid: str | None = None,
    native_binary: Path | None = None,
    display_name: str = "Amethyst AgentDebug",
    jobs: int = 2,
    allow_production_bundle: bool = False,
) -> PackageResult:
    repo_root = repo_root.resolve()
    base_ipa = base_ipa.expanduser().resolve()
    profile_path = profile_path.expanduser().resolve()
    output = output.expanduser().resolve()
    if not base_ipa.is_file() or base_ipa.suffix.lower() != ".ipa":
        raise AgentDebugBuildError("--base-ipa must point to an existing IPA")
    if not profile_path.is_file():
        raise AgentDebugBuildError("--profile must point to an existing .mobileprovision")
    if bundle_id == "org.angelauramc.amethyst" and not allow_production_bundle:
        raise AgentDebugBuildError(
            "refusing to overwrite the production bundle identity; use a dedicated AgentDebug bundle or explicitly allow it"
        )

    profile = decode_profile(profile_path)
    validate_profile(profile, bundle_id=bundle_id, device_udid=device_udid)
    signing_identity = discover_identity(identity)
    executable = native_binary.expanduser().resolve() if native_binary else build_native(repo_root, jobs=jobs)
    require_macho_executable(executable)

    with tempfile.TemporaryDirectory(prefix="amethyst-agentdebug-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(base_ipa) as archive:
            _safe_extract(archive, root)
        apps = list((root / "Payload").glob("*.app"))
        if len(apps) != 1:
            raise AgentDebugBuildError(f"expected one app in IPA payload, found {len(apps)}")
        app = apps[0]
        info_path = app / "Info.plist"
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        executable_name = str(info.get("CFBundleExecutable") or "AngelAuraAmethyst")
        info["CFBundleIdentifier"] = bundle_id
        info["CFBundleDisplayName"] = display_name
        info["CFBundleName"] = display_name
        with info_path.open("wb") as handle:
            plistlib.dump(info, handle, fmt=plistlib.FMT_BINARY)

        target_executable = app / executable_name
        shutil.copy2(executable, target_executable)
        target_executable.chmod(target_executable.stat().st_mode | 0o111)
        shutil.copy2(profile_path, app / "embedded.mobileprovision")
        _sign_app(app, signing_identity, dict(profile.get("Entitlements") or {}))

        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)
        _run(["/usr/bin/zip", "--symlinks", "-qry", str(output), "Payload"], cwd=root, timeout=300)

    if not output.is_file() or output.stat().st_size == 0:
        raise AgentDebugBuildError("AgentDebug IPA packaging produced no output")
    return PackageResult(
        output=output,
        bundle_id=bundle_id,
        identity=signing_identity,
        profile_name=str(profile.get("Name") or "<unnamed>"),
        profile_uuid=str(profile.get("UUID")) if profile.get("UUID") else None,
        native_binary=executable,
    )
