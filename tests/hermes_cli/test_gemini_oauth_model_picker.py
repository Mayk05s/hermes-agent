"""Regression coverage for Gemini OAuth in the dashboard/model picker."""


def test_gemini_oauth_appears_authenticated_in_model_picker(tmp_path, monkeypatch):
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("hermes_cli.models.get_curated_nous_model_ids", lambda: [])
    monkeypatch.setattr("hermes_cli.models.fetch_ollama_cloud_models", lambda: [])
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids",
        lambda provider, *args, **kwargs: (
            ["gemini-3.5-flash"] if provider == "google-gemini-cli" else []
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.auth.get_gemini_oauth_auth_status",
        lambda: {
            "logged_in": True,
            "source": "google-oauth",
            "api_key": "token",
            "expires_at_ms": 1_775_640_710_946,
            "email": "tek@nous.ai",
        },
    )

    providers = list_authenticated_providers(max_models=10)

    gemini = next(p for p in providers if p["slug"] == "google-gemini-cli")
    assert gemini["name"] == "Google Gemini (OAuth)"
    assert gemini["models"] == ["gemini-3.5-flash"]
    assert gemini["total_models"] == 1
