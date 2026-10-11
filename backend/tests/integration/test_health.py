import httpx

from app.config import Settings
from app.main import create_app


async def test_health_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_503_when_database_is_down(settings: Settings) -> None:
    bad = settings.model_copy(
        update={"postgres_host": "127.0.0.1", "postgres_port": 1},
    )
    app = create_app(bad)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        response = await c.get("/api/health")
    assert response.status_code == 503
