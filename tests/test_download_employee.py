import argparse
import stat

import httpx
import pytest

from scripts import download_employee as cli

XML = """<response><numResults>1</numResults><hasMore>false</hasMore>
<CompoundEmployee><person><person_id_external>EXAMPLE001</person_id_external>
</person></CompoundEmployee></response>"""


@pytest.mark.parametrize("person_id", ["../escape", "", "/tmp/payload", "..", "a/b"])
def test_invalid_employee_id_is_rejected(person_id):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.validate_person_id(person_id)


@pytest.mark.parametrize(
    "body",
    [
        "not xml",
        "<Fault>denied</Fault>",
        XML.replace("EXAMPLE001", "OTHER"),
        XML.replace("<numResults>1", "<numResults>2"),
        XML.replace("<hasMore>false", "<hasMore>true"),
        '<!DOCTYPE response [<!ENTITY sample "example">]>' + XML,
    ],
)
def test_invalid_or_incomplete_payload_is_rejected(body):
    with pytest.raises(ValueError):
        cli.validate_payload(body, "EXAMPLE001")


def test_download_validates_and_saves_without_overwrite(monkeypatch, tmp_path, capsys):
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(
            200, json={"status_code": 200, "body": XML}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(cli.httpx, "post", post)
    path = cli.download_employee(
        "EXAMPLE001",
        base_url="http://localhost:8000",
        api_key="synthetic-api-key",
        output_dir=tmp_path,
        company_id="example",
    )
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert requests[0][1]["headers"] == {"X-API-Key": "synthetic-api-key"}
    assert requests[0][1]["json"]["include_contingent_workers"] is True
    assert requests[0][1]["json"]["connection"] == {"company_id": "example"}
    with pytest.raises(FileExistsError):
        cli.download_employee(
            "EXAMPLE001",
            base_url="http://localhost:8000",
            api_key="synthetic-api-key",
            output_dir=tmp_path,
        )
    assert capsys.readouterr().out == ""
