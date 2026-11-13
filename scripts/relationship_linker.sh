#!/usr/bin/env bash
# Relationship Linker - Link stage, deterministic (no LLM).
#
#   sync -> prerequisites -> doctor -> aee-link -> aee-seams -> assemble graph
#        -> verify + summary (aee-link-report.py) -> register -> commit + push
#
# Woken by the Fact Extractor's push, as a sibling of the Documenter and the analyzers:
# it only needs the KB records, so it is a separate stage, not part of aee-entry.sh.
# Run it from the repository root.
#
# Usage: scripts/relationship_linker.sh [--no-sync] [--no-publish]
#   --no-sync     skip `git fetch/checkout/pull` (local experiments)
#   --no-publish  write .aee/link-graph.json and .aee/link-summary.md only: no
#                 aee-artifact, no commit, no push
#
# Replies: [PASS] (nothing to link, or the graph is unchanged: nothing written),
#          [ERROR] <message> (exit 1: a tool, the verification or git failed).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
SCAN=/tmp/scan   # raw tool output only; never committed
BRANCH=agent_workspace

sync=1
publish=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-sync)    sync=0; shift ;;
        --no-publish) publish=0; shift ;;
        -h|--help)    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "[ERROR] unknown option: $1"; exit 1 ;;
    esac
done

###############################################################################
# Helper Functions
###############################################################################

error_exit() {
    echo "[ERROR] $*"
    exit 1
}

# Run a pipeline tool: the command on PATH, else the sibling <name>.py.
run_tool() {
    local name="$1"; shift
    if command -v "$name" > /dev/null 2>&1; then
        "$name" "$@"
    elif [ -f "$HERE/$name.py" ]; then
        "$PY" "$HERE/$name.py" "$@"
    else
        echo "$name: not found (neither on PATH nor as $HERE/$name.py)" >&2
        return 127
    fi
}

# Resolve an artifact path by name, falling back to its well-known default.
artifact_path() {
    local name="$1" default="$2" p=""
    if command -v aee-artifact > /dev/null 2>&1; then
        p=$(aee-artifact path "$name" 2> /dev/null || true)
    fi
    echo "${p:-$default}"
}

check_dependencies() {
    local missing=()
    command -v git > /dev/null 2>&1 || missing+=("git")
    command -v jq > /dev/null 2>&1 || missing+=("jq")
    if [ "$publish" -eq 1 ] && ! command -v aee-artifact > /dev/null 2>&1; then
        missing+=("aee-artifact (or use --no-publish)")
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        error_exit "missing required dependencies: ${missing[*]}"
    fi
}

###############################################################################
# Steps
###############################################################################

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

# Step 2: something to link?
check_prerequisites() {
    echo "Step 2: Checking prerequisites..."

    local extract_log
    extract_log=$(artifact_path "Extract Log" ".aee/extract-log.jsonl")
    if [ ! -f "$extract_log" ]; then
        echo "[PASS] No Extract Log found, nothing to link"
        exit 0
    fi

    # at least one line without an `error` field
    if ! jq -s -e 'any(.[]; has("error") | not)' "$extract_log" > /dev/null 2>&1; then
        echo "[PASS] Extract Log has no successful entries"
        exit 0
    fi

    if [ -z "$(find .aee/kb -type f -name '*.json' -print -quit 2> /dev/null)" ]; then
        echo "[PASS] No KB records found in .aee/kb/"
        exit 0
    fi
}

# Step 3: toolchain doctor (informational)
run_toolchain_doctor() {
    echo "Step 3: Running toolchain doctor..."
    if command -v aee-toolchain > /dev/null 2>&1; then
        aee-toolchain doctor > /dev/null 2>&1 \
            || echo "Warning: toolchain doctor reported issues, continuing anyway..."
    else
        echo "Warning: aee-toolchain not found, skipping the doctor"
    fi
}

# Step 3b: file graph from all KB records (not only the last batch)
build_file_graph() {
    echo "Building file graph with aee-link..."
    mkdir -p "$SCAN"
    run_tool aee-link --kb .aee/kb > "$SCAN/link.json" 2> "$SCAN/link.err" \
        || error_exit "aee-link: $(head -c 500 "$SCAN/link.err")"
    [ -s "$SCAN/link.json" ] || error_exit "aee-link: produced no output"
    jq -e '.fileEdges and .resolved and .summary' "$SCAN/link.json" > /dev/null 2>&1 \
        || error_exit "aee-link: output is not a link graph"
}

# Step 4: seams of the source folder
find_seams() {
    echo "Step 4: Finding seams..."
    local changed_files source_folder="."
    changed_files=$(artifact_path "Changed Files" ".aee/changed-files.json")
    if [ -f "$changed_files" ]; then
        source_folder=$(jq -r '.sourceFolder // "."' "$changed_files" 2> /dev/null || echo ".")
    fi
    echo "Scanning source folder: $source_folder"

    run_tool aee-seams "$source_folder" --repo-root . --json > "$SCAN/seams.json" 2> "$SCAN/seams.err" \
        || error_exit "aee-seams: $(head -c 500 "$SCAN/seams.err")"
    jq -e 'type == "array"' "$SCAN/seams.json" > /dev/null 2>&1 \
        || error_exit "aee-seams: output is not a list"
}

# Step 5: assemble link-graph.json; identical to the existing one means [PASS]
assemble_link_graph() {
    echo "Step 5: Assembling link-graph.json..."

    jq -s '{
        fileEdges: .[0].fileEdges,
        resolved: .[0].resolved,
        ambiguous: .[0].ambiguous,
        unresolved: .[0].unresolved,
        summary: (.[0].summary + {seams: (.[1] | length)}),
        warnings: .[0].warnings,
        crossComponentSeams: .[1]
    }' "$SCAN/link.json" "$SCAN/seams.json" > "$SCAN/link-graph.json"

    if [ -f .aee/link-graph.json ] && cmp -s "$SCAN/link-graph.json" .aee/link-graph.json; then
        echo "[PASS] Link graph unchanged"
        exit 0
    fi
    mkdir -p .aee
    cp "$SCAN/link-graph.json" .aee/link-graph.json
}

# Steps 6 and 8: verify the graph, then write link-summary.md (nothing is written on failure)
generate_summary() {
    echo "Steps 6 and 8: Verifying the graph and writing link-summary.md..."
    "$PY" "$HERE/aee-link-report.py" --graph .aee/link-graph.json --out .aee/link-summary.md \
        || exit 1
}

# Step 7: register
register_artifacts() {
    echo "Step 7: Registering artifacts..."
    aee-artifact add "Link Graph" .aee/link-graph.json
    aee-artifact add "Link Summary" .aee/link-summary.md
}

# Step 9: commit and push
commit_and_push() {
    echo "Step 9: Committing and pushing..."
    git add .aee/link-graph.json .aee/link-summary.md
    [ -f .agent-studio/artifacts.jsonl ] && git add .agent-studio/artifacts.jsonl

    if git diff --cached --quiet; then
        echo "No changes to commit"
        return
    fi
    git commit -q -m "chore(link): add relationship link graph and summary"

    if ! git pull --rebase origin "$BRANCH" > /dev/null 2>&1; then
        git rebase --abort > /dev/null 2>&1 || true
        error_exit "git pull --rebase origin $BRANCH failed, manual intervention required"
    fi
    git push origin "$BRANCH" > /dev/null 2>&1 || error_exit "git push origin $BRANCH failed"
}

###############################################################################
# Main
###############################################################################

main() {
    echo "=== Relationship Linker ==="
    check_dependencies
    [ "$sync" -eq 1 ] && sync_agent_workspace
    check_prerequisites
    run_toolchain_doctor
    build_file_graph
    find_seams
    assemble_link_graph
    generate_summary
    if [ "$publish" -eq 1 ]; then
        register_artifacts
        commit_and_push
    fi
    echo "=== Link stage completed ==="
}

main
