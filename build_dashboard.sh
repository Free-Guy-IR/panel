#! /bin/bash
set -e
cd "$(dirname "$0")/dashboard"
VITE_BUILD_ID="$(date -u +%Y%m%d%H%M%S)" VITE_BASE_API=/ bun run build
cp ./build/index.html ./build/404.html
