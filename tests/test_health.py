from httpx import AsyncClient


async def test_health_returns_200_with_ok_status(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_unknown_path_returns_404(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
