# Running the deterministic pipeline

Five stages of the documentation pipeline run as scripts, with no model involved: Repo Checker, File Intake Classifier, Fact Extractor, Relationship Linker and Synthesizer. Three shell scripts run them. The other stages (File Documenter, Security Analyzer, Code Quality Analyzer, Cross-File Security Correlator, Verifier Critic and the publishers) are still agents.

```
aee-entry.sh                       relationship_linker.sh        synthesizer.sh
Repo Checker -> Intake -> Extractor  ->  Relationship Linker  ->  Synthesizer
 aee-changed-files.py  aee-intake.py        aee-link.py              aee-synthesize.py
                       aee-extract.py       aee-seams.py             aee-callflow.py
                                            aee-link-report.py
```

Every script is run **from the repository root**. Each one reads and writes `.aee/` (pipeline state) and, for the Synthesizer, `docs/components/`.

## 1. Prerequisites

| Needed by | What | Notes |
|---|---|---|
| all | `bash`, `git`, `python3` (3.10 or newer) | `python3` can be overridden with `PYTHON=/path/to/python` |
| Intake | `tokei` | `brew install tokei`. Without it the run still works, but no line counts are recorded |
| Extractor | `tree-sitter-language-pack` and `lizard` (Python packages) | see below. `aee-inventory.py` and `lizard` do the parsing |
| Linker | `jq` | `brew install jq` |
| publishing | `aee-artifact` on `PATH`, and a git identity | see "Publishing" |

Installing the Python tools into a virtualenv (the first run downloads tree-sitter grammars, so it needs network access):

```bash
python3 -m venv /tmp/aee-venv
/tmp/aee-venv/bin/pip install tree-sitter-language-pack lizard
export PYTHON=/tmp/aee-venv/bin/python
export PATH="/tmp/aee-venv/bin:$PATH"        # puts `lizard` on PATH
export AEE_TS_CACHE=/tmp/aee-ts              # writable directory for the grammars
```

`aee-link`, `aee-seams` and `aee-inventory` do not need to be on `PATH`: the scripts use the `.py` files next to them when the commands are missing. `aee-toolchain` is optional; the doctor step is skipped without it.

## 2. Quick start: a local run that pushes nothing

This is the safest way to try the pipeline. It writes files only: no `aee-artifact`, no commit, no push.

```bash
scripts/aee-entry.sh          --source-folder src/ --no-sync --no-publish
scripts/relationship_linker.sh                      --no-sync --no-publish
scripts/synthesizer.sh                              --no-sync --no-publish
```

Results appear in `.aee/` (manifest, ledger, KB records, link graph) and `docs/components/` (component documents and index).

To run on **one folder only**, without touching your repository, use a scratch repository that holds just those sources:

```bash
W=/tmp/aee-run && rm -rf $W && mkdir $W
git archive HEAD src/nova/db | tar -x -C $W          # the sources to analyse
cd $W && git init -q -b agent_workspace && git add -A \
  && git -c user.name=t -c user.email=t@example.com commit -q -m sources
/path/to/repo/scripts/aee-entry.sh --source-folder src/nova/db/ --no-sync --no-publish
/path/to/repo/scripts/relationship_linker.sh --no-sync --no-publish
/path/to/repo/scripts/synthesizer.sh --no-sync --no-publish
```

## 3. The scripts

### `aee-entry.sh` : Repo Checker, Intake, Fact Extractor

| Option | Meaning |
|---|---|
| `--source-folder DIR` | folder to analyse. Default: the `Source folder:` line of `./CLAUDE.md` |
| `--no-sync` | skip `git fetch / checkout / pull` of `agent_workspace` |
| `--no-publish` | no `aee-artifact`, no commit, no push |
| `--tokei-json FILE` | use this tokei output instead of running tokei |

Stages run in order and stop early on `[PASS]`:

1. **Repo Checker** (`aee-changed-files.py`): files added or modified under the source folder since the last run (`.aee/changed-files.json`). On the first run, every file counts as added.
2. **Intake** (`aee-intake.py`): language by extension, component from `.aee/components.json`, hash, line counts; appends to `.aee/intake.jsonl`; writes `intake-summary.md` and `source-overview.md`.
3. **Fact Extractor** (`aee-extract.py`): one KB record per source file in `.aee/kb/` and a line in `.aee/extract-log.jsonl`.

### `relationship_linker.sh` : Relationship Linker

Options: `--no-sync`, `--no-publish`. Builds `.aee/link-graph.json` (file edges, resolved references, seams) and `.aee/link-summary.md`. It refuses to write a summary if the graph fails its integrity checks. Replies `[PASS]` if there is nothing to link or the graph is unchanged.

### `synthesizer.sh` : Synthesizer

Options: `--no-sync`, `--no-publish`, `--aee DIR`, `--docs DIR`, `--file-docs DIR`. Writes one document per component and an index to `docs/components/`, plus the agent's input bundles to `.aee/synth-inputs/`. `--file-docs` points at the File Documenter's per-file documents (default `docs`); their Overview paragraphs go into the bundles. If the Link Graph is missing, the diagram, call flow and interfaces are omitted and say so.

## 4. The one agent step: purpose sentences

The Synthesizer cannot write what a component is for. At the end of its run it prints which components need a sentence, for example:

```
Agent hand-off: a purpose sentence is needed for: api db main
  inputs: .aee/synth-inputs/<component>.json   output: .aee/purposes.json
```

An agent reads each `.aee/synth-inputs/<component>.json` and writes `.aee/purposes.json`:

```json
{ "db": { "purpose": "One or two sentences.", "inputHash": "<copied from the bundle>" } }
```

Then run `synthesizer.sh` again. The sentences appear in the documents and the index. A purpose whose `inputHash` no longer matches is still shown, marked _(to be refreshed)_, and listed in `.aee/synth-todo.json`, so only changed components need the agent again.

## 5. Publishing to git

Without `--no-publish`, each stage registers its artifacts, commits, rebases and pushes to `agent_workspace`, so downstream stages are woken by the same pushes. Before you do this:

- `aee-artifact` must be on `PATH` (this repository has one: `export PATH="$PWD/scripts:$PATH"`).
- A git identity must be configured (`git config user.name` and `user.email`), because the scripts commit.
- `AGENT_ID` (optional) is recorded as the producer in `.agent-studio/artifacts.jsonl`.
- Sync fast-forwards `agent_workspace`; it stops with `[ERROR]` if the branch has diverged from the remote.
- Run `aee-entry.sh`, then `relationship_linker.sh`, then `synthesizer.sh`. The Synthesizer works without the link graph, but its diagram, call flow and interfaces need it.

## 6. Replies and exit codes

| Reply | Exit | Meaning |
|---|---|---|
| (completion line) | 0 | the stage ran |
| `[PASS] ...` | 0 (the Python scripts use 10) | nothing to do: no changes, nothing to link, outputs already up to date |
| `[ERROR] ...` | 1 | a tool, a check or a git command failed; nothing was written for that stage |

`aee-entry.sh` stops at the first `[PASS]` or error, so a stage never runs on missing input.

## 7. Tests

```bash
python3 -I -m unittest discover -s scripts/tests
```

The suite builds throwaway git repositories and, for the stages that call external tools, puts stub tools on `PATH`. It needs `git`; the Linker tests also need `jq` and are skipped without it. It does not touch your repository.

## 8. Known limits

- The Fact Extractor leaves `sideEffects`, `securitySurface` and `injection_flags` empty. The Synthesizer's Side Effects section says so; its Call Flow only uses resolved cross-file calls, because intra-file calls are not in the KB.
- Dependency classification (stdlib vs third-party) is exact for Python only.
- `aee-seams` finds literals with fixed patterns. Project-specific ones go into `.aee/seam-config.json`.
- Language detection is by extension; unknown extensions are flagged `UNKNOWN`. Extend the lists in `.aee/intake-config.json`.
