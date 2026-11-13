#!/usr/bin/env bash
# Quality audit: read-only validation of what the quality analyzer and reviewer agents pushed.
#
# For the pre-step of the next stage (Quality Reporter) or for a human. It
# changes no file, commits nothing and runs nothing inside the analyzer/reviewer loop. Run it from the
# repository root.
#
# Usage: scripts/quality_audit.sh [--no-sync] [--require-approval] [--aee DIR]
#   --no-sync           skip `git fetch/checkout/pull`
#   --require-approval  also require the reviewer's verdict to be `approve` with no open issue
#
# Replies / exit codes: 0 [PASS] the output is valid (warnings are printed) | 1 [FAIL] errors found,
# the listed problems go back to the analyzer or reviewer | 2 [ERROR] the pre-stage did not complete.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
BRANCH=agent_workspace

sync=1
require=()
aee=".aee"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-sync)           sync=0; shift ;;
        --require-approval)  require=(--require-approval); shift ;;
        --aee)               aee="${2:?--aee needs a value}"; shift 2 ;;
        -h|--help)           sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "[ERROR] unknown option: $1"; exit 2 ;;
    esac
done

command -v "$PY" > /dev/null 2>&1 || { echo "[ERROR] $PY not found"; exit 2; }
if [ "$sync" -eq 1 ]; then
    git fetch origin > /dev/null 2>&1 || { echo "[ERROR] git fetch origin failed"; exit 2; }
    git checkout "$BRANCH" > /dev/null 2>&1 || { echo "[ERROR] git checkout $BRANCH failed"; exit 2; }
    git pull --ff-only origin "$BRANCH" > /dev/null 2>&1 || { echo "[ERROR] git pull --ff-only origin $BRANCH failed"; exit 2; }
fi

rc=0
"$PY" "$HERE/aee-quality-audit.py" --aee "$aee" ${require[@]+"${require[@]}"} || rc=$?
case "$rc" in
    0) echo "[PASS] the quality output is valid" ;;
    1) echo "[FAIL] the quality output has errors; the problems above go back to the analyzer or reviewer" ;;
    *) echo "[ERROR] the pre-stage did not complete" ;;
esac
exit "$rc"
