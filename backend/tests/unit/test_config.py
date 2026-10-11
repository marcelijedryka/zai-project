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
