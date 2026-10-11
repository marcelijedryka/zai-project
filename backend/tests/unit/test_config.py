import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.config import Settings


def test_database_url_escapes_special_characters() -> None:
    password = "p@ss/w:rd#1%2"
    settings = Settings(
        postgres_user="zai",
        postgres_password=password,
        postgres_db="zai",
        postgres_host="db",
    )
    rendered = settings.database_url.render_as_string(hide_password=False)
    assert password not in rendered
    parsed = make_url(rendered)
    assert parsed.password == password
    assert (parsed.host, parsed.port, parsed.database) == ("db", 5432, "zai")


def test_docs_disabled_in_prod() -> None:
    settings = Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", environment="prod"
    )
    assert settings.docs_enabled is False


def _settings(**overrides: object) -> Settings:
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", **overrides)  # type: ignore[arg-type]


def test_cors_origins_are_split_from_a_csv_string() -> None:
    settings = _settings(cors_allowed_origins="http://localhost:5173, https://app.example.com")
    assert settings.cors_allowed_origins == ["http://localhost:5173", "https://app.example.com"]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://localhost:5173,*",
        "https://app.example.com/",
        "https://app.example.com/path",
        "app.example.com",
        "ftp://app.example.com",
        "https://*.example.com",
        "https://app.example.com?x=1",
    ],
)
def test_invalid_cors_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValidationError, match="invalid CORS origin"):
        _settings(cors_allowed_origins=origin)


def test_log_level_is_case_insensitive_and_validated() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        _settings(log_level="verbose")


def test_measurement_limits_have_safe_defaults_and_validate() -> None:
    settings = _settings()
    assert settings.earliest_measurement_timestamp.isoformat() == "2000-01-01T00:00:00+00:00"
    assert settings.rejection_retention_days == 30
    assert settings.rejection_rows_per_sensor_per_minute == 60
    parsed = _settings(earliest_measurement_timestamp="2024-05-01T00:00:00Z")
    assert parsed.earliest_measurement_timestamp.year == 2024
    for bad in ({"rejection_retention_days": 0}, {"rejection_rows_per_sensor_per_minute": 0}):
        with pytest.raises(ValidationError):
            _settings(**bad)
    with pytest.raises(ValidationError):
        _settings(earliest_measurement_timestamp="2024-05-01T00:00:00")  # no offset
