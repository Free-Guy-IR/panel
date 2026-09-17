#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PANEL_ROOT="${PANEL_ROOT:-/root/dev/panel}"
PY="${PANEL_PYTHON:-$PANEL_ROOT/.venv/bin/python}"
MATRIX="${MATRIX:-$HERE/../../001-content-filtering/experiments/05_real_traffic_matrix.sh}"
OUT="${RESULTS:-$HERE/results.md}"
RESTART_CMD="${RESTART_CMD:-bash /root/dev/tl_restart.sh}"
WORK="$(mktemp -d)"

TOKEN_FILE="${TOKEN_FILE:-/root/dev/.filter_token}"
export TOKEN_FILE PANEL_ROOT
preflight_failed=0
[ -d "$PANEL_ROOT" ] || { echo "PANEL_ROOT does not exist: $PANEL_ROOT" >&2; preflight_failed=1; }
[ -x "$PY" ] || { echo "the panel interpreter is missing or not executable: $PY" >&2; preflight_failed=1; }
[ -r "$TOKEN_FILE" ] || { echo "TOKEN_FILE is missing or unreadable: $TOKEN_FILE" >&2; preflight_failed=1; }
[ -r "$MATRIX" ] || { echo "the content-filter matrix is missing: $MATRIX" >&2; preflight_failed=1; }
[ -r "$HERE/proxies.sh" ] || { echo "proxies.sh is missing next to this runner" >&2; preflight_failed=1; }
if [ "$preflight_failed" != "0" ]; then
    echo "preflight failed; nothing was run and no database was touched" >&2
    exit 2
fi

NAMES=()
CODES=()
PROGRESS="$OUT.progress"
: >"$PROGRESS"

final_status() {
    local rc=$?
    if [ "${SUMMARY_WRITTEN:-0}" = "0" ]; then
        echo
        echo "############ the run ended before the summary was reached (status $rc)"
        printf "%-34s %s\n" "step" "exit"
        while IFS=$'\t' read -r name code; do
            [ -n "$name" ] && printf "%-34s %s\n" "$name" "$code"
        done <"$PROGRESS"
        echo "RUN INCOMPLETE"
    fi
    rm -rf "$WORK"
}
trap final_status EXIT

export PANEL_ROOT

{
    echo "# Raw experiment output — Live Traffic Log"
    echo
    echo "Captured verbatim by \`run_all.sh\` on $(date -u '+%Y-%m-%d %H:%M:%S UTC') from the test panel."
    echo "Every success criterion in \`../spec.md\` traces to one of the blocks below."
    echo
    echo "| step | covers |"
    echo "|---|---|"
    echo "| 00_parse.py | FR-003, FR-016 — the access-line parser and the absence of any source address |"
    echo "| 01_live_e2e.py | SC-001, SC-002, FR-002/003/004, the raw viewer while collecting |"
    echo "| 05_viewer_parity.py | SC-008 — the per-node viewer attached versus detached |"
    echo "| 04_controls.py | FR-012 dropped control, the multi-worker unavailable control |"
    echo "| 16_filter_fixture.py | the content-filter fixture every traffic stage depends on, provisioned by the suite itself |"
    echo "| 02_history_purge.py | SC-003/004/005, FR-006..FR-010 and the status surface |"
    echo "| 03_pause_restart.sh | SC-006, FR-011 no_reports, FR-013 pause |"
    echo "| 05_real_traffic_matrix.sh | SC-008 — the content-filter matrix with collection active |"
    echo
    echo "## Environment"
    echo
    echo '```'
    echo "panel root   $PANEL_ROOT"
    echo "panel url    ${PANEL_URL:-http://127.0.0.1:8001}"
    echo "restart cmd  $RESTART_CMD"
    echo "matrix       $MATRIX"
    echo '```'
} >"$OUT"

run() {
  local label="$1"
  case "$label" in
    0*|1*) bash "$HERE/proxies.sh" up >/dev/null 2>&1 ;;
  esac
    local title="$1"
    shift
    echo
    echo "############ $title"
    local log="$WORK/step.log"
    "$@" >"$log" 2>&1
    local rc=$?
    sed 's/^/    /' <"$log"
    {
        echo
        echo "## $title"
        echo
        echo "\`\`\`"
        cat "$log"
        echo "\`\`\`"
        echo
        echo "exit status: $rc"
    } >>"$OUT"
    NAMES+=("$title")
    CODES+=("$rc")
    printf '%s\t%s\n' "$title" "$rc" >>"$PROGRESS"
    return $rc
}

run "proxies.sh up" bash "$HERE/proxies.sh" up
run "16_filter_fixture.py" "$PY" "$HERE/16_filter_fixture.py"
run "00_parse.py" "$PY" "$HERE/00_parse.py"
run "11_bucket_key.py" "$PY" "$HERE/11_bucket_key.py"
run "01_live_e2e.py" "$PY" "$HERE/01_live_e2e.py"
run "05_viewer_parity.py" "$PY" "$HERE/05_viewer_parity.py"
run "04_controls.py" "$PY" "$HERE/04_controls.py"
run "12_owner_only.py" "$PY" "$HERE/12_owner_only.py"
run "13_cleanup_faults.py" "$PY" "$HERE/13_cleanup_faults.py"
run "14_single_reader.py" "$PY" "$HERE/14_single_reader.py"
run "15_purge_race.py" "$PY" "$HERE/15_purge_race.py"
run "02_history_purge.py" "$PY" "$HERE/02_history_purge.py"
run "03_pause_restart.sh" bash "$HERE/03_pause_restart.sh" "$RESTART_CMD"
run "06_drop_paths.py" "$PY" "$HERE/06_drop_paths.py"
run "07_retention_and_purge.py" "$PY" "$HERE/07_retention_and_purge.py"
run "08_retention_across_processes.py" "$PY" "$HERE/08_retention_across_processes.py"
run "09_disk_reclamation.py" "$PY" "$HERE/09_disk_reclamation.py"
if [ "${RUN_RESTART_RECOVERY:-0}" = "1" ]; then
    run "10_restart_recovery.py (restarts the core)" "$PY" "$HERE/10_restart_recovery.py"
else
    echo "############ 10_restart_recovery.py SKIPPED (set RUN_RESTART_RECOVERY=1 to include it; it restarts core 1)" | tee -a "$OUT"
fi
run "proxies.sh down" bash "$HERE/proxies.sh" down
run "001/05_real_traffic_matrix.sh" bash "$MATRIX"

failed=0
{
    echo
    echo "## Summary"
    echo
    echo "| step | exit status |"
    echo "|---|---|"
} >>"$OUT"
for i in "${!NAMES[@]}"; do
    printf "| %s | %s |\n" "${NAMES[$i]}" "${CODES[$i]}" >>"$OUT"
    [ "${CODES[$i]}" = "0" ] || failed=1
done

SUMMARY_WRITTEN=1
echo
echo "results written to $OUT"
printf "%-34s %s\n" "step" "exit"
for i in "${!NAMES[@]}"; do
    printf "%-34s %s\n" "${NAMES[$i]}" "${CODES[$i]}"
done

if [ "$failed" = "0" ]; then
    echo "ALL EXPERIMENTS PASSED"
else
    echo "SOME EXPERIMENTS FAILED"
fi
exit "$failed"
