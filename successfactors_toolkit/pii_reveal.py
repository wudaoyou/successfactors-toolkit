"""successfactors-pii-reveal: put PII plaintext back into a file the model wrote.

Runs locally against PII_VAULT_DIR. Deliberately not an MCP tool, so the model
can't reveal what it was only ever given as tokens.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError

from successfactors_toolkit.config import get_settings
from successfactors_toolkit.services.pii_filter import PiiVaultError, Vault, reveal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="successfactors-pii-reveal",
        description="Replace [PII-...] tokens in a text file with their vault plaintext.",
    )
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "-o", "--output", help="Output file path, or - for stdout. Default: stdout."
    )
    args = parser.parse_args(argv)

    try:
        vault_dir = get_settings().pii_vault_dir
    except (ValidationError, SettingsError):
        print("error: invalid settings, see PII_* env vars", file=sys.stderr)
        return 2
    if not (vault_dir / "key").is_file():
        print(f"error: no PII vault at {vault_dir} (check PII_VAULT_DIR)", file=sys.stderr)
        return 2
    try:
        text, replaced, unknown = reveal(args.file.read_text(encoding="utf-8"), Vault(vault_dir))
    except (OSError, PiiVaultError, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.output or args.output == "-":
        sys.stdout.write(text)
        target = "stdout"
    else:
        output = Path(args.output)
        try:
            # O_NOFOLLOW refuses a symlink at `output` (e.g. planted at a predictable
            # path to redirect our write at the vault key); O_NONBLOCK keeps a FIFO
            # there from hanging the open instead of blocking for a reader.
            descriptor = os.open(
                output, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                print(f"error: {output} is not a regular file", file=sys.stderr)
                return 2
            os.ftruncate(descriptor, 0)
            os.fchmod(descriptor, 0o600)  # an existing file keeps its old mode otherwise
            with os.fdopen(descriptor, "w", encoding="utf-8") as out:
                out.write(text)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        target = str(output)
    print(f"{replaced} token(s) revealed -> {target}", file=sys.stderr)
    if unknown:
        print(
            f"{len(unknown)} unknown token(s) left as is: {', '.join(sorted(set(unknown)))}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
