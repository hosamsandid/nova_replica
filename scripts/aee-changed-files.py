#!/usr/bin/env python3
"""Deterministic replacement of the Repo Checker agent.

Finds the files added or modified under the source folder since the previous run
and writes the `Changed Files` manifest (.aee/changed-files.json). Reads paths and
statuses from git only, never file contents. Deletions are out of scope.

Usage: aee-changed-files [--source-folder DIR | --claude-md FILE] [--branch B]
                         [--out FILE] [--sync] [--publish]

  --source-folder  folder to watch; else the `Source folder:` line of --claude-md
                   (default ./CLAUDE.md). Normalised: no leading `/` or `./`,
                   exactly one trailing `/`; nothing left means the whole repository.
  --sync           first run `git fetch`, check out and fast-forward the branch
                   (creates it from origin/main if the remote lacks it).
  --publish        afterwards register the artifact, commit, rebase and push.

Exit code and stdout:
  0   manifest written
  10  `[PASS]`   nothing added or modified under the source folder; nothing written
  1   `[ERROR] <failing command and message>`; nothing written

With the whole repository as source folder, `.aee/` and `.agent-studio/` (the
pipeline's own output) are excluded.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PASS, ERROR = 10, 1
PIPELINE_DIRS = (".aee/", ".agent-studio/")


class StageError(Exception):
    pass


def git(*args, check=True):
    p = subprocess.run(["git", *args], capture_output=True)
    if check and p.returncode != 0:
        msg = p.stderr.decode(errors="replace").strip() or f"exit {p.returncode}"
        raise StageError(f"git {' '.join(args)}: {msg}")
    return p


def normalise_folder(raw):
    folder = raw.strip()
    while folder.startswith("./") or folder.startswith("/"):
        folder = folder[2:] if folder.startswith("./") else folder[1:]
    folder = folder.strip("/")
    return folder + "/" if folder and folder != "." else "."


def read_source_folder(claude_md):
    path = Path(claude_md)
    if not path.is_file():
        raise StageError(f"no --source-folder given and {claude_md} not found")
    for line in path.read_text().splitlines():
        m = re.match(r"\s*Source folder:\s*(.*)$", line)
        if m:
            return m.group(1)
    raise StageError(f"no `Source folder:` line in {claude_md}")


def sync(branch):
    git("fetch", "origin")
    if git("ls-remote", "--exit-code", "--heads", "origin", branch, check=False).returncode != 0:
        git("checkout", "-b", branch, "origin/main")
        git("push", "-u", "origin", branch)
        return
    git("checkout", branch)
    git("pull", "--ff-only", "origin", branch)


def previous_sha(out_path, after_sha):
    """afterSha of the previous manifest if it is reachable from HEAD, else None."""
    if not out_path.is_file():
        return None
    try:
        before = json.loads(out_path.read_text()).get("afterSha")
    except (ValueError, OSError):
        return None
    if not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{7,64}", before):
        return None
    if git("cat-file", "-e", f"{before}^{{commit}}", check=False).returncode != 0:
        return None
    if git("merge-base", "--is-ancestor", before, after_sha, check=False).returncode != 0:
        return None
    return before


def list_changes(before, after, folder):
    """[(path, status)] of added or modified files under folder."""
    pathspec = folder if folder != "." else "."
    if before:
        raw = git("diff", "-z", "--name-status", "--no-renames", "--diff-filter=AMT",
                  before, after, "--", pathspec).stdout
        fields = raw.decode("utf-8", errors="surrogateescape").split("\0")
        fields = [f for f in fields if f != ""]
        if len(fields) % 2:
            raise StageError("git diff: unexpected --name-status -z output")
        pairs = list(zip(fields[0::2], fields[1::2]))
        changes = [(p, "added" if s == "A" else "modified") for s, p in pairs]
    else:
        raw = git("ls-files", "-z", "--", pathspec).stdout
        paths = [p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p]
        changes = [(p, "added") for p in paths]
    if folder == ".":
        changes = [c for c in changes if not c[0].startswith(PIPELINE_DIRS)]
    return changes


def publish(out_path, branch):
    artifact = shutil.which("aee-artifact")
    if not artifact:
        raise StageError("aee-artifact: not found on PATH")
    subprocess.run([artifact, "add", "Changed Files", str(out_path)], check=True)
    git("add", str(out_path), ".agent-studio/artifacts.jsonl")
    git("commit", "-m", "chore: update changed-files manifest")
    git("pull", "--rebase", "origin", branch)
    git("push", "origin", branch)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source-folder")
    ap.add_argument("--claude-md", default="CLAUDE.md")
    ap.add_argument("--branch", default="agent_workspace")
    ap.add_argument("--out", default=".aee/changed-files.json")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args(argv)
    out_path = Path(a.out)
    try:
        folder = normalise_folder(a.source_folder if a.source_folder is not None
                                  else read_source_folder(a.claude_md))
        if a.sync:
            sync(a.branch)
        after = git("rev-parse", "HEAD").stdout.decode().strip()
        before = previous_sha(out_path, after)
        changes = list_changes(before, after, folder)
        if not changes:
            print("[PASS]")
            return PASS
        manifest = {
            "branch": a.branch,
            "sourceFolder": folder,
            "beforeSha": before,
            "afterSha": after,
            "files": [{"path": p, "status": s} for p, s in changes],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        if a.publish:
            publish(out_path, a.branch)
        print(f"{out_path}: {len(changes)} files ({after[:8]})")
        return 0
    except StageError as e:
        print(f"[ERROR] {e}")
        return ERROR
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {' '.join(map(str, e.cmd))}: exit {e.returncode}")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
