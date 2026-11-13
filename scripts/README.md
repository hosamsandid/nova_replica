# Running the deterministic pipeline

These stages of the documentation and security pipeline run as scripts, with no model involved: Repo Checker, File Intake Classifier, Fact Extractor, Relationship Linker, Synthesizer, and the scan steps of the security and quality flows. Shell scripts run them. The File Documenter, Code Quality Analyzer, Cross-File Security Correlator, Verifier Critic and the publishers are still agents, and so are the Security and Code Quality Analyzers and Reviewers, which run after their scan and need no script in their loop (sections 5 and 5b).

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
| Security scan | `gitleaks`, `semgrep`, `bandit` | missing tools are reported, never silently skipped |
| Quality scan | `ruff`, `pylint` (Python packages), `shellcheck`, `hadolint`, a JDK with PMD and checkstyle for Java, `lizard` | a missing tool is reported and its language is never called lint-clean |
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

## 5. Security: scan, then two agents

Deterministic tools run only **before** the agents. After `security_pre.sh` no script runs in the loop: the Security Analyzer writes the final output files and pushes them, the Security Reviewer reviews them and decides, and the two repeat until the reviewer approves. The agents use only `git`, `aee-artifact` and `gh` labels (the reviewer registers the artifacts on approval).

```
security_pre.sh  ->  Security Analyzer  ->  Security Reviewer  ->  (revise: back to the analyzer)  ->  approved
 scan, normalise,     fills the skeleton,    reads the output        up to 3 review rounds, then
 create skeleton      writes triage.json     writes verdict.json     escalation to a human
 (script, pushes)     label security:analysis-ready
```

| Step | What happens | Files |
|---|---|---|
| `security_pre.sh` | gitleaks, semgrep, bandit (and spotbugs if classes exist) over the files of the last extracted batch; results merged and normalised; sink patterns and KB surface that nothing covers become coverage gaps (ids `G001`…; a pattern that fires 10 or more times is one gap item with its occurrences); a rule that fires in 10 or more non-strong candidates becomes a **rule group** (`R01`…) the analyzer can drop in one decision after sampling; the **skeleton** is created | `.aee/security-work/{candidates,coverage,scanner_status,files,warnings}.json`; skeleton: `.aee/security/<file>.findings.json` (`[]`), `.aee/security-log.jsonl` (zero counts), `.aee/security-warnings.md` |
| Security Analyzer (`custom_prompts/security_analyzer_v2.md`) | analyses every scanner claim against the code, finds false positives and gaps; fills the findings files and log rows with the final contract format; writes the triage | `.aee/security/<file>.findings.json`, `.aee/security-log.jsonl`, `.aee/security-work/triage.json`; label `security:analysis-ready` |
| Security Reviewer (`custom_prompts/security_reviewer.md`) | reviews every finding, every drop of a high-severity or secret candidate, every gap decision and the integrity of the files; keeps the round counter | `.aee/security-work/verdict.json`; on approval registers `Security:<file>` and `Security Log`; label `security:approved`, `security:revisions-required` (wakes the analyzer) or `security:escalated` |

`triage.json` accounts for every candidate (`keep` with file, line and `cweId` of its finding, or `drop` with a reason, or a whole rule group dropped in one `groups` entry with `sampled` members, a reason of 40+ characters and `except` for members that differ) and every gap (`manual` or `benign`). The review round lives in `verdict.json` (`round`); the analyzer answers a revision by setting `addressedVerdictRound` in `triage.json`.

**Noise: groups.** Scanners often repeat one finding across many files, and a few patterns fire everywhere. Two deterministic mechanisms keep that from swamping the analyzer (both start at `GROUP_MIN`, default 10):

- **Gap groups (`coverage.json`).** A sink pattern or KB-surface pattern that fires 10 or more times becomes ONE gap item with `count`, `files` and `occurrences` (file and line of up to 30 of them) instead of one item per occurrence. The analyzer reads a sample and decides the pattern once (`benign` with a specific reason, or `manual` pointing at a manual finding for an occurrence that is a real issue).
- **Rule groups (`candidates.json`).** A scanner rule that fires in 10 or more candidates becomes a group (`ruleGroups`: `id` `R01`…, `ruleId`, `count`, `files`, `ids`), and each member carries `ruleGroup`. The analyzer may drop the whole group in ONE `groups` entry of `triage.json`:

  ```json
  "groups": [{"group": "R01", "action": "drop", "sampled": ["C010", "C022", "C031"], "except": ["C040"],
              "reason": "each copy is bounded by sizeof(dst), as in src/x.c:L41 and src/y.c:L19"}]
  ```

  The rules: only a **drop** can cover a group (a keep is always decided per candidate); the analyzer must have read at least three members (`sampled`, or all of them if the group is smaller); the reason is at least 40 characters and names what was verified; members that differ are listed in `except` and decided individually; **high-severity and secret candidates are never in a group**.

**How a group decision is verified.** `security_audit.sh` expands each group entry into per-candidate drops and fails if the group does not exist, the action is not `drop`, fewer than `min(3, size)` sampled ids belong to the group, the reason is shorter than 40 characters, or `except` names an id outside the group. An excepted member without its own decision is reported as an undecided candidate. A bulk reason is stated once, so it is not flagged as a copy-pasted reason, while the same reason written individually three times still is. The Security Reviewer re-samples the analyzer's `sampled` members plus at least two of its own, checks that the stated reason holds for each and that every differing member is in `except`, and judges the pattern instead of treating a group drop as a mass drop.

**Safety net, read-only:** `security_audit.sh [--require-approval]` validates the pushed output against the scanner evidence (contract keys, line ranges, CWE/OWASP, scanner-backed findings match a candidate, every candidate and gap decided, log counts, no secret in any file, no duplicates). It changes nothing, so it fits the pre-step of the next stage (Correlator, Reporter) or a human review. Exit `0` valid, `1` errors found, `2` the pre-stage did not complete. `--require-approval` also requires an `approve` verdict with no open issue.

Run `security_pre.sh` and `security_audit.sh` from the repository root; both take `--no-sync`, and `security_pre.sh` also `--no-publish` and file arguments to scan exactly those files. **Environment:** `SEMGREP_RULES_DIR` (offline rules mirror) or `AIRGAPPED=1` (refuses the registry), `GITLEAKS_CONFIG`, `GROUP_MIN` (default 10: how often a rule or gap pattern must fire to be grouped). Raw scanner output goes to `/tmp/scan/security` and is never committed. Secret values never reach any file: snippets are redacted, and both agents and the audit reject any text that repeats one.

## 5b. Quality: scan, then two agents

Same shape as the security flow: deterministic tools run only **before** the agents; after `quality_pre.sh` no script runs in the loop. The Code Quality Analyzer writes the final files and pushes them, the Code Quality Reviewer reviews them and decides, and the two repeat until the reviewer approves (3 review rounds, then escalation to a human).

```
quality_pre.sh  ->  Code Quality Analyzer  ->  Code Quality Reviewer  ->  (revise: back to the analyzer)  ->  approved
 linters, metrics,    fills the skeleton,       reads the output            labels quality:analysis-ready,
 pre-triage,          writes triage.json        writes verdict.json         quality:revisions-required,
 skeleton (script)    (agent, pushes)           (agent, pushes)             quality:approved, quality:escalated
```

| Step | What happens | Files |
|---|---|---|
| `quality_pre.sh` | takes the files of the last intake batch (shell scripts and Dockerfiles are recognised by name or shebang, so no KB record is needed); skips generated, vendored and fixture files and reports them; runs each linter ONCE per language (ruff and pylint for Python, shellcheck, hadolint, PMD and checkstyle for Java) and lizard for every supported language; merges, clusters and pre-triages the hits; creates the skeleton | `quality-work/files.json` (index), `quality-work/candidates/<file>.json`, `tool_status.json`, `warnings.json`; skeleton: `quality/<file>.findings.json` (`[]`), `quality-log.jsonl` (zero counts, `maxComplexity`, `lines`, `toolsRun`), `quality-warnings.md` |
| Code Quality Analyzer (`custom_prompts/quality_analyzer_v2.md`) | judges every tool and metric candidate (keep or drop with a reason), does the manual pass for what no tool sees, fills the findings files and log rows | findings files, log rows, `quality-work/triage.json`; label `quality:analysis-ready` |
| Code Quality Reviewer (`custom_prompts/quality_reviewer.md`) | re-verifies every finding against the code, reads the drops (all drops of metric and error-severity candidates, a sample of the rest, the sampled members of every group), checks the integrity of the files, keeps the round counter | `quality-work/verdict.json`; on approval registers `Quality:<file>` and `Quality Log`; labels as above |
| `quality_audit.sh [--require-approval]` | read-only safety net for the pre-step of the next stage (Quality Reporter) or a human; same exit codes as the security audit | none |

**Pre-triage in `quality_pre.sh`** (all deterministic, ported from the quality-pipeline pre-work): ruff `S*` and pylint `eval`/`exec` rules are dropped because they belong to the Security Analyzer, and so is pylint `E0401`; identical hits of different tools are merged by a rule-equivalence table (ruff `F401` and pylint `W0611` are one candidate); repeated hits of one rule in a file become one cluster with `count` and `occurrences`; metric candidates are CCN above 15, a function above 100 lines, more than 6 parameters, nesting above 4 and a file above 1000 lines. Override the defaults in `.aee/quality-config.json` (`thresholds`, `vendoredDirs`, `fixtureDirs`, `testDirs`, `generatedMarkers`, `shellcheckSeverity`, `hadolintThreshold`). shellcheck runs at `style` severity, because the stricter default hides the two shell issues the pre-work plants (SC2086, SC2002).

**What the tools cannot see is the manual pass.** On the pre-work's planted-issue benchmark (35 issues in 36 files) the tools and metrics give a candidate for 19; 7 more would be covered by hadolint, PMD and checkstyle (not installed on every machine: a missing tool is reported and its files are never called lint-clean), and 9 are manual by nature: documentation, duplication, a missing `set -e`, swallowed errors in JavaScript, Go and C, a TODO and a test without an assert. The analyzer prompt lists exactly these.

**Differences from the security flow.**
- Candidates are per file (`candidates/<file>.json`) with ids `T1…` and `M1…` per file. `triage.json` is keyed by file and lists every analysed file: either it has candidates (`files[<file>].candidates`, each `keep` with `line` and `ruleId`, or `drop` with a reason) or it is in `reviewedWithoutCandidates` once the analyzer has read it for the manual pass.
- Rule groups work across files (`file#T3` members, `GROUP_MIN`, same drop-only and sampling rules as in section 5) and exclude error-severity hits.
- A finding has the nine contract keys; manual findings add `evidenceQuote`, text that must appear verbatim in the cited lines (an optional tenth key, ignored by downstream stages).
- The audit also checks that complexity numbers in a finding equal lizard's, that a metric finding's severity follows the definition (high at twice the threshold, else medium) as a warning, that no finding has a security category or a secret-like value, and that skipped files have no findings.
- `tox -e lint` is not run (it needs virtualenvs and network and its output cannot be normalised); the quality-pipeline pre-work's escalation issues, rejected-fingerprint ledger and GitHub Actions runner are not used. The Quality Reporter's note that quality findings are not audited is now outdated: the reviewer audits them.

## 6. Publishing to git

Without `--no-publish`, each stage registers its artifacts, commits, rebases and pushes to `agent_workspace`, so downstream stages are woken by the same pushes. Before you do this:

- `aee-artifact` must be on `PATH` (this repository has one: `export PATH="$PWD/scripts:$PATH"`).
- A git identity must be configured (`git config user.name` and `user.email`), because the scripts commit.
- `AGENT_ID` (optional) is recorded as the producer in `.agent-studio/artifacts.jsonl`.
- Sync fast-forwards `agent_workspace`; it stops with `[ERROR]` if the branch has diverged from the remote.
- Run `aee-entry.sh`, then `relationship_linker.sh`, then `synthesizer.sh`. The Synthesizer works without the link graph, but its diagram, call flow and interfaces need it.

## 7. Replies and exit codes

| Reply | Exit | Meaning |
|---|---|---|
| (completion line) | 0 | the stage ran |
| `[PASS] ...` | 0 (the Python scripts use 10) | nothing to do: no changes, nothing to link, outputs already up to date |
| `[ERROR] ...` | 1 | a tool, a check or a git command failed; nothing was written for that stage |

`aee-entry.sh` stops at the first `[PASS]` or error, so a stage never runs on missing input.

## 8. Tests

```bash
python3 -I -m unittest discover -s scripts/tests
```

The suite builds throwaway git repositories and, for the stages that call external tools, puts stub tools on `PATH`. It needs `git`; the Linker tests also need `jq` and are skipped without it. It does not touch your repository.

## 9. Known limits

- The Fact Extractor fills `securitySurface`, `sideEffects` and `injection_flags` from fixed pattern rules (Python with `ast`; JavaScript, Go, Java, C and C++ with line regexes). They are coarser than a reader's judgment and an entry point they do not list is not evidence that none exists. Any other language has no rules: the KB record's `warnings` say so, and the Security Analyzer is told to read those files itself. The Synthesizer's Call Flow only uses resolved cross-file calls, because intra-file calls are not in the KB.
- Dependency classification (stdlib vs third-party) is exact for Python only.
- `aee-seams` finds literals with fixed patterns. Project-specific ones go into `.aee/seam-config.json`.
- Language detection is by extension; unknown extensions are flagged `UNKNOWN`. Extend the lists in `.aee/intake-config.json`.
