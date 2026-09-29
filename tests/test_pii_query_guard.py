"""pii_query_guard: on tokenized tenants, PII fields may only be compared by
token equality. Pure string checks, no network."""

import pytest

from successfactors_toolkit.services.pii_filter import PiiVaultError
from successfactors_toolkit.services.pii_query_guard import PiiQueryRefused, check_query

TOK = "[PII-T3-0123456789abcdef]"
TOK2 = "[PII-T3-fedcba9876543210]"
ENCODED = "%5BPII-T3-0123456789abcdef%5D"

_PROTECTED = {"lastName", "nationalId", "dateOfBirth", "nationality"}


def protects(entity, field):
    """Stub: cust_Unknown protects every bare field; elsewhere a fixed set."""
    if entity == "cust_Unknown":
        return True
    return field in _PROTECTED


def check(name, value, entity="PerPerson"):
    check_query([(name, value)], entity, protects)


REFUSED = [
    ("$filter", "startswith(nationalId,'1')"),
    ("$filter", "nationalId ge '5'"),
    ("$filter", "dateOfBirth lt datetime'2000-01-01T00:00:00'"),
    ("$filter", "substring(lastName,0,1) eq 'A'"),
    ("$filter", f"tolower(lastName) eq '{TOK}'"),
    ("$filter", "length(lastName) gt 3"),
    ("$filter", "lastName eq 'Smith'"),
    ("$filter", f"lastName eq '{TOK}' and startswith(lastName,'A')"),
    ("$filter", f"lastName eq '{TOK}' or lastName ge 'M'"),
    ("$filter", f"'{TOK}' eq lastName"),
    ("$filter", f"lastName in '{TOK}','Smith'"),
    ("$filter", f"lastName in ('{TOK}','Smith')"),
    ("$filter", f"lastName in ('{TOK}'"),
    ("$filter", "lastName eq"),
    ("$filter", "lastName eq lastName"),
    ("$filter", "lastName'x'"),
    ("$filter", "lastName ge'M'"),
    ("$filter", "lastName eq'Smith'"),
    ("$filter", f"lastName eq'{TOK}' and lastName ge'M'"),
    ("$filter", f"lastName eq ('{TOK}')"),
    ("$filter", "employmentNav/personNav/lastName ge 'M'"),
    ("$filter", "personNav/nationalId gt '1'"),
    ("$filter", "employmentNav/any(d: d/lastName ge 'M')"),
    ("$filter", f"employmentNav/any(d: d/lastName eq '{TOK}' and startswith(d/lastName,'A'))"),
    ("$FILTER", "nationalId ge '5'"),
    ("filter", "nationalId ge '5'"),
    ("$Filter", "nationalId ge '5'"),
    ("$orderby", "dateOfBirth desc"),
    ("$orderby", "userId asc, lastName"),
    ("$orderby", "personNav/dateOfBirth"),
    ("orderby", "lastName"),
    ("$search", "anything"),
    ("search", "Smith"),
    ("$apply", "groupby((nationality))"),
    ("$apply", "filter(lastName eq 'x')/aggregate($count as n)"),
    ("$compute", "length(lastName) as n"),
    ("$expand", "personalInfoNav($filter=lastName ge 'M')"),
    ("$expand", "personalInfoNav($orderby=lastName)"),
    ("$expand", "a($expand=b($filter=nationalId ge '5'))"),
    ("$expand", "personalInfoNav($select=userId;$search=Smith)"),
    ("$expand", "personalInfoNav($filter=startswith(lastName,'A'))"),
]


@pytest.mark.parametrize(("name", "value"), REFUSED)
def test_probing_forms_are_refused(name, value):
    with pytest.raises(PiiQueryRefused):
        check(name, value)


def test_refusal_is_a_vault_error_and_names_field_and_what_is_allowed():
    with pytest.raises(PiiVaultError, match=r"nationalId.*tokenized PII.*eq, ne or in"):
        check("$filter", "nationalId ge '5'")
    with pytest.raises(PiiQueryRefused, match=r"dateOfBirth.*\$orderby"):
        check("$orderby", "dateOfBirth")
    with pytest.raises(PiiQueryRefused, match="Full-text search is not available"):
        check("$search", "x")


def test_one_bad_duplicate_option_is_enough():
    options = [("$filter", f"lastName eq '{TOK}'"), ("$filter", "lastName ge 'M'")]
    with pytest.raises(PiiQueryRefused):
        check_query(options, "PerPerson", protects)


ALLOWED = [
    ("$filter", f"lastName eq '{TOK}'"),
    ("$filter", f"lastName ne '{TOK}'"),
    ("$filter", f"lastName EQ '{TOK}'"),
    ("$filter", "lastName eq null"),
    ("$filter", f"lastName eq'{TOK}'"),
    (
        "$filter",
        "startDate ge DateTime'2020-01-01T00:00:00' and id eq GUID'0123abcd-0123-4123-8123-0123456789ab'",
    ),
    (
        "$filter",
        "photo eq X'0FA1' and span eq duration'P1D' and spot eq geography'SRID=0;Point(1 2)'",
    ),
    ("$filter", f"lastName in '{TOK}','{TOK2}'"),
    ("$filter", f"lastName in ('{TOK}','{TOK2}')"),
    ("$filter", f"lastName in ('{TOK}')"),
    ("$filter", f"lastName eq '{ENCODED}'"),
    ("$filter", f"lastName eq '{TOK}' and nationalId eq '{TOK2}' and userId eq '1'"),
    ("$filter", f"personNav/lastName eq '{TOK}'"),
    ("$filter", f"employmentNav/any(d: d/lastName eq '{TOK}')"),
    ("$filter", "userId eq 'x' and emplStatus eq '123'"),
    ("$filter", "startDate ge datetime'2020-01-01T00:00:00' and endDate lt 2021-01-01"),
    ("$filter", "startswith(userId,'1') and salary gt 1.5 and n lt -1 and m ge 12M"),
    ("$filter", "jobInfoNav/company in 'A','B'"),
    ("$filter", "firstName eq 'lastName ge 5'"),
    ("$filter", "userId eq 'it''s lastName ge 5'"),
    ("$orderby", "userId"),
    ("$orderby", "userId desc, jobInfoNav/startDate"),
    ("$select", "lastName,nationalId"),
    ("$expand", "personalInfoNav"),
    ("$expand", "personalInfoNav,employmentNav/jobInfoNav"),
    ("$expand", f"personalInfoNav($filter=lastName eq '{TOK}';$select=lastName)"),
    ("$expand", "personalInfoNav($select=lastName;$top=5)"),
    ("$search", ""),
    ("$top", "10"),
]


@pytest.mark.parametrize(("name", "value"), ALLOWED)
def test_token_equality_and_non_pii_queries_pass(name, value):
    check(name, value)


def test_unknown_entity_bare_fields_are_protected():
    check("$filter", f"externalCode eq '{TOK}'", entity="cust_Unknown")
    with pytest.raises(PiiQueryRefused):
        check("$filter", "externalCode eq 'X'", entity="cust_Unknown")
    # A path is judged by its last segment, not the entity.
    check("$filter", "externalCode eq 'X'", entity="cust_Other")
    check("$filter", "someNav/externalCode eq 'X'", entity="cust_Unknown")
