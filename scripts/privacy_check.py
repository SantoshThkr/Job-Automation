#!/usr/bin/env python3
"""Keep personal data and secrets out of Git.

    python3 scripts/privacy_check.py --staged          what the next commit would contain   (pre-commit hook)
    python3 scripts/privacy_check.py --range A..B      the commits about to be pushed       (pre-push hook)
    python3 scripts/privacy_check.py --all             everything Git can see + full history (make privacy-check)

Three checks, all local (nothing is uploaded):

1. paths    - files that must never be in Git (your profile, .env, databases, browser sessions, resumes, keys...)
2. personal - your own identifying values, read from your local profile (config/profile.json): e-mail, phone,
              name, profile URLs, address. A match means real data leaked into source, a test or a fixture.
3. secrets  - credential patterns (private keys, API tokens, passwords in code); gitleaks is used too when installed

Findings name the file and the KIND of value - never the value itself. Exit status 1 = something must not be
committed. Standard library only, so it runs before the project is set up.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# files that hold personal data or secrets by nature (templates and fixtures are listed in ALLOWED)
FORBIDDEN = [
    "config/profile.json", "config/profile.*.json", "config/*.local.json", "config/*.private.json",
    ".env", ".env.*", "*.env", "*/.env", "*/.env.*",
    "data/*", "*/data/*.db", "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    "*browser-profile/*", "*storage-state*.json", "*storageState*.json", "cookies*.json", "*/cookies*.json", "*.har",
    "resumes/*", "*.log", "*/logs/*", "*/screenshots/*", "trace.zip", "*/trace.zip",
    "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "*/id_rsa*", "id_ed25519*", "*/id_ed25519*", ".netrc", ".npmrc",
    ".pypirc", "secrets/*", "*/secrets/*", "credentials.*", "*/credentials.*", "service-account*.json",
]
ALLOWED = ["config/profile.example.json", ".env.example", "*.env.example", "resumes/README.md",
           "backend/tests/fixtures/*", "frontend/tests/*"]

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("API key (sk-...)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("LinkedIn session cookie", re.compile(r"\bli_at\s*[=:]\s*['\"]?AQED[A-Za-z0-9_-]{40,}")),
    ("password / secret assigned in code",
     re.compile(r"""(?ix)(?<![A-Za-z0-9])(?:\w*[_-])?(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)
                    \w*['"]?\s*[:=]\s*
                    ['"](?!\s*['"])(?!(?:your|example|changeme|change-me|placeholder|dummy|test|xxx|\*\*\*|<|\$\{|\{\{))
                    [^'"\s]{12,}['"]""")),
]
# values that are too common to identify anybody (a first name alone, a city, a country...)
COMMON = {"india", "remote", "immediate", "frontend", "full-time", "bengaluru", "bangalore", "noida", "gurugram",
          "delhi", "mumbai", "pune", "hyderabad", "chennai", "kolkata"}
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".woff", ".woff2", ".ttf", ".mp4", ".webp"}


def git(*args: str, check: bool = False) -> str:
    res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, errors="replace")
    if check and res.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p) and "/" not in p
               for p in patterns)


def forbidden(path: str) -> bool:
    return matches(path, FORBIDDEN) and not matches(path, ALLOWED)


def profile_path() -> Path:
    env = ROOT / ".env"
    configured = os.environ.get("PJA_PROFILE_PATH", "")
    if not configured and env.is_file():
        for line in env.read_text(errors="replace").splitlines():
            if line.strip().startswith("PJA_PROFILE_PATH="):
                configured = line.split("=", 1)[1].split("#")[0].strip().strip("'\"")
    p = Path(configured or "config/profile.json")
    return p if p.is_absolute() else ROOT / p


def personal_needles() -> dict[str, str]:
    """{lower-cased value: kind}: the things that identify you. Empty when there is no local profile."""
    path = profile_path()
    if not path.is_file():
        return {}
    try:
        profile = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    person = profile.get("personal") or {}
    out: dict[str, str] = {}

    def add(value: object, kind: str, min_len: int = 6) -> None:
        text = str(value or "").strip().lower()
        if len(text) >= min_len and text not in COMMON and not text.startswith(("your_", "you@example", "+91xxx")):
            out[text] = kind

    first, last = str(person.get("first_name") or "").strip(), str(person.get("last_name") or "").strip()
    if first and last:
        add(f"{first} {last}", "your full name")
    add(last, "your last name", 5)
    email = str(person.get("email") or "")
    add(email, "your e-mail address")
    if "@" in email:
        add(email.split("@")[0], "your e-mail name", 6)
    digits = re.sub(r"\D", "", str(person.get("phone") or ""))
    if len(digits) >= 10:
        add(digits[-10:], "your phone number", 10)
    for key, kind in (("linkedin_url", "your LinkedIn URL"), ("github_url", "your GitHub URL"),
                      ("portfolio_url", "your portfolio URL"), ("address", "your address"),
                      ("postal_code", "your postal code"), ("date_of_birth", "your date of birth")):
        value = str(person.get(key) or "")
        add(re.sub(r"^https?://(www\.)?", "", value).rstrip("/") if key.endswith("_url") else value, kind)
    return out


def scan_text(text: str, needles: dict[str, str]) -> list[str]:
    kinds: list[str] = []
    low = text.lower()
    for value, kind in needles.items():
        if value in low and kind not in kinds:
            kinds.append(kind)
    for kind, rx in SECRET_PATTERNS:
        if rx.search(text) and kind not in kinds:
            kinds.append(kind)
    return kinds


class Report:
    def __init__(self) -> None:
        self.findings: list[tuple[str, str]] = []

    def add(self, where: str, what: str) -> None:
        if (where, what) not in self.findings:
            self.findings.append((where, what))

    def check_blob(self, label: str, path: str, content: str | None, needles: dict[str, str]) -> None:
        if forbidden(path):
            self.add(label, "a private file that must never be in Git")
        if content is None or Path(path).suffix.lower() in BINARY or os.path.basename(path) == "package-lock.json":
            return
        if os.path.normpath(path) == os.path.normpath("scripts/privacy_check.py"):
            return  # this file names the patterns it looks for
        for kind in scan_text(content, needles):
            self.add(label, kind)


def read_worktree(path: str) -> str | None:
    p = ROOT / path
    try:
        return p.read_text(errors="replace") if p.is_file() and p.stat().st_size <= 5_000_000 else None
    except OSError:
        return None


def check_staged(report: Report, needles: dict[str, str]) -> int:
    paths = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p]
    for path in paths:
        report.check_blob(path, path, git("show", f":{path}"), needles)
    return len(paths)


def check_range(report: Report, needles: dict[str, str], rev_range: str) -> int:
    paths: set[tuple[str, str]] = set()
    for commit in git("rev-list", rev_range).split():
        for line in git("diff-tree", "--no-commit-id", "-r", "--root", "--diff-filter=ACMR", commit).splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 4 and path:
                paths.add((parts[3], path))
    for sha, path in sorted(paths, key=lambda x: x[1]):
        report.check_blob(f"{path} (in the commits being pushed)", path, git("cat-file", "-p", sha), needles)
    return len(paths)


def check_all(report: Report, needles: dict[str, str]) -> tuple[int, int, int]:
    tracked = [p for p in git("ls-files").splitlines() if p]
    untracked = [p for p in git("ls-files", "--others", "--exclude-standard").splitlines() if p]
    for path in tracked:
        report.check_blob(f"{path} (tracked)", path, read_worktree(path), needles)
    for path in untracked:
        report.check_blob(f"{path} (untracked, not ignored - `git add .` would commit it)", path,
                          read_worktree(path), needles)
    blobs = 0
    seen: set[str] = set()
    for line in git("rev-list", "--all", "--objects").splitlines():
        sha, _, path = line.partition(" ")
        if not path or sha in seen:
            continue
        seen.add(sha)
        if git("cat-file", "-t", sha).strip() != "blob":
            continue
        blobs += 1
        report.check_blob(f"{path} (Git history, object {sha[:10]})", path, git("cat-file", "-p", sha), needles)
    return len(tracked), len(untracked), blobs


_LEAKS = 42  # gitleaks exit status reserved for "leaks found" (its other failures exit 1)


def gitleaks(mode: str, rev_range: str = "") -> tuple[bool | None, str]:
    """(clean?, summary). None = gitleaks is not installed."""
    exe = None if os.environ.get("PRIVACY_CHECK_GITLEAKS") == "0" else shutil.which("gitleaks")
    if not exe:
        return None, "gitleaks not used (brew install gitleaks) - the built-in patterns were applied"
    common = ["--no-banner", "--redact", "--exit-code", str(_LEAKS), str(ROOT)]
    if mode == "staged":
        cmd = [exe, "git", "--staged", "--pre-commit", *common]
    elif mode == "range":
        cmd = [exe, "git", f"--log-opts={rev_range}", *common]
    else:
        cmd = [exe, "git", "--log-opts=--all", *common]
    res = subprocess.run(cmd, capture_output=True, text=True)
    tail = [ln for ln in (res.stderr or res.stdout).splitlines() if "leaks found" in ln or "no leaks" in ln]
    note = re.sub(r"\x1b\[[0-9;]*m", "", tail[-1]).strip() if tail else f"gitleaks exit {res.returncode}"
    if res.returncode == _LEAKS:
        return False, note
    if res.returncode != 0:  # could not run here (e.g. a repository without commits): not a finding
        return None, f"gitleaks could not scan ({note}) - the built-in patterns were applied"
    return True, note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="check the staged changes (default)")
    group.add_argument("--range", metavar="A..B", help="check the commits in a revision range")
    group.add_argument("--all", action="store_true", help="check every tracked / visible file and the full history")
    args = ap.parse_args()

    needles = personal_needles()
    report = Report()
    if args.all:
        tracked, untracked, blobs = check_all(report, needles)
        scope, mode = (f"{tracked} tracked + {untracked} untracked-but-visible files, {blobs} objects in the full "
                       f"history ({len(git('rev-list', '--all').split())} commit(s))"), "all"
    elif args.range:
        scope, mode = f"{check_range(report, needles, args.range)} file version(s) in {args.range}", "range"
    else:
        scope, mode = f"{check_staged(report, needles)} staged file(s)", "staged"

    leaks_clean, leaks_note = gitleaks(mode, args.range or "")
    if leaks_clean is False:
        report.add("gitleaks", f"credential pattern(s) reported: {leaks_note} (run: gitleaks git --redact -v)")

    print(f"privacy check: {scope}")
    source = profile_path().relative_to(ROOT) if needles and profile_path().is_relative_to(ROOT) else (
        "your local profile" if needles else "no local profile")
    print(f"  personal values compared: {len(needles)} (from {source})")
    print(f"  gitleaks: {leaks_note}")
    if not report.findings:
        print("  OK - no private file, personal value or credential found")
        return 0
    print(f"\n  BLOCKED - {len(report.findings)} finding(s). Nothing below may be committed or pushed:")
    for where, what in report.findings:
        print(f"    {where}: {what}")
    print("\n  Fix: replace real values with placeholders / synthetic data, or `git rm --cached <file>` for a private "
          "file\n  (that keeps your local copy). Values are never printed here on purpose.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
