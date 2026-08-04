import inspect

from fastapi.testclient import TestClient

from agent_hub.app import create_app


def test_health_endpoint() -> None:
    client = TestClient(create_app())
    response = client.get('/health/live')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_health_endpoint_is_tagged_as_system() -> None:
    application = create_app()
    route = next(route for route in application.routes if route.path == "/health/live")
    assert route.tags == ["system"]


def test_health_endpoint_handler_is_async() -> None:
    application = create_app()
    route = next(route for route in application.routes if route.path == "/health/live")
    assert inspect.iscoroutinefunction(route.endpoint)
