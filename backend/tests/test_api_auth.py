"""Auth tests for the dashboard API.

- `test_every_mutating_route_requires_auth`: static contract test that every
  non-GET route in `api.routes.router` declares `require_api_token` in its
  dependencies. Catches the case where someone adds a new POST/DELETE and
  forgets the auth dependency.
- The behavior tests mount `require_api_token` on a minimal FastAPI app rather
  than booting the full bot (which would require DB, WS, etc.). They monkeypatch
  `_expected_token` so we don't have to wrestle with module-reload semantics
  around the frozen-dataclass `Settings` singleton.
"""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import auth as auth_module
from api.auth import require_api_token
from api.routes import router

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def test_every_mutating_route_requires_auth():
    failures = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if not (route.methods & UNSAFE_METHODS):
            continue
        deps = [d.call for d in route.dependant.dependencies]
        if require_api_token not in deps:
            failures.append(f"{sorted(route.methods)} {route.path}")
    assert not failures, f"non-GET routes missing require_api_token: {failures}"


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/safe")
    async def safe():
        return {"ok": True}

    @app.post("/protected", dependencies=[Depends(require_api_token)])
    async def protected():
        return {"ok": True}

    return app


@pytest.fixture
def app_with_token(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(auth_module, "_expected_token", lambda: "test-token-123")
    yield TestClient(_build_app())


@pytest.fixture
def app_no_token(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setattr(auth_module, "_expected_token", lambda: "")
    yield TestClient(_build_app())


def test_auth_disabled_when_token_unset(app_no_token: TestClient):
    r = app_no_token.post("/protected")
    assert r.status_code == 200, "auth must be a no-op when DASHBOARD_API_TOKEN is unset"


def test_safe_method_never_requires_auth(app_with_token: TestClient):
    r = app_with_token.get("/safe")
    assert r.status_code == 200


def test_missing_header_rejected(app_with_token: TestClient):
    r = app_with_token.post("/protected")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_wrong_token_rejected(app_with_token: TestClient):
    r = app_with_token.post("/protected", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_correct_token_accepted(app_with_token: TestClient):
    r = app_with_token.post("/protected", headers={"Authorization": "Bearer test-token-123"})
    assert r.status_code == 200


def test_malformed_authorization_rejected(app_with_token: TestClient):
    r = app_with_token.post("/protected", headers={"Authorization": "test-token-123"})
    assert r.status_code == 401, "must reject auth header missing 'Bearer ' prefix"


def test_basic_auth_scheme_rejected(app_with_token: TestClient):
    r = app_with_token.post("/protected", headers={"Authorization": "Basic dGVzdA=="})
    assert r.status_code == 401
