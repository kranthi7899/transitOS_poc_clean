"""Secrets must never be committed: fails if any tracked file contains something shaped like an Anthropic API key."""
import re
import subprocess
from pathlib import Path

KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")


def test_no_api_key_is_tracked_in_git():
    files = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True, text=True).stdout.split("\0")
    leaks = []
    for f in filter(None, files):
        try:
            text = Path(f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if KEY.search(text):
            leaks.append(f)
    assert leaks == [], f"key-shaped strings in tracked files: {leaks}"


def test_env_files_are_git_ignored():
    for name in (".env", ".env.local"):
        r = subprocess.run(["git", "check-ignore", "-q", name], check=False)
        assert r.returncode == 0, f"{name} must be ignored"
