"""Validate bootstrap hygiene and version syntax; no dependencies or live access."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
PRIVATE_DIRS = {
    "secrets",
    "certs",
    "tenants",
    "results",
    "logs",
    ".codex",
    ".agents",
    ".claude",
    ".superpowers",
    ".cursor",
}
PRIVATE_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules"}
PRIVATE_SUFFIXES = {".pem", ".key", ".crt", ".p12", ".pfx", ".zip", ".log"}


def main():
    errors = []
    if not SEMVER.fullmatch((ROOT / "VERSION").read_text().strip()):
        errors.append("VERSION must contain a valid SemVer identifier.")
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in filter(None, names):
        path = Path(name)
        if (
            set(path.parts) & PRIVATE_DIRS
            or path.name in PRIVATE_NAMES
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or (path.name.startswith(".env") and path.name != ".env.example")
            or name == ".github/copilot-instructions.md"
            or name.startswith((".github/instructions/", ".github/prompts/"))
        ):
            errors.append(f"Private or generated artifact is tracked: {name}")
        content = (ROOT / path).read_bytes()
        if re.search(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----", content):
            errors.append(f"Potential private key content: {name}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\r" in text or (text and not text.endswith("\n")):
            errors.append(f"Use LF and a final newline: {name}")
        if any(line != line.rstrip() for line in text.splitlines()):
            errors.append(f"Trailing whitespace: {name}")
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("Repository hygiene and SemVer checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
