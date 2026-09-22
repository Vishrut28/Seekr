"""A page size with no floor returns the whole table.

`limit` on /v1/persons was `Query(50, le=500)`: bounded above, unbounded
below. SQLite and Postgres both read `LIMIT -1` as "no limit", so
`?limit=-1` answered with every person in the corpus -- 769 rows and 325 KB
at the time, and however many there are later. `?limit=99999` was refused in
the same breath, which is what made it easy to miss: the parameter looked
validated. The line directly beneath it, `offset`, had `ge=0` all along.

The structural test is the one that matters here. Five endpoints had the same
omission, so testing the five would only pin the five; the rule is that a
parameter bounded above and not below is an oversight, whoever writes the
next one.
"""

from fastapi.testclient import TestClient

from rip import api


def _integer_query_params():
    found = []
    for route in api.app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for param in dependant.query_params:
            if getattr(param.field_info, "annotation", None) is int:
                found.append((route.path, param))
    return found


INTEGER_QUERY_PARAMS = _integer_query_params()


def bounds(param):
    meta = getattr(param.field_info, "metadata", [])
    low = next((m.ge for m in meta if hasattr(m, "ge")), None)
    high = next((m.le for m in meta if hasattr(m, "le")), None)
    return low, high


def test_every_integer_parameter_bounded_above_is_bounded_below():
    assert INTEGER_QUERY_PARAMS, "found no integer query parameters to check"
    unbounded = []
    for path, param in INTEGER_QUERY_PARAMS:
        low, high = bounds(param)
        if high is not None and low is None:
            unbounded.append(f"{path} ?{param.name}")
    assert not unbounded, (
        "bounded above, unbounded below — a negative value reaches the SQL as "
        f"LIMIT -1, which is no limit at all: {unbounded}")


def test_a_negative_page_size_is_refused_rather_than_unlimited():
    client = TestClient(api.app)
    for path in ("/v1/persons", "/v1/changes", "/v1/facets?field=country"):
        joiner = "&" if "?" in path else "?"
        for value in (-1, -999999):
            got = client.get(f"{path}{joiner}limit={value}")
            assert got.status_code == 422, f"{path} limit={value} -> {got.status_code}"


def test_the_page_size_is_still_bounded_above():
    client = TestClient(api.app)
    assert client.get("/v1/persons?limit=99999").status_code == 422
    assert client.get("/v1/persons?limit=500").status_code == 200


def test_zero_still_means_the_query_default_where_that_is_the_contract():
    """/v1/query documents `0 = query default`, so nought is a value there and
    the floor is ge=0, not ge=1. Everywhere else a page of nothing is a
    mistake worth reporting."""
    low, _high = bounds(next(p for path, p in INTEGER_QUERY_PARAMS
                             if path == "/v1/query" and p.name == "limit"))
    assert low == 0
    low, _high = bounds(next(p for path, p in INTEGER_QUERY_PARAMS
                             if path == "/v1/persons" and p.name == "limit"))
    assert low == 1
