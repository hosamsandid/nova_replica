#!/usr/bin/env bash
# Entry stages of the pipeline, deterministic:
# Repo Checker -> File Intake Classifier -> Fact Extractor.
#
#   sync -> aee-changed-files -> (PASS? stop) -> aee-intake -> (PASS? stop) -> aee-extract
#
# Each stage commits and pushes its own artifacts (--publish), as the agents did, so
# downstream stages are woken by the same pushes. Run it from the repository root.
#
# Usage: scripts/aee-entry.sh [--source-folder DIR] [--no-sync] [--no-publish]
#                             [--tokei-json FILE]
#   --source-folder  folder to watch; default: `Source folder:` line of ./CLAUDE.md
#   --no-sync        skip `git fetch/checkout/pull` (local experiments)
#   --no-publish     write files only: no aee-artifact, no commit, no push
#   --tokei-json     use this tokei output instead of running tokei (testing)
#
# Exit codes: 0 ran to the end or stopped on [PASS]; 1 a stage failed ([ERROR] shown).
# Stage exit codes of the Python scripts: 0 done, 10 [PASS], anything else = error.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
PASS_CODE=10

source_folder=""
sync=1
publish=1
tokei_json=""

while [ $# -gt 0 ]; do
  case "$1" in
    --source-folder) source_folder="${2:?--source-folder needs a value}"; shift 2 ;;
    --no-sync)       sync=0; shift ;;
    --no-publish)    publish=0; shift ;;
    --tokei-json)    tokei_json="${2:?--tokei-json needs a value}"; shift 2 ;;
    -h|--help)       sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[ERROR] unknown option: $1" >&2; exit 1 ;;
  esac
done

log() { printf '[%s] %s\n' "$1" "$2"; }

# Run one stage; map its exit code to: continue (0), stop cleanly (PASS), fail (other).
# Sets STAGE_RC for the caller.
run_stage() {
  local name="$1"; shift
  set +e
  "$PY" -I "$@"
  STAGE_RC=$?
  set -e
  case "$STAGE_RC" in
    0) log OK "$name" ;;
    "$PASS_CODE") log PASS "$name: nothing to do" ;;
    *) log ERROR "$name failed (exit $STAGE_RC)"; exit 1 ;;
  esac
}

if [ "$publish" -eq 1 ] && ! command -v aee-artifact >/dev/null 2>&1; then
  log ERROR "aee-artifact not found on PATH (use --no-publish for a local run)"; exit 1
fi
if command -v aee-toolchain >/dev/null 2>&1; then
  aee-toolchain doctor || log WARN "aee-toolchain doctor reported problems"
fi
if [ -z "$tokei_json" ] && ! command -v tokei >/dev/null 2>&1; then
  log WARN "tokei not found: the batch will have no line counts"
fi

# --- stage 1: Repo Checker -------------------------------------------------------
args=()
[ -n "$source_folder" ] && args+=(--source-folder "$source_folder")
[ "$sync" -eq 1 ] && args+=(--sync)
[ "$publish" -eq 1 ] && args+=(--publish)
run_stage "repo-checker" "$HERE/aee-changed-files.py" ${args[@]+"${args[@]}"}
[ "$STAGE_RC" -eq "$PASS_CODE" ] && exit 0

# --- stage 2: File Intake Classifier ---------------------------------------------
# Already on an up-to-date agent_workspace from stage 1, so no second sync.
args=()
[ "$publish" -eq 1 ] && args+=(--publish)
[ -n "$tokei_json" ] && args+=(--tokei-json "$tokei_json")
run_stage "file-intake-classifier" "$HERE/aee-intake.py" ${args[@]+"${args[@]}"}
[ "$STAGE_RC" -eq "$PASS_CODE" ] && exit 0

# --- stage 3: Fact Extractor -----------------------------------------------------
# Needs aee-inventory (tree-sitter) and lizard; see `aee-toolchain doctor`.
args=()
[ "$publish" -eq 1 ] && args+=(--publish)
run_stage "fact-extractor" "$HERE/aee-extract.py" ${args[@]+"${args[@]}"}
[ "$STAGE_RC" -eq "$PASS_CODE" ] && exit 0

log OK "extract stage finished: File Documenter, Security Analyzer, Code Quality Analyzer and Relationship Linker can start"
