#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
lock="${KITS_LOCK:-$root/.github/config-kits.lock}"
owner="${KITS_OWNER:?set KITS_OWNER to the GitHub owner of the kit repositories}"
dest="${KITS_DIR:-$root/..}"
base="${KITS_BASE_URL:-https://github.com}"

pinned=()
while read -r kit ref; do
    [ -z "${kit:-}" ] && continue
    if ! [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
        echo "::error::$kit is not pinned to a full commit: '$ref'"
        exit 1
    fi
    dir="$dest/$kit"
    git clone --filter=blob:none --no-checkout "$base/$owner/$kit.git" "$dir"
    git -C "$dir" checkout --detach "$ref"
    got="$(git -C "$dir" rev-parse HEAD)"
    if [ "$got" != "$ref" ]; then
        echo "::error::$kit resolved to $got, expected $ref"
        exit 1
    fi
    echo "$kit pinned at $got"
    pinned+=("$kit@$ref")
done < "$lock"

if [ "${#pinned[@]}" -eq 0 ]; then
    echo "::error::no config kits listed in $lock"
    exit 1
fi

for entry in "${pinned[@]}"; do
    (cd "$dest/${entry%%@*}" && bun install --frozen-lockfile)
done

if [ -n "${GITHUB_ENV:-}" ]; then
    echo "KITS_PINNED=${pinned[*]}" >> "$GITHUB_ENV"
fi
