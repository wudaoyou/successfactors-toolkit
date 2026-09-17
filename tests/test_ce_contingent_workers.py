import unittest

from fastapi import HTTPException

from successfactors_toolkit.models.sfapi import (
    CEQueryByPersonIdRequest,
    CEQueryByUserIdRequest,
    CEQueryFilter,
)
from successfactors_toolkit.routers.sfapi import (
    ce_query,
    ce_query_by_person_id,
    ce_query_by_user_id,
)
from successfactors_toolkit.services.ce_query_builder import (
    COMMON_SEGMENTS,
    build_select_clause,
    build_where_clause,
)


class _RecordingClient:
    def __init__(self) -> None:
        self.query_string = ""

    async def query(self, query_string: str, *, conn=None, params=None) -> dict:
        self.query_string = query_string
        return {"status_code": 200, "headers": {}, "body": "<result/>"}


class DedicatedContingentWorkerLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_person_id_lookup_includes_contingent_workers_and_common_segments(self) -> None:
        payload = CEQueryByPersonIdRequest(
            person_id_external=["CW001"],
            include_contingent_workers=True,
        )
        client = _RecordingClient()

        await ce_query_by_person_id(payload, client)

        select = build_select_clause(list(COMMON_SEGMENTS))
        self.assertEqual(
            client.query_string,
            f"{select} where person_id_external in('CW001') "
            "and isContingentWorker in('true','false')",
        )

    async def test_user_id_lookup_includes_contingent_workers_and_common_segments(self) -> None:
        payload = CEQueryByUserIdRequest(
            user_id=["cw.user"],
            include_contingent_workers=True,
        )
        client = _RecordingClient()

        await ce_query_by_user_id(payload, client)

        select = build_select_clause(list(COMMON_SEGMENTS))
        self.assertEqual(
            client.query_string,
            f"{select} where user_id in('cw.user') and isContingentWorker in('true','false')",
        )

    async def test_person_id_lookup_default_query_is_unchanged(self) -> None:
        payload = CEQueryByPersonIdRequest(person_id_external=["EMP001"])
        client = _RecordingClient()

        await ce_query_by_person_id(payload, client)

        select = build_select_clause(list(COMMON_SEGMENTS))
        self.assertEqual(
            client.query_string,
            f"{select} where person_id_external in('EMP001')",
        )


class StructuredContingentWorkerFilterTests(unittest.TestCase):
    def test_default_structured_filter_is_unchanged(self) -> None:
        payload = CEQueryFilter(
            last_modified_on="2026-07-01T00:00:00+0000",
            company="EXAMPLE",
        )

        self.assertEqual(
            build_where_clause(payload),
            "last_modified_on>to_datetime('2026-07-01T00:00:00Z') and company in('EXAMPLE')",
        )

    def test_structured_filter_includes_contingent_workers(self) -> None:
        payload = CEQueryFilter(
            last_modified_on="2026-07-01T00:00:00+0000",
            company="EXAMPLE",
            include_contingent_workers=True,
        )

        self.assertEqual(
            build_where_clause(payload),
            "isContingentWorker in('true','false') and "
            "last_modified_on>to_datetime('2026-07-01T00:00:00Z') and company in('EXAMPLE')",
        )


class RejectedFilterIsARequestErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_modified_on_without_a_timezone_is_a_400(self) -> None:
        payload = CEQueryFilter(last_modified_on="2026-07-01T00:00:00")

        with self.assertRaises(HTTPException) as raised:
            await ce_query(payload, _RecordingClient())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("timezone", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
