import base64
import unittest

import httpx

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.odata_client import ODataClient


class _RecordingHTTPClient:
    """Captures the params/headers the OData client sends, returns an empty 200."""

    def __init__(self) -> None:
        self.params: dict = {}
        self.headers: dict = {}
        self.url = ""

    async def request(self, *, method, url, headers, params, json, timeout):
        self.url = url
        self.headers = headers
        self.params = params
        return httpx.Response(200, text="", request=httpx.Request(method, url))


def _client() -> tuple[ODataClient, _RecordingHTTPClient]:
    settings = Settings(
        sf_host="api.example.invalid",
        sf_company_id="example-a",
        # Resolution step 4 (base64 PEM) so no key file has to exist here.
        sf_private_key_pem=base64.b64encode(b"not-a-real-key").decode(),
        tenant_keys_dir="/nonexistent",
    )
    http = _RecordingHTTPClient()
    client = ODataClient(settings, http)
    # Skip the SAML Bearer round trip; this test is about the request shape.
    client._tokens[client._token_key(client._resolve(None))] = ("tok", float("inf"))
    return client, http


class MetadataRequestShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_is_requested_as_xml(self) -> None:
        client, http = _client()

        await client.request("GET", "$metadata")

        # SF answers 406 to $metadata?$format=JSON.
        self.assertNotIn("$format", http.params)
        self.assertEqual(http.headers["Accept"], "application/xml")

    async def test_metadata_keeps_the_entity_set_filter(self) -> None:
        client, http = _client()

        await client.request("GET", "$metadata", params={"entitySet": "BenefitEnrollment"})

        self.assertEqual(http.params, {"entitySet": "BenefitEnrollment"})

    async def test_entity_scoped_metadata_is_requested_as_xml(self) -> None:
        client, http = _client()

        await client.request("GET", "EmpJob/$metadata")

        # Entity-scoped metadata is EDMX too; $format=JSON earns the same 406.
        self.assertNotIn("$format", http.params)
        self.assertEqual(http.headers["Accept"], "application/xml")

    async def test_entity_reads_still_default_to_json(self) -> None:
        client, http = _client()

        await client.request("GET", "BenefitEnrollment", params={"$top": 1})

        self.assertEqual(http.params["$format"], "JSON")
        self.assertEqual(http.headers["Accept"], "application/json")


if __name__ == "__main__":
    unittest.main()
