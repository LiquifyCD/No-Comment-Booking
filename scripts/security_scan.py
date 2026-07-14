from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache"}
PATTERNS = {
    "Discord webhook": re.compile(r"https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
    "Trafikverket cookie": re.compile(
        r"(?:FpsExternalIdentity|LoginValid|ASP\.NET_SessionId)\s*[=:]\s*[A-Za-z0-9]"
    ),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"(?:github_pat_|ghp_)[A-Za-z0-9_]{20,}"),
}
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".html",
    ".css",
    ".md",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".example",
    ".ps1",
}


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != "Dockerfile":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: possible {label}")
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Security scan passed: no committed credentials or session cookies found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
