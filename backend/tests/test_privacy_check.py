"""scripts/privacy_check.py - the pre-commit / pre-push guard that keeps personal data and secrets out of Git.
Every case runs in a throw-away repository with a synthetic identity; the real profile is never involved."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_check.py"
PERSON = {"first_name": "Riya", "last_name": "Kapoorwala", "email": "riya.kapoorwala.dev@example.org",
          "phone": "+91 98111 22334", "linkedin_url": "https://www.linkedin.com/in/riya-kapoorwala-dev/",
          "github_url": "", "address": "", "postal_code": "", "current_city": "Pune"}


def run(repo: Path, *args: str, gitleaks: bool = False) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PRIVACY_CHECK_GITLEAKS": "1" if gitleaks else "0"}
    env.pop("PJA_PROFILE_PATH", None)  # the test session points this at its own profile
    return subprocess.run(["python3", str(repo / "scripts" / "privacy_check.py"), *args], cwd=repo, env=env,
                          capture_output=True, text=True)


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test",
                          "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "privacy_check.py")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.json").write_text(json.dumps({"personal": PERSON}))
    (tmp_path / "config" / "profile.example.json").write_text(json.dumps({"personal": {"first_name": "YOUR_FIRST_NAME",
                                                                                      "email": "you@example.com"}}))
    (tmp_path / ".gitignore").write_text("config/profile.json\n.env\ndata/\n")
    (tmp_path / "app.py").write_text("print('hello')\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_clean_changes_pass(repo):
    (repo / "notes.md").write_text("Frontend Engineer at Acme, Pune. Contact: you@example.com\n")
    git(repo, "add", "notes.md")
    res = run(repo, "--staged")
    assert res.returncode == 0 and "OK - no private file" in res.stdout, res.stdout
    assert run(repo, "--all").returncode == 0


@pytest.mark.parametrize("content,kind", [
    ("mail riya.kapoorwala.dev@example.org for details", "your e-mail address"),
    ("Hi Riya Kapoorwala, thank you for applying", "your full name"),
    ("call 9811122334 after 6pm", "your phone number"),
    ("see linkedin.com/in/riya-kapoorwala-dev", "your LinkedIn URL"),
])
def test_personal_values_are_blocked_and_never_printed(repo, content, kind):
    (repo / "fixture.html").write_text(f"<p>{content}</p>\n")
    git(repo, "add", "fixture.html")
    res = run(repo, "--staged")
    assert res.returncode == 1 and "BLOCKED" in res.stdout and f"fixture.html: {kind}" in res.stdout, res.stdout
    for value in ("riya.kapoorwala", "Kapoorwala", "9811122334"):  # the finding names the kind, never the value
        assert value.lower() not in res.stdout.lower()


@pytest.mark.parametrize("path", ["config/profile.json", ".env", "data/jobs.db", "resumes/my-cv.pdf",
                                  "data/browser-profile/Default/Cookies", "backend/storage-state.json", "id_rsa"])
def test_private_files_cannot_be_committed_even_when_forced(repo, path):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(b"x")
    git(repo, "add", "-f", path)
    res = run(repo, "--staged")
    assert res.returncode == 1 and f"{path}: a private file that must never be in Git" in res.stdout, res.stdout


def test_templates_and_fixtures_are_allowed(repo):
    (repo / ".env.example").write_text("PJA_API_PORT=8000\nPJA_ATS_ACCOUNT_PASSWORD=\n")
    (repo / "resumes").mkdir()
    (repo / "resumes" / "README.md").write_text("Put your resume PDFs here.\n")
    git(repo, "add", ".env.example", "resumes/README.md", "config/profile.example.json")
    assert run(repo, "--staged").returncode == 0


@pytest.mark.parametrize("secret,kind", [
    ("-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabc\n", "private key"),
    ("token = '" + "_".join(("ghp", "aB3dE6gH9k" * 4)) + "'", "GitHub token"),
    ("AWS_KEY=AKIA" + "ABCDEFGHIJKLMNOP", "AWS access key"),
    ("OPENAI = 'sk-" + "proj-" + "Zx9" * 12 + "'", "API key (sk-...)"),
    ('db_password = "' + "S3cure" + "Pass" + 'word!2026"', "password / secret assigned in code"),
])
def test_credentials_are_blocked(repo, secret, kind):
    (repo / "settings.py").write_text(secret + "\n")
    git(repo, "add", "settings.py")
    res = run(repo, "--staged")
    assert res.returncode == 1 and f"settings.py: {kind}" in res.stdout, res.stdout


def test_placeholders_are_not_credentials(repo):
    (repo / "example.py").write_text('password = ""\napi_key = "your-api-key-here"\nPASSWORD = os.environ["X"]\n'
                                     'secret = "${SECRET_FROM_ENV}"\n')
    git(repo, "add", "example.py")
    assert run(repo, "--staged").returncode == 0


def test_pushed_commits_and_history_are_checked(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "leak.txt").write_text("contact riya.kapoorwala.dev@example.org\n")
    git(repo, "add", "leak.txt")
    git(repo, "commit", "-q", "-m", "oops")
    git(repo, "rm", "-q", "leak.txt")
    git(repo, "commit", "-q", "-m", "remove it again")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert run(repo, "--staged").returncode == 0  # nothing staged, the file is gone from the work tree...
    pushed = run(repo, "--range", f"{base}..{head}")  # ...but the commits about to be pushed still contain it
    assert pushed.returncode == 1 and "leak.txt (in the commits being pushed): your e-mail address" in pushed.stdout
    history = run(repo, "--all")  # and so does the history
    assert history.returncode == 1 and "leak.txt (Git history" in history.stdout


def test_untracked_visible_files_are_reported(repo):
    (repo / "scratch.txt").write_text("Riya Kapoorwala - 9811122334\n")  # not ignored: `git add .` would take it
    res = run(repo, "--all")
    assert res.returncode == 1 and "scratch.txt (untracked, not ignored" in res.stdout


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not installed")
def test_gitleaks_is_used_when_installed(repo):
    assert "no leaks found" in run(repo, "--all", gitleaks=True).stdout
    (repo / "key.txt").write_text("-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEpAIBAAKCAQEA1c7" * 20 + "\n-----END " + "RSA PRIVATE KEY-----\n")
    git(repo, "add", "key.txt")
    res = run(repo, "--staged", gitleaks=True)
    assert res.returncode == 1 and "key.txt: private key" in res.stdout
