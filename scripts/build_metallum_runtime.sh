#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LWJGL_REPOSITORY="${LWJGL_REPOSITORY:-https://github.com/AngelAuraMC/lwjgl3.git}"
LWJGL_REF="${LWJGL_REF:-702c2102a2876cdf67d1d57f6819fa2d458217d9}"
DEPS_DIR="${AMETHYST_DEPS_DIR:-$ROOT_DIR/.deps}"
LWJGL_DIR="${LWJGL_SOURCE_DIR:-$DEPS_DIR/lwjgl3-ios-3.4.1}"
METALUNIVERSAL_DIR="${METALUNIVERSAL_DIR:-$(cd "$ROOT_DIR/.." 2>/dev/null && pwd)/MetalUniversal}"
BUILD_TARGET="${1:-package}"
if [ "$#" -gt 0 ]; then shift; fi

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

if [ "$(uname -s)" != "Darwin" ]; then
    fail "the iOS LWJGL and MetalUniversal native builds require macOS/Xcode"
fi

for command in git bash python3 ant xcodebuild xcrun vtool install_name_tool unzip wget make; do
    require_command "$command"
done

if [ ! -x /usr/libexec/java_home ]; then
    fail "/usr/libexec/java_home is unavailable"
fi

JAVA8_HOME="${JAVA8_HOME:-$(/usr/libexec/java_home -v 1.8 2>/dev/null || true)}"
JAVA25_HOME="${JAVA25_HOME:-$(/usr/libexec/java_home -v 25 2>/dev/null || true)}"
[ -n "$JAVA8_HOME" ] || fail "JDK 8 is required (set JAVA8_HOME if java_home cannot find it)"
[ -n "$JAVA25_HOME" ] || fail "JDK 25 is required (set JAVA25_HOME if java_home cannot find it)"
[ -x "$JAVA8_HOME/bin/javac" ] || fail "JAVA8_HOME does not contain javac: $JAVA8_HOME"
[ -x "$JAVA25_HOME/bin/java" ] || fail "JAVA25_HOME does not contain java: $JAVA25_HOME"

if ! command -v autoconf >/dev/null 2>&1 \
        || ! command -v automake >/dev/null 2>&1 \
        || ! command -v libtool >/dev/null 2>&1; then
    fail "autoconf, automake and libtool are required; install them before running this script"
fi

mkdir -p "$DEPS_DIR"
if [ ! -d "$LWJGL_DIR/.git" ]; then
    git clone "$LWJGL_REPOSITORY" "$LWJGL_DIR"
fi

git -C "$LWJGL_DIR" fetch --no-tags origin "$LWJGL_REF"
git -C "$LWJGL_DIR" checkout --detach "$LWJGL_REF"
if [ "$(git -C "$LWJGL_DIR" rev-parse HEAD)" != "$LWJGL_REF" ]; then
    fail "LWJGL source did not resolve to pinned commit $LWJGL_REF"
fi

LWJGL_MODULES_DIR="$LWJGL_DIR/bin/RELEASE"
LWJGL_IOS_NATIVES_DIR="$LWJGL_DIR/bin/out"

lwjgl_runtime_complete() {
    [ -f "$LWJGL_MODULES_DIR/lwjgl/lwjgl.jar" ] \
        && [ -f "$LWJGL_MODULES_DIR/lwjgl-glfw/lwjgl-glfw.jar" ] \
        && [ -f "$LWJGL_MODULES_DIR/lwjgl-spvc/lwjgl-spvc.jar" ] \
        && [ -f "$LWJGL_IOS_NATIVES_DIR/liblwjgl.dylib" ]
}

if ! lwjgl_runtime_complete; then
    printf 'Building pinned AngelAura LWJGL 3.4.1 iOS runtime (%s)\n' "$LWJGL_REF"
    (
        cd "$LWJGL_DIR"
        export JAVA_HOME="$JAVA25_HOME"
        export JAVA8_HOME="$JAVA8_HOME"
        bash ci_build_ios.bash
    )
fi
lwjgl_runtime_complete || fail "AngelAura LWJGL iOS build completed without the expected module/native outputs"

[ -d "$METALUNIVERSAL_DIR" ] || fail "MetalUniversal checkout not found: $METALUNIVERSAL_DIR (set METALUNIVERSAL_DIR)"
[ -x "$METALUNIVERSAL_DIR/gradlew" ] || fail "MetalUniversal Gradle wrapper not found: $METALUNIVERSAL_DIR/gradlew"

printf 'Building MetalUniversal iOS native bridge and SPIRV-Cross\n'
(
    cd "$METALUNIVERSAL_DIR"
    export JAVA_HOME="$JAVA25_HOME"
    ./gradlew buildIOSNative buildIOSSpvc --stacktrace
)

METALLUM_NATIVE_DIR="$METALUNIVERSAL_DIR/src/main/resources/natives/ios"
[ -f "$METALLUM_NATIVE_DIR/libmetallum.dylib" ] || fail "MetalUniversal buildIOSNative did not produce libmetallum.dylib"
[ -f "$METALLUM_NATIVE_DIR/libspvc.dylib" ] || fail "MetalUniversal buildIOSSpvc did not produce libspvc.dylib"

printf 'Validating Amethyst Java/LWJGL ABI before the full iOS package\n'
make -C "$ROOT_DIR/JavaApp" build/lwjgl.jar \
    BOOTJDK="$JAVA8_HOME/bin" \
    LWJGL_MODULES_DIR="$LWJGL_MODULES_DIR"

printf 'Building Amethyst target %s with coherent LWJGL 3.4.1 + MetalUniversal natives\n' "$BUILD_TARGET"
make -C "$ROOT_DIR" "$BUILD_TARGET" \
    BOOTJDK="$JAVA8_HOME/bin" \
    LWJGL_MODULES_DIR="$LWJGL_MODULES_DIR" \
    LWJGL_IOS_NATIVES_DIR="$LWJGL_IOS_NATIVES_DIR" \
    METALLUM_NATIVE_DIR="$METALLUM_NATIVE_DIR" \
    "$@"
