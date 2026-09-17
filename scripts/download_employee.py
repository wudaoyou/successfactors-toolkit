#!/usr/bin/env python3
"""Download one employee through the toolkit REST API without logging HR data."""

import argparse
import os
import re
import sys
from pathlib import Path

import httpx
from lxml import etree


def validate_person_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise argparse.ArgumentTypeError("Employee ID must be 1-128 filename-safe characters.")
    return value


def validate_payload(body: str, person_id: str) -> bytes:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(body.encode("utf-8"), parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ValueError("Response is not valid XML.") from exc
    if root.getroottree().docinfo.doctype:
        raise ValueError("Response contains a forbidden document type.")
    if root.xpath("//*[local-name()='Fault']"):
        raise ValueError("Upstream returned a SOAP Fault.")
    counts = root.xpath("//*[local-name()='numResults']/text()")
    employees = root.xpath("//*[local-name()='CompoundEmployee']")
    identities = root.xpath("//*[local-name()='person_id_external']/text()")
    more = root.xpath("//*[local-name()='hasMore']/text()")
    if [value.strip() for value in counts] != ["1"] or len(employees) != 1:
        raise ValueError("Response does not contain exactly one employee result.")
    if not identities or any(value.strip() != person_id for value in identities):
        raise ValueError("Response employee identity does not match the request.")
    if any(value.strip().lower() not in ("false", "0") for value in more):
        raise ValueError("Response is incomplete; more pages are available.")
    return etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)


def download_employee(
    person_id: str,
    *,
    base_url: str,
    api_key: str,
    output_dir: Path,
    company_id: str | None = None,
) -> Path:
    validate_person_id(person_id)
    if not api_key:
        raise ValueError("Set API_KEY to the toolkit API access key.")
    payload: dict = {
        "person_id_external": [person_id],
        "include_contingent_workers": True,
    }
    if company_id:
        payload["connection"] = {"company_id": company_id}
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/sfapi/ce/query-by-person-id",
        headers={"X-API-Key": api_key},
        json=payload,
        timeout=60.0,
        follow_redirects=False,
    )
    response.raise_for_status()
    result = response.json()
    if (
        not isinstance(result, dict)
        or type(result.get("status_code")) is not int
        or not 200 <= result["status_code"] < 300
        or not isinstance(result.get("body"), str)
    ):
        raise ValueError("Upstream returned an unsuccessful or invalid response.")
    xml = validate_payload(result["body"], person_id)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output_dir / f"{person_id}.xml"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(xml)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("person_id", type=validate_person_id)
    parser.add_argument("--company-id", help="Override the default SuccessFactors company ID.")
    parser.add_argument(
        "--base-url", default=os.getenv("SAP_SF_PAYLOAD_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(os.getenv("RESULTS_DIR", "results"))
    )
    args = parser.parse_args(argv)
    try:
        path = download_employee(
            args.person_id,
            base_url=args.base_url,
            api_key=os.getenv("API_KEY", ""),
            output_dir=args.output_dir,
            company_id=args.company_id,
        )
    except httpx.HTTPStatusError as exc:
        print(f"Download failed: HTTP {exc.response.status_code}.", file=sys.stderr)
        return 1
    except httpx.RequestError:
        print("Download failed: connection or timeout error.", file=sys.stderr)
        return 1
    except FileExistsError:
        print("Download failed: output exists; choose another output directory.", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("Download failed: invalid response, configuration, or output path.", file=sys.stderr)
        return 1
    print(f"Verified employee payload saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
