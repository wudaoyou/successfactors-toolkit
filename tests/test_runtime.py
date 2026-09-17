from pathlib import Path

from fastapi.testclient import TestClient

from successfactors_toolkit.main import app


def test_rest_startup_health_openapi_and_shutdown():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        schema = client.get("/openapi.json").json()
        assert "/api/odata/execute" in schema["paths"]
        assert "/api/sfapi/ce/query-by-person-id" in schema["paths"]
        assert not app.state.http_client.is_closed
    assert app.state.http_client.is_closed


def test_api_access_fails_closed_and_admin_needs_both_keys(monkeypatch):
    with TestClient(app) as client:
        assert client.get("/api/tenants").status_code == 503
    from successfactors_toolkit.config import get_settings

    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/tenants").status_code == 401
        assert client.get("/api/tenants", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/tenants", headers={"X-API-Key": "test-api-key"}).status_code == 401
        both = {"X-API-Key": "test-api-key", "X-Admin-Key": "test-admin-key"}
        assert client.get("/api/tenants", headers=both).status_code == 200
        assert (
            client.delete("/api/tenants/example", headers={"X-API-Key": "test-api-key"}).status_code
            == 401
        )
        response = client.options(
            "/api/tenants",
            headers={"Origin": "https://other.invalid", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in response.headers


def test_results_dir_defaults_to_results_and_follows_the_environment(monkeypatch, tmp_path):
    from successfactors_toolkit.config import get_settings

    assert get_settings().results_dir == Path("results")
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "payloads"))
    get_settings.cache_clear()
    assert get_settings().results_dir == tmp_path / "payloads"
