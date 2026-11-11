#!/usr/bin/env bash
# Builds src/utsu_portal/static/app.css with the Tailwind standalone CLI (no Node/npm). The output is committed so that
# `pip install` works offline; rebuild after changing web/app.src.css or any class used in static/*.js|html.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${TAILWIND_VERSION:-v4.3.5}"
BIN="${TAILWIND_BIN:-}"
if [ -z "$BIN" ]; then
    BIN="web/.tailwindcss-$VERSION"
    if [ ! -x "$BIN" ]; then
        case "$(uname -s)-$(uname -m)" in
            Linux-x86_64) T=linux-x64 ;; Linux-aarch64) T=linux-arm64 ;; Darwin-arm64) T=macos-arm64 ;; Darwin-x86_64) T=macos-x64 ;;
            *) echo "unsupported platform; set TAILWIND_BIN" >&2; exit 1 ;;
        esac
        curl -fsSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/tailwindcss-$T"
        chmod +x "$BIN"
    fi
fi
exec "$BIN" --input web/app.src.css --output src/utsu_portal/static/app.css --minify
