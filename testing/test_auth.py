"""Tests for the Vertex AI auth factory — no network, no real credentials.

Focus: ``api`` mode must mirror the *active* machine profile into the process
environment on every call.  A ``setdefault`` there pinned the first profile's
project/location for the lifetime of the long-lived Streamlit process, so
switching machines in Settings silently kept converting against the old one.
"""

from __future__ import annotations

import os

import pytest

from src.auth import ConfigError, build_client

_ENV_VARS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


@pytest.fixture
def api_env(monkeypatch):
    """Clean api-mode environment: a key present, no GOOGLE_CLOUD_* leftovers."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_api_mode_switching_profile_updates_env(api_env):
    build_client(auth_mode="api", project_id="proj-a", location="europe-west3")
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "proj-a"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "europe-west3"

    # Same process, user switches the active machine in the Settings tab.
    build_client(auth_mode="api", project_id="proj-b", location="us-central1")
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "proj-b"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us-central1"


def test_api_mode_config_wins_over_preexisting_export(api_env):
    api_env.setenv("GOOGLE_CLOUD_PROJECT", "exported-elsewhere")
    api_env.setenv("GOOGLE_CLOUD_LOCATION", "asia-east1")

    build_client(auth_mode="api", project_id="from-config", location="europe-west3")

    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "from-config"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "europe-west3"


def test_api_mode_blank_location_clears_stale_value(api_env):
    api_env.setenv("GOOGLE_CLOUD_LOCATION", "europe-west3")

    build_client(auth_mode="api", project_id="proj-a", location="")

    # A profile with no location must not leave the previous one in place, and
    # must not pin an empty string either.
    assert "GOOGLE_CLOUD_LOCATION" not in os.environ


def test_api_mode_requires_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_client(auth_mode="api", project_id="proj-a", location="europe-west3")


def test_unknown_auth_mode_rejected(api_env):
    with pytest.raises(ConfigError):
        build_client(auth_mode="oauth", project_id="proj-a", location="europe-west3")
