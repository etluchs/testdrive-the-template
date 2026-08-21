"""The deployment must configure appkit, and must fail loudly if it doesn't.

appkit refuses to guess `APPKIT_BACKEND` or `APPKIT_AUTH` on an Azure app
platform, because a wrong guess fails silently: mail is discarded, database
writes vanish on restart, and every caller is signed in as the dev user, while
each page still renders as though it worked. These tests pin the configuration
that keeps that from happening.
"""

from pathlib import Path

import pytest
from appkit import ConfigError

DOCKERFILE = (Path(__file__).parent.parent / "Dockerfile").read_text()


def test_the_image_selects_the_azure_backend():
    assert "APPKIT_BACKEND=azure" in DOCKERFILE


def test_the_image_declares_an_auth_mode():
    """Without this, the container raises on the first request it serves."""
    assert "APPKIT_AUTH=easyauth" in DOCKERFILE


def test_a_deployment_missing_its_auth_mode_refuses_to_serve(client, monkeypatch):
    monkeypatch.setenv("CONTAINER_APP_NAME", "adate")
    monkeypatch.delenv("APPKIT_AUTH", raising=False)

    with pytest.raises(ConfigError, match="APPKIT_AUTH is not set"):
        client.get("/")


def test_a_deployment_left_on_the_dev_user_refuses_to_serve(client, monkeypatch):
    """APPKIT_AUTH=dev in Azure would sign every caller in. appkit refuses."""
    monkeypatch.setenv("CONTAINER_APP_NAME", "adate")
    monkeypatch.setenv("APPKIT_AUTH", "dev")

    with pytest.raises(ConfigError, match="refused on an Azure app platform"):
        client.get("/")


def test_the_signed_in_user_is_shown(signed_in_client):
    body = signed_in_client(name="Amelia Stucki").get("/").text

    assert "Signed in as " in body
    assert "Amelia Stucki" in body


def test_the_app_never_reads_the_easy_auth_headers_itself(signed_in_client):
    """Identity must come from appkit, which applies whatever APPKIT_AUTH says.

    Reading ``x-ms-client-principal*`` directly would bypass that: on any request
    path that does not pass through Easy Auth, a caller can set those headers
    themselves (AGENTS.md).
    """
    app_dir = Path(__file__).parent.parent / "app"
    for source in app_dir.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "x-ms-client-principal" not in text.lower(), (
            f"{source.name} reads an Easy Auth header directly; use auth.user(request)"
        )

    # And the identity appkit resolved does reach the page.
    assert "Amelia Stucki" in signed_in_client().get("/").text
