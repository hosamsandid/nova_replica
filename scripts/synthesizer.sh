#!/usr/bin/env bash
# Synthesizer - Synthesize stage, deterministic except for one sentence per component.
#
#   sync -> prerequisites -> aee-synthesize.py (docs, index, agent inputs)
#        -> register -> commit + push -> report which components need an agent
#
# Woken by the Extract and Link pushes. It only needs the KB records and the intake
# ledger; the Link Graph is optional (without it the diagram, call flow and
# interfaces are omitted and say so). Run it from the repository root.
#
# The agent's part, outside this script: for every component listed at the end, read
# .aee/synth-inputs/<component>.json and write {"purpose", "inputHash"} for it into
# .aee/purposes.json, then run this script again to put the sentences into the documents.
#
# Usage: scripts/synthesizer.sh [--no-sync] [--no-publish] [--aee DIR] [--docs DIR]
#                               [--file-docs DIR]
#   --no-sync      skip `git fetch/checkout/pull` (local experiments)
#   --no-publish   write the files only: no aee-artifact, no commit, no push
#   --aee DIR      pipeline state directory      (default .aee)
#   --docs DIR     where the component documents go (default docs/components)
#   --file-docs DIR  per-file documents of the File Documenter (default docs)
#
# Replies: [PASS] (nothing to document, or every file is already up to date),
#          [ERROR] <message> (exit 1).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
BRANCH=agent_workspace

sync=1
publish=1
aee=".aee"
docs="docs/components"
file_docs="docs"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-sync)    sync=0; shift ;;
        --no-publish) publish=0; shift ;;
        --aee)        aee="${2:?--aee needs a value}"; shift 2 ;;
        --docs)       docs="${2:?--docs needs a value}"; shift 2 ;;
        --file-docs)  file_docs="${2:?--file-docs needs a value}"; shift 2 ;;
        -h|--help)    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "[ERROR] unknown option: $1"; exit 1 ;;
    esac
done

error_exit() {
    echo "[ERROR] $*"
    exit 1
}

check_dependencies() {
    local missing=()
    command -v git > /dev/null 2>&1 || missing+=("git")
    command -v "$PY" > /dev/null 2>&1 || missing+=("$PY")
    if [ "$publish" -eq 1 ] && ! command -v aee-artifact > /dev/null 2>&1; then
        missing+=("aee-artifact (or use --no-publish)")
    fi
    [ -f "$HERE/aee-synthesize.py" ] || missing+=("$HERE/aee-synthesize.py")
    [ -f "$HERE/aee-callflow.py" ] || missing+=("$HERE/aee-callflow.py")
    if [ ${#missing[@]} -gt 0 ]; then
        error_exit "missing required dependencies: ${missing[*]}"
    fi
}

# Step 1: sync with agent_workspace
sync_agent_workspace() {
    echo "Step 1: Syncing with $BRANCH..."
    git fetch origin > /dev/null 2>&1 || error_exit "git fetch origin failed"

    if git ls-remote --exit-code --heads origin "$BRANCH" > /dev/null 2>&1; then
        git checkout "$BRANCH" > /dev/null 2>&1 || error_exit "git checkout $BRANCH failed"
        git pull --ff-only origin "$BRANCH" > /dev/null 2>&1 \
            || error_exit "git pull --ff-only origin $BRANCH failed"
    else
        echo "$BRANCH does not exist on the remote, creating it from main..."
        git checkout -b "$BRANCH" origin/main > /dev/null 2>&1 \
            || error_exit "git checkout -b $BRANCH origin/main failed"
        git push -u origin "$BRANCH" > /dev/null 2>&1 \
            || error_exit "git push -u origin $BRANCH failed"
    fi
}

# Step 2: is there anything to document?
check_prerequisites() {
    echo "Step 2: Checking prerequisites..."
    if [ ! -s "$aee/intake.jsonl" ]; then
        echo "[PASS] No intake ledger ($aee/intake.jsonl), nothing to document"
        exit 0
    fi
    if [ -z "$(find "$aee/kb" -type f -name '*.json' -print -quit 2> /dev/null)" ]; then
        echo "[PASS] No KB records in $aee/kb/, nothing to document"
        exit 0
    fi
    if [ ! -f "$aee/link-graph.json" ]; then
        echo "Warning: no Link Graph: the diagram, call flow and interfaces will be omitted"
    fi
}

# Step 3: generate documents, index and the agent inputs; register, commit and push
run_synthesizer() {
    echo "Step 3: Generating the component documents..."
    local args=(--aee "$aee" --docs "$docs" --file-docs "$file_docs")
    [ "$publish" -eq 1 ] && args+=(--publish)

    local out rc=0
    out=$("$PY" "$HERE/aee-synthesize.py" "${args[@]}" 2>&1) || rc=$?
    case "$rc" in
        0)  echo "$out" ;;
        10) echo "[PASS] Component documents are up to date"; exit 0 ;;
        *)  # the script prints its own [ERROR] line; keep it, do not double it
            case "$out" in
                "[ERROR]"*) echo "$out"; exit 1 ;;
                *) error_exit "aee-synthesize failed (exit $rc): $out" ;;
            esac ;;
    esac
}

# Step 4: hand-off, the components whose purpose sentence an agent has to (re)write
report_agent_work() {
    local todo="$aee/synth-todo.json"
    [ -f "$todo" ] || return 0
    local needed
    needed=$("$PY" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["purposesNeeded"]))' "$todo")
    if [ -n "$needed" ]; then
        echo "Agent hand-off: a purpose sentence is needed for: $needed"
        echo "  inputs: $aee/synth-inputs/<component>.json   output: $aee/purposes.json"
    else
        echo "Agent hand-off: every component has a current purpose sentence"
    fi
}

main() {
    echo "=== Synthesizer ==="
    check_dependencies
    [ "$sync" -eq 1 ] && sync_agent_workspace
    check_prerequisites
    run_synthesizer
    report_agent_work
    echo "=== Synthesize stage completed ==="
}

main
