#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-"$REPOSITORY/.venv/bin/python"}
WORKSPACE=${1:-"$(mktemp -d "${TMPDIR:-/tmp}/reliagent-ml-demo.XXXXXX")"}

mkdir -p "$WORKSPACE"
cd "$REPOSITORY"

run_cli() {
    "$PYTHON_BIN" -m corecoder.reliagent_cli "$@"
}

echo "[1/7] Start the workflow and stop at Approval" >&2
START_OUTPUT=$(run_cli workflow ml start \
    --workspace "$WORKSPACE" \
    --inject-process-loss experiment_after_effect)
printf '%s\n' "$START_OUTPUT"
RUN_ID=$(printf '%s\n' "$START_OUTPUT" | "$PYTHON_BIN" -c \
    'import json, sys; print(json.loads(sys.stdin.readline())["run_id"])')
APPROVAL_ID=$(printf '%s\n' "$START_OUTPUT" | "$PYTHON_BIN" -c \
    'import json, sys; print([json.loads(line) for line in sys.stdin if line.strip()][-1]["pending_approval_id"])')

echo "[2/7] Approve the exact experiment attempt" >&2
run_cli approve "$APPROVAL_ID" --workspace "$WORKSPACE"

echo "[3/7] Resume in a child process; the injected checkpoint exits 87" >&2
set +e
run_cli workflow ml resume "$RUN_ID" --workspace "$WORKSPACE"
PROCESS_EXIT_CODE=$?
set -e
if [ "$PROCESS_EXIT_CODE" -ne 87 ]; then
    echo "expected process exit 87, received $PROCESS_EXIT_CODE" >&2
    exit 1
fi

echo "[4/7] A new process scans the orphan and requires reconciliation" >&2
BLOCKED_OUTPUT=$(run_cli workflow ml resume "$RUN_ID" --workspace "$WORKSPACE")
printf '%s\n' "$BLOCKED_OUTPUT"
TOOL_CALL_ID=$(printf '%s\n' "$BLOCKED_OUTPUT" | "$PYTHON_BIN" -c \
    'import json, sys; print(json.load(sys.stdin)["pending_reconciliation_tool_call_id"])')

echo "[5/7] Reconcile from the persisted artifact evidence, without replay" >&2
run_cli reconcile "$TOOL_CALL_ID" --decision completed --workspace "$WORKSPACE"
COMPLETED_FILE="$WORKSPACE/completed.json"
run_cli workflow ml resume "$RUN_ID" --workspace "$WORKSPACE" > "$COMPLETED_FILE"
cat "$COMPLETED_FILE"

echo "[6/7] Resume twice more; terminal state and effect marker stay unchanged" >&2
run_cli workflow ml resume "$RUN_ID" --workspace "$WORKSPACE" >/dev/null
run_cli workflow ml resume "$RUN_ID" --workspace "$WORKSPACE" >/dev/null

echo "[7/7] Generate the report and emit one machine-readable demo summary" >&2
REPORT_CLI_FILE="$WORKSPACE/report-cli.json"
TRACE_FILE="$WORKSPACE/trace.json"
run_cli workflow ml report "$RUN_ID" --workspace "$WORKSPACE" > "$REPORT_CLI_FILE"
run_cli trace "$RUN_ID" --workspace "$WORKSPACE" > "$TRACE_FILE"

"$PYTHON_BIN" - "$PROCESS_EXIT_CODE" "$COMPLETED_FILE" "$REPORT_CLI_FILE" "$TRACE_FILE" <<'PY'
import json
import sys
from pathlib import Path

process_exit_code = int(sys.argv[1])
completed = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
report_cli = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
trace = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
report = json.loads(Path(report_cli["raw_json"]).read_text(encoding="utf-8"))
print(json.dumps({
    "duplicate_effect": report["experiment"]["duplicate_effect"],
    "effect_count": report["experiment"]["effect_count"],
    "markdown": report_cli["markdown"],
    "process_exit_code": process_exit_code,
    "raw_json": report_cli["raw_json"],
    "run_id": completed["run_id"],
    "status": completed["status"],
    "trace_missing": trace["integrity"]["missing"],
    "trace_sequence_contiguous": trace["integrity"]["sequence_contiguous"],
}, sort_keys=True))
PY
