import pytest

from app.core.problems import MAX_VALIDATION_ERRORS, _field, problem_response


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (("body", "name"), "name"),
        (("body", "location", "path"), "location.path"),  # nested "path" keeps its name
        (("body", "path"), "path"),
        (("body", "items", 3, "query"), "items.3.query"),
        (("query", "limit"), "limit"),
        (("path", "series_id"), "series_id"),
        (("body",), "body"),
        ((), "body"),
        (("unknown", "x"), "unknown.x"),
    ],
)
def test_field_drops_only_the_leading_request_part(loc: tuple[object, ...], expected: str) -> None:
    assert _field(loc) == expected


def test_extensions_cannot_overwrite_standard_members() -> None:
    response = problem_response(
        409,
        detail="real detail",
        extensions={"status": 1, "title": "spoofed", "type": "urn:x", "dataMin": 1.5},
    )
    body = bytes(response.body).decode()
    assert '"status":409' in body
    assert '"title":"Conflict"' in body
    assert '"type":"about:blank"' in body
    assert '"dataMin":1.5' in body


def test_validation_error_cap_is_sane() -> None:
    assert 1 <= MAX_VALIDATION_ERRORS <= 100
