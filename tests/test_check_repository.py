"""The repository hygiene check refuses private keys, plain or base64-encoded."""

from __future__ import annotations

import base64
import json
import runpy
import subprocess
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(Path(__file__).parents[1] / "scripts" / "check_repository.py")
main = SCRIPT["main"]
has_private_key = SCRIPT["has_private_key"]

# Built at run time so no key-shaped literal sits in the repository.
PEM = (
    b"-----BEGIN "
    + b"PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n"
)


def test_plain_pem_is_detected() -> None:
    assert has_private_key(PEM)


@pytest.mark.parametrize("prefix", [b"", b"a", b"ab"], ids=["offset0", "offset1", "offset2"])
@pytest.mark.parametrize("label", [b"PRIVATE KEY", b"RSA PRIVATE KEY", b"ENCRYPTED PRIVATE KEY"])
def test_base64_pem_is_detected_at_every_alignment(prefix: bytes, label: bytes) -> None:
    pem = PEM.replace(b"PRIVATE KEY", label)
    assert has_private_key(base64.b64encode(prefix + pem))
    assert has_private_key(b"SF_PRIVATE_KEY_PEM=" + base64.b64encode(prefix + pem) + b"\n")


def test_line_wrapped_base64_pem_is_detected() -> None:
    wrapped = base64.encodebytes(b"x" * 50 + PEM)  # wraps at 76 columns
    assert wrapped.count(b"\n") > 1
    assert has_private_key(wrapped)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"ordinary text with no key",
        base64.b64encode(b"just some other payload " * 20),
        base64.b64encode(b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"),
    ],
    ids=["empty", "text", "other-base64", "certificate"],
)
def test_other_content_is_not_flagged(content: bytes) -> None:
    assert not has_private_key(content)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "VERSION").write_text("1.2.3\n")
    (root / "server.json").write_text(
        json.dumps({"name": "io.example/x", "version": "1.2.3"}) + "\n"
    )
    (root / "Dockerfile").write_text('LABEL io.modelcontextprotocol.server.name="io.example/x"\n')
    monkeypatch.setitem(main.__globals__, "ROOT", root)

    def check(**files: bytes) -> int:
        for name, content in files.items():
            path = root / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        return main()

    return check


def test_clean_repository_passes(repo) -> None:
    assert repo(**{"notes.txt": b"hello\n"}) == 0


def test_tracked_base64_pem_fails(repo, capsys) -> None:
    assert repo(**{"config.txt": base64.b64encode(PEM) + b"\n"}) == 1
    assert "Potential private key content: config.txt" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["credentials__sf.env", "data__results.json", "prod.env"])
def test_tracked_credentials_and_data_fail(repo, capsys, name: str) -> None:
    assert repo(**{name: b"x\n"}) == 1
    assert "Private or generated artifact is tracked" in capsys.readouterr().err
