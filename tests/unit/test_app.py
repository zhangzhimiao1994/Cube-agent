from fastapi.testclient import TestClient

from agent_hub.app import create_app


def test_health_endpoint() -> None:
    client = TestClient(create_app())
    response = client.get('/health/live')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
