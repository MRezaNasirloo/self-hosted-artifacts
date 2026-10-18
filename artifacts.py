#!/usr/bin/env python3
"""Passes files between jobs of a GitHub Actions run through a folder on a self-hosted runner's
machine, instead of GitHub's artifact storage.

    artifacts.py save <name>    saves the files matching ARTIFACT_PATH (one pattern per line) as <name>
    artifacts.py load <name>    puts <name>'s files back where they were saved from

Every runner the jobs use must be on the same machine. Files are kept under ARTIFACTS_DIR
(default ~/self-hosted-artifacts), in <owner>_<repository>/<key>/<name>, at their paths inside
the checkout. The key is ARTIFACT_KEY, or the run ID when it's empty, so by default each run has
its own folder and re-running a job finds it again. A key such as a commit ID lets later runs
load the files too.

Each save deletes this repository's other keys under ARTIFACTS_DIR that haven't been saved to for
RETENTION_DAYS (default 2). Use a separate ARTIFACTS_DIR for files kept for a different time.

Set IF_NO_FILES_FOUND to warn or ignore to save nothing, instead of failing, when no file matches.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sys
import time
import uuid

MANIFEST = "manifest.json"


class Failure(Exception):
    pass


def root() -> str:
    return os.path.expanduser(os.environ.get("ARTIFACTS_DIR") or "~/self-hosted-artifacts")


def keep_days() -> float:
    try:
        days = float(os.environ.get("RETENTION_DAYS") or 2)
    except ValueError:
        days = 0
    if days <= 0:
        raise Failure("retention-days must be a number above 0.")
    return days


def repo_dir() -> str:
    return os.path.join(root(), os.environ["GITHUB_REPOSITORY"].replace("/", "_"))


def key() -> str:
    return os.environ.get("ARTIFACT_KEY") or os.environ["GITHUB_RUN_ID"]


def check_folder_name(kind: str, value: str) -> None:
    if not value or "/" in value or value.startswith("."):
        raise Failure(f"'{value}' can't be used as a {kind}.")


def saved_dir(name: str) -> str:
    check_folder_name("name", name)
    check_folder_name("key", key())
    return os.path.join(repo_dir(), key(), name)


def workspace() -> str:
    return os.path.realpath(os.environ.get("GITHUB_WORKSPACE") or os.getcwd())


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def megabytes(size: int) -> str:
    return f"{size / 1e6:.1f} MB"


def summary(*lines: str) -> None:
    """Adds lines to the run's summary page on GitHub."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n\n")


def matching_files(patterns: list[str]) -> list[str]:
    """Files matching the patterns, as paths inside the checkout. Folders count as all they hold."""
    base = workspace()
    found: set[str] = set()
    for pattern in patterns:
        for match in glob.glob(os.path.join(base, pattern), recursive=True):
            paths = [match] if os.path.isfile(match) else [
                os.path.join(folder, file) for folder, _, files in os.walk(match) for file in files]
            for path in paths:
                # Saved under its own path, but a link must not lead outside the checkout either.
                relative = os.path.relpath(os.path.abspath(path), base)
                target = os.path.relpath(os.path.realpath(path), base)
                if relative.startswith("..") or target.startswith(".."):
                    raise Failure(f"{pattern} reaches outside the checkout.")
                found.add(relative)
    return sorted(found)


def sweep() -> None:
    """Deletes this repository's keys older than the keep time, except the current one.

    Best effort: another save may be sweeping at the same moment, and cleanup must never fail a save.
    """
    cutoff = time.time() - keep_days() * 86400
    try:
        entries = list(os.scandir(repo_dir()))
    except OSError:
        return
    for entry in entries:
        try:
            if (entry.name != key() and entry.is_dir(follow_symlinks=False)
                    and entry.stat().st_mtime < cutoff):
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


def save(name: str) -> None:
    target = saved_dir(name)
    keep_days()  # Reject a bad retention before saving anything.
    patterns = [line.strip() for line in os.environ.get("ARTIFACT_PATH", "").splitlines() if line.strip()]
    if not patterns:
        raise Failure("No files to save: the path input is empty.")
    files = matching_files(patterns)
    if_missing = os.environ.get("IF_NO_FILES_FOUND") or "error"
    if if_missing not in ("error", "warn", "ignore"):
        raise Failure(f"if-no-files-found must be error, warn or ignore, not '{if_missing}'.")
    if not files:
        message = f"No files match {', '.join(patterns)}"
        if if_missing == "error":
            raise Failure(f"{message}.")
        print(f"{'::warning::' if if_missing == 'warn' else ''}{message}; nothing saved.")
        return

    # Copy into a scratch folder first, then swap it in, so a load never sees half a save.
    run_dir = os.path.dirname(target)
    os.makedirs(run_dir, exist_ok=True)
    scratch = os.path.join(run_dir, f".saving-{uuid.uuid4().hex}")
    manifest = {}
    try:
        for relative in files:
            source = os.path.join(workspace(), relative)
            copy = os.path.join(scratch, "files", relative)
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            shutil.copy2(source, copy)
            manifest[relative] = {"sha256": sha256(copy), "size": os.path.getsize(copy)}
        with open(os.path.join(scratch, MANIFEST), "w") as f:
            json.dump({"job": os.environ.get("GITHUB_JOB"), "sha": os.environ.get("GITHUB_SHA"),
                       "files": manifest}, f, indent=2)
        if os.path.exists(target):
            old = os.path.join(run_dir, f".replaced-{uuid.uuid4().hex}")
            os.rename(target, old)
            shutil.rmtree(old, ignore_errors=True)
        os.rename(scratch, target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    os.utime(run_dir)  # The key counts as fresh while saves keep coming.

    total = sum(entry["size"] for entry in manifest.values())
    count = f"{len(files)} file{'s' if len(files) != 1 else ''}"
    print(f"Saved {count} ({megabytes(total)}) as {name} in {target}")
    summary(f"Saved **{name}** on the runner: {count}, {megabytes(total)}, in `{target}`.")
    sweep()


def load(name: str) -> None:
    source = saved_dir(name)
    try:
        with open(os.path.join(source, MANIFEST)) as f:
            files = json.load(f)["files"]
    except FileNotFoundError:
        raise Failure(f"Nothing was saved as {name} under {key()}, or it has been cleaned up since.") from None

    for relative, expected in files.items():
        destination = os.path.join(workspace(), relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(os.path.join(source, "files", relative), destination)
        if sha256(destination) != expected["sha256"]:
            raise Failure(f"{relative} changed on the runner after it was saved.")
        print(f"Loaded {relative}")


def main() -> None:
    commands = {"save": save, "load": load}
    if len(sys.argv) != 3 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    try:
        commands[sys.argv[1]](sys.argv[2])
    except Failure as failure:
        print(f"::error::{failure}")
        summary("### Failed", str(failure))
        sys.exit(1)


if __name__ == "__main__":
    main()
