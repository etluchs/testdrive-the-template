import base64
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """In-memory data and the local dev user, reset before every test."""
    monkeypatch.setenv("APPKIT_BACKEND", "fake")
    monkeypatch.setenv("APPKIT_AUTH", "dev")
    # appkit refuses to guess a backend or an auth mode when these are set, so
    # make sure a CI runner that happens to define them cannot change what the
    # tests exercise.
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    import appkit

    appkit.reset_fakes()
    yield
    appkit.reset_fakes()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def signed_in_client():
    """A client that presents Easy Auth headers, the way the platform would.

    Use this to check what a particular user or role sees. It works because the
    tests run with ``APPKIT_AUTH=dev``, where header simulation is allowed;
    in a real deployment appkit trusts these headers only when the operator has
    declared that a trusted proxy sits in front of the app.
    """
    from app.main import app

    def make(*, name="Amelia Stucki", email="amelia.stucki@uzh.ch", roles=("approver",)):
        principal = {
            "auth_typ": "aad",
            "claims": [
                {"typ": "name", "val": name},
                {"typ": "preferred_username", "val": email},
                *[{"typ": "roles", "val": role} for role in roles],
            ],
        }
        encoded = base64.b64encode(json.dumps(principal).encode()).decode()
        return TestClient(
            app,
            headers={
                "x-ms-client-principal": encoded,
                "x-ms-client-principal-name": email,
                "x-ms-client-principal-idp": "aad",
            },
        )

    return make
