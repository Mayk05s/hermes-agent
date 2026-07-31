import base64
from pathlib import Path


def test_gemini_provider_requires_api_key(monkeypatch, tmp_path):
    from plugins.image_gen.gemini import GeminiImageGenProvider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = GeminiImageGenProvider().generate("draw a cat", "square")

    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_gemini_provider_saves_inline_image(monkeypatch, tmp_path):
    import httpx
    from plugins.image_gen.gemini import GeminiImageGenProvider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nstub").decode("ascii")

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Done"},
                                {"inlineData": {"mimeType": "image/png", "data": png_b64}},
                            ]
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, *, headers, json):
            self.calls.append((url, headers, json))
            assert headers["x-goog-api-key"] == "test-key"
            assert "gemini-2.5-flash-image:generateContent" in url
            assert json["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = GeminiImageGenProvider().generate("draw a cat", "square")

    assert result["success"] is True
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-2.5-flash-image"
    assert Path(result["image"]).is_file()
    assert Path(result["image"]).read_bytes() == b"\x89PNG\r\n\x1a\nstub"
