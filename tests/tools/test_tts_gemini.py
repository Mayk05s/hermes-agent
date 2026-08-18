"""Tests for the Google Gemini TTS provider in tools/tts_tool.py."""

import base64
import json
import struct
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_BASE_URL",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_pcm_bytes():
    # 0.1s of silence at 24kHz mono 16-bit = 4800 bytes
    return b"\x00" * 4800


@pytest.fixture
def mock_gemini_response(fake_pcm_bytes):
    """A successful Gemini generateContent response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/L16;codec=pcm;rate=24000",
                                "data": base64.b64encode(fake_pcm_bytes).decode(),
                            }
                        }
                    ]
                }
            }
        ]
    }
    return resp


class TestWrapPcmAsWav:
    def test_riff_header_structure(self):
        from tools.tts_tool import _wrap_pcm_as_wav

        pcm = b"\x01\x02\x03\x04" * 10
        wav = _wrap_pcm_as_wav(pcm, sample_rate=24000, channels=1, sample_width=2)

        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert wav[12:16] == b"fmt "
        # Audio format (PCM=1)
        assert struct.unpack("<H", wav[20:22])[0] == 1
        # Channels
        assert struct.unpack("<H", wav[22:24])[0] == 1
        # Sample rate
        assert struct.unpack("<I", wav[24:28])[0] == 24000
        # Bits per sample
        assert struct.unpack("<H", wav[34:36])[0] == 16
        assert wav[36:40] == b"data"
        assert wav[44:] == pcm

    def test_header_size_is_44(self):
        from tools.tts_tool import _wrap_pcm_as_wav

        pcm = b"\xff" * 100
        wav = _wrap_pcm_as_wav(pcm)
        assert len(wav) == 44 + len(pcm)


def test_prebuilt_voice_list_matches_nanoclaw_samples():
    from tools.tts_tool import GEMINI_TTS_PREBUILT_VOICES

    assert GEMINI_TTS_PREBUILT_VOICES == (
        "Achernar",
        "Achird",
        "Algenib",
        "Algieba",
        "Alnilam",
        "Aoede",
        "Autonoe",
        "Callirrhoe",
        "Charon",
        "Despina",
        "Enceladus",
        "Erinome",
        "Fenrir",
        "Gacrux",
        "Iapetus",
        "Kore",
        "Laomedeia",
        "Leda",
        "Orus",
        "Puck",
        "Pulcherrima",
        "Rasalgethi",
        "Sadachbia",
        "Sadaltager",
        "Schedar",
        "Sulafat",
        "Umbriel",
        "Vindemiatrix",
        "Zephyr",
        "Zubenelgenubi",
    )


class TestGenerateGeminiTts:
    def test_missing_api_key_raises_value_error(self, tmp_path):
        from tools.tts_tool import _generate_gemini_tts

        output_path = str(tmp_path / "test.wav")
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            _generate_gemini_tts("Hello", output_path, {})

    def test_google_api_key_fallback(self, tmp_path, monkeypatch, mock_gemini_response):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GOOGLE_API_KEY", "from-google-env")
        output_path = str(tmp_path / "test.wav")

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", output_path, {})

        # Confirm it used the GOOGLE_API_KEY as the query parameter
        _, kwargs = mock_post.call_args
        assert kwargs["params"]["key"] == "from-google-env"

    def test_wav_output_fast_path(self, tmp_path, monkeypatch, mock_gemini_response, fake_pcm_bytes):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        output_path = str(tmp_path / "test.wav")

        with patch("requests.post", return_value=mock_gemini_response):
            result = _generate_gemini_tts("Hi", output_path, {})

        assert result == output_path
        data = (tmp_path / "test.wav").read_bytes()
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"
        # Audio payload should match the PCM we put in
        assert data[44:] == fake_pcm_bytes

    def test_default_voice_and_model(self, tmp_path, monkeypatch, mock_gemini_response):
        from tools.tts_tool import (
            DEFAULT_GEMINI_TTS_MODEL,
            DEFAULT_GEMINI_TTS_VOICE,
            _generate_gemini_tts,
        )

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), {})

        args, kwargs = mock_post.call_args
        assert DEFAULT_GEMINI_TTS_MODEL in args[0]
        payload = kwargs["json"]
        voice = (
            payload["generationConfig"]["speechConfig"]["voiceConfig"]
            ["prebuiltVoiceConfig"]["voiceName"]
        )
        assert voice == DEFAULT_GEMINI_TTS_VOICE

    def test_custom_voice(self, tmp_path, monkeypatch, mock_gemini_response):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        config = {"gemini": {"voice": "Puck"}}

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), config)

        payload = mock_post.call_args[1]["json"]
        voice = (
            payload["generationConfig"]["speechConfig"]["voiceConfig"]
            ["prebuiltVoiceConfig"]["voiceName"]
        )
        assert voice == "Puck"

    def test_custom_model(self, tmp_path, monkeypatch, mock_gemini_response):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        config = {"gemini": {"model": "gemini-2.5-pro-preview-tts"}}

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), config)

        endpoint = mock_post.call_args[0][0]
        assert "gemini-2.5-pro-preview-tts" in endpoint

    def test_custom_style_is_separated_from_verbatim_transcript(
        self, tmp_path, monkeypatch, mock_gemini_response
    ):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        config = {"gemini": {"style": "warm, calm Russian voice"}}

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Привет", str(tmp_path / "test.wav"), config)

        payload = mock_post.call_args[1]["json"]
        prompt = payload["contents"][0]["parts"][0]["text"]
        assert prompt.startswith("TTS: Synthesize speech only")
        assert "VOICE DIRECTION (DO NOT READ ALOUD):\nwarm, calm Russian voice" in prompt
        assert prompt.endswith("TRANSCRIPT — READ VERBATIM:\nПривет")

    def test_blank_style_still_uses_explicit_audio_only_preamble(
        self, tmp_path, monkeypatch, mock_gemini_response
    ):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        config = {"gemini": {"style": "  "}}

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), config)

        payload = mock_post.call_args[1]["json"]
        prompt = payload["contents"][0]["parts"][0]["text"]
        assert prompt.startswith("TTS: Synthesize speech only")
        assert "VOICE DIRECTION" not in prompt
        assert prompt.endswith("TRANSCRIPT — READ VERBATIM:\nHi")

    def test_response_modality_is_audio(self, tmp_path, monkeypatch, mock_gemini_response):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), {})

        payload = mock_post.call_args[1]["json"]
        assert payload["generationConfig"]["responseModalities"] == ["AUDIO"]

    def test_http_error_raises_runtime_error(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        err_resp = MagicMock()
        err_resp.status_code = 400
        err_resp.json.return_value = {"error": {"message": "Invalid voice"}}

        with patch("requests.post", return_value=err_resp):
            with pytest.raises(RuntimeError, match="HTTP 400.*Invalid voice"):
                _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), {})

    def test_empty_audio_raises(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"data": ""}}]}}
            ]
        }

        with patch("requests.post", return_value=resp):
            with pytest.raises(RuntimeError, match="empty audio"):
                _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), {})

    def test_malformed_response_raises(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"candidates": []}  # no content

        with patch("requests.post", return_value=resp):
            with pytest.raises(RuntimeError, match="malformed"):
                _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), {})

    def test_block_without_fallback_preserves_attempt_diagnostics(
        self, tmp_path, monkeypatch
    ):
        from tools.tts_tool import GeminiTTSGenerationError, _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        blocked = MagicMock()
        blocked.status_code = 200
        blocked.json.return_value = {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}
        }
        config = {
            "gemini": {
                "model": "primary-tts-model",
                "fallback_model": "",
            }
        }

        with patch("requests.post", return_value=blocked):
            with pytest.raises(GeminiTTSGenerationError) as exc_info:
                _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), config)

        assert len(exc_info.value.attempts) == 2
        assert {attempt["prompt_variant"] for attempt in exc_info.value.attempts} == {
            "directed",
            "minimal_retry",
        }
        assert all(
            attempt["model"] == "primary-tts-model"
            and attempt["error_code"] == "content_policy_block"
            and attempt["provider_reason"] == "PROHIBITED_CONTENT"
            for attempt in exc_info.value.attempts
        )

    def test_prompt_feedback_block_retries_same_model_with_minimal_prompt(
        self, tmp_path, monkeypatch, mock_gemini_response
    ):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        blocked = MagicMock()
        blocked.status_code = 200
        blocked.json.return_value = {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}
        }
        config = {
            "gemini": {
                "model": "gemini-3.1-flash-tts-preview",
                "fallback_model": "gemini-2.5-flash-preview-tts",
                "style": "warm",
            }
        }

        with patch(
            "requests.post",
            side_effect=[blocked, mock_gemini_response],
        ) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), config)

        assert "gemini-3.1-flash-tts-preview" in mock_post.call_args_list[0][0][0]
        assert "gemini-3.1-flash-tts-preview" in mock_post.call_args_list[1][0][0]
        retry_prompt = mock_post.call_args_list[1][1]["json"]["contents"][0]["parts"][0]["text"]
        assert "VOICE DIRECTION" not in retry_prompt
        assert retry_prompt.endswith("TRANSCRIPT — READ VERBATIM:\nHi")
        assert (tmp_path / "test.wav").read_bytes()[:4] == b"RIFF"

    def test_fallback_model_after_both_primary_prompt_variants_fail(
        self, tmp_path, monkeypatch, mock_gemini_response
    ):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        blocked = MagicMock()
        blocked.status_code = 200
        blocked.json.return_value = {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}
        }
        config = {
            "gemini": {
                "model": "primary-tts-model",
                "fallback_model": "fallback-tts-model",
            }
        }

        with patch(
            "requests.post",
            side_effect=[blocked, blocked, mock_gemini_response],
        ) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), config)

        assert len(mock_post.call_args_list) == 3
        assert "primary-tts-model" in mock_post.call_args_list[0][0][0]
        assert "primary-tts-model" in mock_post.call_args_list[1][0][0]
        assert "fallback-tts-model" in mock_post.call_args_list[2][0][0]
        assert (tmp_path / "test.wav").read_bytes()[:4] == b"RIFF"

    def test_tool_returns_actionable_content_block_diagnostics(
        self, tmp_path, monkeypatch
    ):
        from tools.tts_tool import text_to_speech_tool

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        blocked = MagicMock()
        blocked.status_code = 200
        blocked.json.return_value = {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}
        }
        config = {
            "provider": "gemini",
            "gemini": {
                "model": "primary-tts-model",
                "fallback_model": "",
                "voice": "Enceladus",
            },
        }

        with patch("tools.tts_tool._load_tts_config", return_value=config), \
             patch("requests.post", return_value=blocked):
            result = json.loads(text_to_speech_tool(
                "спасибо", output_path=str(tmp_path / "test.wav")
            ))

        assert result["success"] is False
        assert result["error_code"] == "content_policy_block"
        assert result["configured_model"] == "primary-tts-model"
        assert result["attempted_models"] == ["primary-tts-model"]
        assert result["attempts"][0]["provider_reason"] == "PROHIBITED_CONTENT"
        assert result["fallback_configured"] is False
        assert result["fallback_attempted"] is False
        assert "not evidence" in result["explanation"]
        assert "Do not reduce this" in result["assistant_instruction"]

    def test_snake_case_inline_data_accepted(self, tmp_path, monkeypatch, fake_pcm_bytes):
        """Some Gemini SDK versions return inline_data instead of inlineData."""
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inline_data": {
                                    "data": base64.b64encode(fake_pcm_bytes).decode()
                                }
                            }
                        ]
                    }
                }
            ]
        }

        output_path = str(tmp_path / "test.wav")
        with patch("requests.post", return_value=resp):
            _generate_gemini_tts("Hi", output_path, {})

        data = (tmp_path / "test.wav").read_bytes()
        assert data[:4] == b"RIFF"

    def test_custom_base_url_env(self, tmp_path, monkeypatch, mock_gemini_response):
        from tools.tts_tool import _generate_gemini_tts

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_BASE_URL", "https://custom-gemini.example.com/v1beta")

        with patch("requests.post", return_value=mock_gemini_response) as mock_post:
            _generate_gemini_tts("Hi", str(tmp_path / "test.wav"), {})

        assert mock_post.call_args[0][0].startswith("https://custom-gemini.example.com/v1beta/")


class TestGeminiInCheckRequirements:
    def test_gemini_api_key_satisfies_requirements(self, monkeypatch):
        from tools.tts_tool import check_tts_requirements

        # Strip everything else
        for key in (
            "ELEVENLABS_API_KEY",
            "OPENAI_API_KEY",
            "VOICE_TOOLS_OPENAI_KEY",
            "MINIMAX_API_KEY",
            "XAI_API_KEY",
            "MISTRAL_API_KEY",
            "GOOGLE_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "k")

        # Force edge_tts import to fail so we actually hit the gemini check
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "edge_tts":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            assert check_tts_requirements() is True
