"""Build the opt-in Mac native bridge; does not start or stop Capture One."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from c1ai.macos_backend import inspect_installation, SUPPORTED_VERSION
from c1ai.macos_ipc import state_directory, secure_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", default="clang", help="Apple clang or a local Zig compiler")
    parser.add_argument("--enable", action="store_true", help="Enable this build for the current user's Mac backend")
    args = parser.parse_args()
    environment = inspect_installation()
    if environment["version"] != SUPPORTED_VERSION or environment["architecture"] != "arm64":
        parser.error("This prototype currently targets the checked arm64 Capture One build only")
    secure_directory(state_directory() / "native-bridge")
    root = state_directory() / "native-bridge" / "v2"
    secure_directory(root)
    source = Path(__file__).resolve().parents[1] / "internal" / "macos" / "NativeBridge.m"
    build_id = hashlib.sha256(b"".join((source.parent / name).read_bytes() for name in
        ("NativeBridge.m", "GradientActions.inc", "MaskCommands.inc", "ColorActions.inc", "LayerActions.inc", "ViewerCache.inc", "FoundationTypes.h"))).hexdigest()
    output = root / ("NativeBridge-" + build_id[:16] + ".dylib")
    zig = Path(args.compiler).name == "zig"
    compiler = [args.compiler, "cc"] if zig else [args.compiler]
    target = "aarch64-macos" if zig else "arm64-apple-macos11"
    # A flat-namespace-only dylib happens to load after AppKit is present, but
    # fails in a clean process. Declare the system framework dependencies.
    # Zig ships libSystem stubs, not Apple framework SDKs. These tiny import
    # declarations describe system ABI names; no vendor framework is copied.
    framework_flags = []
    if zig:
        stubs = root / "link-stubs"
        for framework, symbols in {"Foundation": ["NSObject", "NSString", "NSArray", "NSDictionary"],
                                   "AppKit": ["NSBitmapImageRep"]}.items():
            directory = stubs / (framework + ".framework")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (framework + ".tbd")).write_text(
                "--- !tapi-tbd\ntbd-version: 4\ntargets: [ arm64-macos ]\n"
                f"install-name: '/System/Library/Frameworks/{framework}.framework/Versions/C/{framework}'\n"
                "exports:\n  - targets: [ arm64-macos ]\n"
                f"    objc-classes: [ {', '.join(symbols)} ]\n...\n")
        framework_flags = ["-F", str(stubs)]
    # A prior build may still be mapped in a running host. Publish a new inode,
    # never truncate that library or replace the usable build on compiler failure.
    descriptor, temporary = tempfile.mkstemp(prefix=".NativeBridge-", suffix=".dylib", dir=root)
    os.close(descriptor)
    try:
        subprocess.run(compiler + ["-target", target, "-dynamiclib", "-nostdlib", "-fblocks", "-O2", "-fno-sanitize=all",
            "-fno-objc-arc", "-Werror", "-Wl,-undefined,dynamic_lookup",
            '-DC1AI_BUILD_ID="' + build_id + '"',
            "-Xlinker", "-install_name", "-Xlinker", str(output), "-o", temporary, str(source)]
            + framework_flags + ["-framework", "Foundation", "-framework", "AppKit"], check=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result = {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "build_id": build_id,
              "capture_one_version": SUPPORTED_VERSION, "built": True, "loaded": False,
              "live_render_verified": False}
    if args.enable:
        temporary = state_directory() / ".macos-bridge.tmp"
        with open(temporary, "w", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            json.dump({**result, "enabled": True}, stream)
        temporary.replace(state_directory() / "macos-bridge.json")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
