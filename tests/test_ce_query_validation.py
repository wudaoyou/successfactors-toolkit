"""Compound Employee query inputs reach SFQL only as IDs and known segment names."""

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.common import ODataConnectionConfig
from successfactors_toolkit.models.odata import ODataExtractByFilterInRequest
from successfactors_toolkit.models.sfapi import CEQueryByPersonIdRequest, CEQueryFilter
from successfactors_toolkit.routers.sfapi import ce_query, ce_query_by_person_id
from successfactors_toolkit.services.ce_query_builder import build_query_string
from successfactors_toolkit.services.odata_client import ODataClient


def test_segment_list_cannot_carry_extra_sfql():
    f = CEQueryFilter(select_segments=["person FROM CompoundEmployee where 1=1 --"], user_id="u1")
    with pytest.raises(ValueError, match="Unknown segment"):
        build_query_string(f)


def test_unknown_segment_is_rejected():
    with pytest.raises(ValueError, match="Unknown segment"):
        build_query_string(CEQueryFilter(select_segments=["person", "payroll"], user_id="u1"))


def test_id_value_cannot_close_the_quoted_list():
    f = CEQueryFilter(person_id_external="X') or person_id_external in('")
    with pytest.raises(ValueError, match="Invalid person_id_external value"):
        build_query_string(f)


@pytest.mark.parametrize(
    "field", ["user_id", "company", "business_unit", "division", "location", "employee_class"]
)
def test_every_list_filter_is_validated(field):
    with pytest.raises(ValueError, match="Invalid"):
        build_query_string(CEQueryFilter(**{field: "A,B') or 1=1 --"}))


@pytest.mark.parametrize("value", ["A,,B", "A,", ",", "a\nb", "a'b", "a;b"])
def test_empty_items_and_unusual_characters_are_rejected(value):
    with pytest.raises(ValueError):
        build_query_string(CEQueryFilter(user_id=value))


def test_normal_ids_and_segments_still_work():
    f = CEQueryFilter(
        user_id="jsmith, s.grant@example-corp.com ,EMP_001",
        select_segments=["person", "job_information"],
    )
    assert build_query_string(f) == (
        "SELECT person, job_information FROM CompoundEmployee "
        "where user_id in('jsmith','s.grant@example-corp.com','EMP_001')"
    )


def test_rest_returns_400_for_a_rejected_value():
    payload = CEQueryByPersonIdRequest(person_id_external=["X') or person_id_external in('"])
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ce_query_by_person_id(payload, client=None))
    assert raised.value.status_code == 400

    with pytest.raises(HTTPException) as raised:
        asyncio.run(ce_query(CEQueryFilter(select_segments=["payroll"]), client=None))
    assert raised.value.status_code == 400


@pytest.mark.parametrize("column", ["x' or 1 eq 1 or id", "a b", "a)", "a\n", ""])
def test_filter_in_column_must_be_a_property_path(column):
    with pytest.raises(ValidationError):
        ODataExtractByFilterInRequest(path="FOCompany", column=column, values=["a"])


def test_filter_in_column_accepts_paths():
    for column in ("externalCode", "jobInfoNav/company", "cust_field_1"):
        ODataExtractByFilterInRequest(path="FOCompany", column=column, values=["a"])


def test_client_rejects_a_bad_column_before_any_request():
    class _NoHTTP:
        def stream(self, *args, **kwargs):
            pytest.fail("no request expected")

    client = ODataClient(Settings(_env_file=None), _NoHTTP())
    with pytest.raises(ValueError, match="column"):
        asyncio.run(
            client.extract_by_filter_in(
                "FOCompany", "x' or 1 eq 1", ["a"], conn=ODataConnectionConfig()
            )
        )
