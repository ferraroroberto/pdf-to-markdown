"""Authentication factory for Vertex AI / Google Gemini clients.

Two supported modes
-------------------
``api``     — Vertex AI Express Mode.  Requires ``GOOGLE_API_KEY`` env var.
``gcloud``  — Application Default Credentials (ADC).  Requires a prior
              ``gcloud auth application-default login`` or a service-account
              key pointed to by ``GOOGLE_APPLICATION_CREDENTIALS``.

Usage
-----
    from src.auth import build_client

    client = build_client(auth_mode="api", project_id="my-proj", location="europe-west3")
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("auth")

API_MODE = "api"
GCLOUD_MODE = "gcloud"
VALID_MODES = (API_MODE, GCLOUD_MODE)


class ConfigError(ValueError):
    """Raised when required auth configuration is missing."""


def build_client(auth_mode: str, project_id: str, location: str) -> object:
    """Build and return a ``google.genai.Client`` configured for Vertex AI.

    Parameters
    ----------
    auth_mode:
        ``"api"`` for Express Mode (API key) or ``"gcloud"`` for ADC.
    project_id:
        Google Cloud project ID.  Required for both modes.
    location:
        Vertex AI region, e.g. ``"europe-west3"``.

    Raises
    ------
    ConfigError
        If required credentials or configuration are missing.
    ImportError
        If ``google-genai`` is not installed.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ImportError(
            "google-genai is not installed. Run: pip install google-genai"
        ) from exc

    if auth_mode not in VALID_MODES:
        raise ConfigError(
            f"Unknown auth_mode '{auth_mode}'. Valid choices: {VALID_MODES}"
        )

    if not project_id:
        raise ConfigError(
            "project_id is required. Set it in config.json or pass --project-id."
        )

    if auth_mode == API_MODE:
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            raise ConfigError(
                "auth_mode='api' requires GOOGLE_API_KEY environment variable. "
                "Set it in your .env file or export it before running."
            )
        logger.info("ℹ️ Authenticating via Vertex AI Express Mode (API key)")
        logger.debug("API key present (%d chars), project=%s, location=%s",
                      len(api_key), project_id, location)
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", location)
        client = genai.Client(
            vertexai=True,
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1beta1"),
        )
        logger.debug("API-mode client created successfully")
        return client

    # auth_mode == GCLOUD_MODE
    logger.info(
        "ℹ️ Authenticating via ADC (gcloud) — project=%s, location=%s",
        project_id, location,
    )
    logger.debug("Building gcloud client with ADC — project=%s, location=%s", project_id, location)
    client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        http_options=types.HttpOptions(api_version="v1"),
    )
    logger.debug("ADC-mode client created successfully")
    return client
