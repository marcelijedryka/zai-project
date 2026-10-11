import pytest
from starlette.datastructures import Headers

from app.core.middleware import accepts, has_body, is_json_media_type

JSON = ("application/json", "application/problem+json")
SSE = ("text/event-stream",)


@pytest.mark.parametrize(
    ("accept", "offered", "expected"),
    [
        ("application/json", JSON, True),
        ("application/problem+json", JSON, True),
        ("*/*", JSON, True),
        ("application/*", JSON, True),
        ("text/html, application/json;q=0.9", JSON, True),
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", JSON, True),
        ("APPLICATION/JSON", JSON, True),
        ("application/xml", JSON, False),
        ("text/html", JSON, False),
        ("application/json;q=0", JSON, False),
        ("application/json;q=abc", JSON, False),
        ("text/*", JSON, False),
        ("application/json", SSE, False),
        ("text/event-stream", SSE, True),
        ("*/*", SSE, True),
    ],
)
def test_accepts(accept: str, offered: tuple[str, ...], expected: bool) -> None:
    assert accepts(accept, offered) is expected


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("Application/JSON", True),
        ("application/vnd.api+json", True),
        ("application/problem+json", True),
        ("text/plain", False),
        ("application/xml", False),
        ("application/x-www-form-urlencoded", False),
        ("text/json", False),
        ("", False),
    ],
)
def test_is_json_media_type(content_type: str, expected: bool) -> None:
    assert is_json_media_type(content_type) is expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, False),
        ({"content-length": "0"}, False),
        ({"content-length": "7"}, True),
        ({"transfer-encoding": "chunked"}, True),
        ({"content-length": "garbage"}, True),
    ],
)
def test_has_body(headers: dict[str, str], expected: bool) -> None:
    assert has_body(Headers(headers)) is expected
