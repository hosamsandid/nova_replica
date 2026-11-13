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
#                             [--tokei-json FILE] [--reset]
#   --source-folder  folder to watch; default: `Source folder:` line of ./CLAUDE.md
#   --no-sync        skip `git fetch/checkout/pull` (local experiments)
#   --no-publish     write files only: no aee-artifact, no commit, no push
#   --tokei-json     use this tokei output instead of running tokei (testing)
#   --reset          archive existing pipeline state before running (requires confirmation)
#

# PATH="$PWD/scripts:$PATH" scripts/aee-entry.sh --source-folder nova
# Exit codes: 0 ran to the end or stopped on [PASS]; 1 a stage failed ([ERROR] shown).
# Stage exit codes of the Python scripts: 0 done, 10 [PASS], anything else = error.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
GIT_CONFIG_FILE="$REPO_ROOT/.git-config.json"
PY="${PYTHON:-python3}"
PASS_CODE=10

# Set tree-sitter grammar cache to a writable location
export AEE_TS_CACHE="${AEE_TS_CACHE:-$HOME/.cache/aee-ts-grammars}"

# Ensure scripts directory is in PATH for aee-artifact and other tools
export PATH="$HERE:$PATH"

# Function to read JSON config values
read_git_config() {
  local key="$1"
  local default="${2:-}"

  if [ ! -f "$GIT_CONFIG_FILE" ]; then
    echo "$default"
    return
  fi

  # Try jq first, fall back to python
  if command -v jq >/dev/null 2>&1; then
    jq -r "$key // \"$default\"" "$GIT_CONFIG_FILE" 2>/dev/null || echo "$default"
  else
    $PY -c "import json; data=json.load(open('$GIT_CONFIG_FILE')); keys='$key'.strip('.').split('.'); result=data; [result:=result.get(k, {}) for k in keys]; print(result if isinstance(result, str) else '$default')" 2>/dev/null || echo "$default"
  fi
}

# Load git configuration
GIT_REPO_URL="$(read_git_config '.repository.url' '')"
GIT_AGENT_BRANCH="$(read_git_config '.repository.agent_branch' 'agent_workspace')"
GIT_MAIN_BRANCH="$(read_git_config '.repository.main_branch' 'main')"
GIT_USER_NAME="$(read_git_config '.user.name' '')"
GIT_USER_EMAIL="$(read_git_config '.user.email' '')"
DEFAULT_SYNC="$(read_git_config '.defaults.auto_sync' 'true')"
DEFAULT_PUBLISH="$(read_git_config '.defaults.auto_publish' 'true')"

# Convert boolean strings to 0/1
[ "$DEFAULT_SYNC" = "true" ] && sync=1 || sync=0
[ "$DEFAULT_PUBLISH" = "true" ] && publish=1 || publish=0

source_folder=""
tokei_json=""
reset=0

while [ $# -gt 0 ]; do
  case "$1" in
    --source-folder) source_folder="${2:?--source-folder needs a value}"; shift 2 ;;
    --no-sync)       sync=0; shift ;;
    --no-publish)    publish=0; shift ;;
    --tokei-json)    tokei_json="${2:?--tokei-json needs a value}"; shift 2 ;;
    --reset)         reset=1; shift ;;
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

# Get source folder from CLAUDE.md
get_source_folder_from_claude_md() {
  if [ -f "CLAUDE.md" ]; then
    grep -m1 '^Source folder:' CLAUDE.md 2>/dev/null | sed 's/^Source folder:[[:space:]]*//' || true
  fi
}

# Change to repository root early so all relative paths work correctly
cd "$REPO_ROOT"

# --- Interactive prompts for missing required inputs ---------------------------------
# Determine source folder if not provided via command line
if [ -z "$source_folder" ]; then
  source_folder=$(get_source_folder_from_claude_md)
  if [ -z "$source_folder" ]; then
    echo ""
    echo "No source folder specified and none found in CLAUDE.md"
    echo -n "Enter source folder to analyze: "
    read -r source_folder
    if [ -z "$source_folder" ]; then
      log ERROR "Source folder is required"; exit 1
    fi
  else
    log OK "Using source folder from CLAUDE.md: $source_folder"
  fi
fi

# Verify source folder exists
if [ ! -d "$source_folder" ]; then
  log ERROR "Source folder does not exist: $source_folder"; exit 1
fi

if [ "$publish" -eq 1 ] && ! command -v aee-artifact >/dev/null 2>&1; then
  log ERROR "aee-artifact not found on PATH (use --no-publish for a local run)"; exit 1
fi
if command -v aee-toolchain >/dev/null 2>&1; then
  aee-toolchain doctor || log WARN "aee-toolchain doctor reported problems"
fi
if [ -z "$tokei_json" ] && ! command -v tokei >/dev/null 2>&1; then
  log WARN "tokei not found: the batch will have no line counts"
fi

# --- Reset: Remove existing pipeline state ------------------------------------------
if [ "$reset" -eq 1 ]; then
  if [ -d ".aee" ] || [ -d ".agent-studio" ]; then
    echo ""
    echo "WARNING: The --reset flag will PERMANENTLY DELETE pipeline folders:"
    [ -d ".aee" ] && echo "  - .aee/ (changed-files.json, intake.jsonl, kb/, etc.)"
    [ -d ".agent-studio" ] && echo "  - .agent-studio/ (artifacts.jsonl, etc.)"
    echo ""
    echo "This action CANNOT be undone!"
    echo ""
    echo -n "Do you want to continue? (YES/NO): "
    read -r confirmation

    if [ "$confirmation" = "YES" ]; then
      log OK "Removing pipeline state..."
      [ -d ".aee" ] && rm -rf ".aee" && log OK "Deleted .aee folder"
      [ -d ".agent-studio" ] && rm -rf ".agent-studio" && log OK "Deleted .agent-studio folder"
      log OK "Starting pipeline with fresh state..."
      echo ""
    elif [ "$confirmation" = "NO" ]; then
      log OK "Reset cancelled by user. Exiting."
      exit 0
    else
      log ERROR "Invalid response. Please answer YES or NO (capital letters). Exiting."
      exit 1
    fi
  else
    log OK "Pipeline folders do not exist. Starting pipeline with fresh state..."
    echo ""
  fi
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
