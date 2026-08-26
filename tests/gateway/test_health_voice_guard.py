"""Focused contracts for the hudeem-tripio Telegram voice guard."""

import pytest

from gateway.config import Platform
from gateway.health_voice_guard import build_health_voice_guard
from gateway.platforms.base import MessageType


def _decision(text: str, **overrides):
    values = {
        "platform": Platform.TELEGRAM,
        "profile_name": "hudeem-tripio",
        "message_type": MessageType.VOICE,
        "transcript": text,
    }
    values.update(overrides)
    return build_health_voice_guard(**values)


def test_ordinary_food_dictation_is_not_blocked():
    decision = _decision("На завтрак гречка, два яйца и огурец")

    assert decision is not None
    assert decision.requires_confirmation is False
    assert decision.categories == ()
    assert "process ordinary food dictation normally" in decision.instruction
    assert "Never invent a person's name" in decision.instruction


def test_pri_pp_phrase_does_not_trigger_but_tri_piva_does():
    correctly_heard = _decision("Это мой обычный ужин при ПП")
    stt_mishearing = _decision("Это мой обычный ужин, три пива")

    assert correctly_heard is not None
    assert correctly_heard.requires_confirmation is False
    assert stt_mishearing is not None
    assert stt_mishearing.requires_confirmation is True
    assert "alcohol" in stt_mishearing.categories
    assert "три пива" in stt_mishearing.instruction
    assert stt_mishearing.confirmation_question == (
        "Правильно распознал: «пива»? Подтверди или поправь эту фразу, пожалуйста."
    )
    assert "Do not call a mutating tool" in stt_mishearing.instruction


@pytest.mark.parametrize("transcript", ["Сергей съел суп и салат", "Василий выпил кефир"])
def test_person_name_requires_identity_confirmation_without_invention(transcript):
    decision = _decision(transcript)

    assert decision is not None
    assert decision.requires_confirmation is True
    assert "person identity" in decision.categories
    assert decision.confirmation_question is not None
    assert "чей дневник" in decision.confirmation_question
    assert "confirm who they are and whose journal is meant" in decision.instruction
    assert "never create or infer a participant" in decision.instruction


@pytest.mark.parametrize(
    ("transcript", "category"),
    [
        ("После еды принял метформин 500 мг", "medication"),
        ("Мне поставили диагноз диабет", "diagnosis"),
        ("Не буду есть два дня, хочу голодовку", "high-risk interpretation"),
    ],
)
def test_medical_and_high_risk_interpretations_require_confirmation(
    transcript, category
):
    decision = _decision(transcript)

    assert decision is not None
    assert decision.requires_confirmation is True
    assert category in decision.categories
    assert "nutrition or medical advice" in decision.instruction


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform": Platform.DISCORD},
        {"profile_name": "personal"},
        {"message_type": MessageType.TEXT},
    ],
)
def test_guard_is_scoped_to_telegram_health_voice(overrides):
    assert _decision("три пива", **overrides) is None


def test_transcribed_telegram_audio_uses_the_same_guard():
    decision = _decision("бокал вина", message_type=MessageType.AUDIO)

    assert decision is not None
    assert decision.requires_confirmation is True
    assert decision.categories == ("alcohol",)
