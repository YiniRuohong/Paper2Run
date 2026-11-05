#!/usr/bin/env bash
# Build the tablet App: Rust tunnel for arm64 Android, then the debug APK.
#   apps/android/build-apk.sh            -> app/build/outputs/apk/debug/app-debug.apk
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TUNNEL="$HERE/../tunnel"
"$TUNNEL/scripts/build-android.sh"
mkdir -p "$HERE/app/src/main/jniLibs/arm64-v8a" "$HERE/app/src/main/assets"
cp "$TUNNEL/target/aarch64-linux-android/release/p2r-tunnel" "$HERE/app/src/main/jniLibs/arm64-v8a/libp2rtunnel.so"
cp "$TUNNEL/deploy/remote.json" "$TUNNEL/deploy/relay-ca.pem" "$HERE/app/src/main/assets/"
cd "$HERE"
export JAVA_HOME="${JAVA_HOME:-$(/usr/libexec/java_home -v 17)}"
./gradlew --no-daemon testDebugUnitTest assembleDebug
ls -la app/build/outputs/apk/debug/app-debug.apk
