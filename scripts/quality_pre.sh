#!/usr/bin/env bash
# Quality pre-stage: run the linters and metrics, normalise and pre-triage their output, create the output skeleton, push it.
#
#   sync -> prerequisites -> aee-quality-scan.py -> register -> commit + push
#
# Run after the Intake stage (it needs the intake ledger; shell scripts and Dockerfiles are found by name, so no KB
# record is required). This is the last script before the agents: the Code Quality Analyzer then reads
# .aee/quality-work/files.json and the per-file candidates, fills in the skeleton (.aee/quality/<file>.findings.json,
# .aee/quality-log.jsonl) and writes triage.json; the Code Quality Reviewer reviews it. Nothing deterministic runs
# after them. Run it from the repository root.
#
# Usage: scripts/quality_pre.sh [--no-sync] [--no-publish] [--aee DIR] [FILE ...]
#   --no-sync     skip `git fetch/checkout/pull` (local experiments)
#   --no-publish  write the files only: no aee-artifact, no commit, no push
#   --aee DIR     pipeline state directory (default .aee)
#   FILE ...      scan exactly these files instead of the last extracted batch
#
# Environment: GROUP_MIN (default 10), PYTHON. Optional config: .aee/quality-config.json (thresholds, directories).
# Replies: [PASS] (no file to scan), [ERROR] <message> (exit 1).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
BRANCH=agent_workspace

sync=1
publish=1
aee=".aee"
files=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-sync)    sync=0; shift ;;
        --no-publish) publish=0; shift ;;
        --aee)        aee="${2:?--aee needs a value}"; shift 2 ;;
        -h|--help)    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "[ERROR] unknown option: $1"; exit 1 ;;
        *)  files+=("$1"); shift ;;
    esac
done

error_exit() { echo "[ERROR] $*"; exit 1; }

check_dependencies() {
    local missing=()
    command -v git > /dev/null 2>&1 || missing+=("git")
    command -v "$PY" > /dev/null 2>&1 || missing+=("$PY")
    if [ "$publish" -eq 1 ] && ! command -v aee-artifact > /dev/null 2>&1; then
        missing+=("aee-artifact (or use --no-publish)")
    fi
    [ -f "$HERE/aee-quality-scan.py" ] || missing+=("$HERE/aee-quality-scan.py")
    [ ${#missing[@]} -eq 0 ] || error_exit "missing required dependencies: ${missing[*]}"
}

sync_agent_workspace() {
    echo "Step 1: Syncing with $BRANCH..."
    git fetch origin > /dev/null 2>&1 || error_exit "git fetch origin failed"
    if git ls-remote --exit-code --heads origin "$BRANCH" > /dev/null 2>&1; then
        git checkout "$BRANCH" > /dev/null 2>&1 || error_exit "git checkout $BRANCH failed"
        git pull --ff-only origin "$BRANCH" > /dev/null 2>&1 || error_exit "git pull --ff-only origin $BRANCH failed"
    else
        git checkout -b "$BRANCH" origin/main > /dev/null 2>&1 || error_exit "git checkout -b $BRANCH origin/main failed"
        git push -u origin "$BRANCH" > /dev/null 2>&1 || error_exit "git push -u origin $BRANCH failed"
    fi
}

check_prerequisites() {
    echo "Step 2: Checking prerequisites..."
    if [ ${#files[@]} -eq 0 ] && [ ! -s "$aee/intake.jsonl" ]; then
        echo "[PASS] No intake ledger ($aee/intake.jsonl), nothing to analyse"
        exit 0
    fi
    if command -v aee-toolchain > /dev/null 2>&1; then
        aee-toolchain doctor > /dev/null 2>&1 || echo "Warning: toolchain doctor reported issues, continuing anyway..."
    fi
}

run_scan() {
    echo "Step 3: Running the linters and metrics and normalising their output..."
    local out rc=0
    out=$("$PY" "$HERE/aee-quality-scan.py" --aee "$aee" ${files[@]+"${files[@]}"} 2>&1) || rc=$?
    case "$rc" in
        0)  echo "$out" ;;
        10) echo "[PASS] No quality file in the last batch"; exit 0 ;;
        *)  case "$out" in
                "[ERROR]"*) echo "$out"; exit 1 ;;
                *) error_exit "aee-quality-scan failed (exit $rc): $out" ;;
            esac ;;
    esac
}

publish_results() {
    echo "Step 4: Registering and pushing the candidates..."
    aee-artifact add "Quality Candidates" "$aee/quality-work/files.json" 2> /dev/null
    git add -A -- "$aee/quality-work" "$aee/quality" "$aee/quality-log.jsonl" "$aee/quality-warnings.md"
    [ -f .agent-studio/artifacts.jsonl ] && git add .agent-studio/artifacts.jsonl
    if git diff --cached --quiet; then
        echo "No changes to commit"
        return
    fi
    git commit -q -m "chore(quality): scan results, candidates and output skeleton"
    git pull --rebase origin "$BRANCH" > /dev/null 2>&1 || { git rebase --abort > /dev/null 2>&1 || true
        error_exit "git pull --rebase origin $BRANCH failed, manual intervention required"; }
    git push origin "$BRANCH" > /dev/null 2>&1 || error_exit "git push origin $BRANCH failed"
}

main() {
    echo "=== Quality pre-stage ==="
    check_dependencies
    [ "$sync" -eq 1 ] && sync_agent_workspace
    check_prerequisites
    run_scan
    [ "$publish" -eq 1 ] && publish_results
    echo "=== Done: the Code Quality Analyzer reads $aee/quality-work/files.json and fills in $aee/quality/ ==="
}

main
