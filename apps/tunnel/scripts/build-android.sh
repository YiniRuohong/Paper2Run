#!/usr/bin/env bash
# Cross-compile p2r-tunnel for arm64 Android with cargo-ndk.
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"   # rustup toolchain (Homebrew cargo has no Android target)
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$(ls -d "$HOME"/Library/Android/sdk/ndk/* | sort -V | tail -1)}"
cd "$(dirname "$0")/.."
rustup target add aarch64-linux-android >/dev/null
cargo ndk -t arm64-v8a --platform 26 build --release -p p2r-tunnel
file target/aarch64-linux-android/release/p2r-tunnel
