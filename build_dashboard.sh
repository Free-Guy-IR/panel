#! /bin/bash
set -e
cd "$(dirname "$0")/dashboard"
# Changes on every build and on nothing else, which is what makes the
# locale files cacheable without going stale after a deploy.
VITE_BUILD_ID="$(date -u +%Y%m%d%H%M%S)" VITE_BASE_API=/ bun run build
cp ./build/index.html ./build/404.html
