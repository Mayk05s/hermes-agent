"""Trusted prompt guard for ambiguous nutrition voice transcripts.

The classifier is deliberately deterministic and narrow.  It does not try to
correct speech-to-text; it only tells the nutrition agent when a high-impact
interpretation must be confirmed before it is used.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional


_SUPPORTED_PROFILE = "hudeem-tripio"
_SUPPORTED_MESSAGE_TYPES = frozenset({"voice", "audio"})

_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "person identity",
        re.compile(
            r"\b(?i:серге(?:й|я|ю|ем)|александр(?:а|у|ом)?|саш(?:а|и|е|у|ей)|"
            r"татьян(?:а|ы|е|у|ой)|тан(?:я|и|е|ю|ей)|михаил(?:а|у|ом)?|миш(?:а|и|е|у|ей)|"
            r"муж|жена|супруг(?:а|и|у|ом)?|партн[её]р(?:а|у|ом)?|друг|подруг(?:а|и|е|у|ой)|"
            r"сын|дочь|реб[её]нок|мама|папа|коллег(?:а|и|е|у|ой))\b|"
            r"(?:^|[\s,(])(?:[А-ЯЁ][а-яё]{2,})\s+"
            r"(?i:съел\w*|поел\w*|выпил\w*|весит\w*|похудел\w*|набрал\w*)\b",
        ),
    ),
    (
        "alcohol",
        re.compile(
            r"\b(?:алкогол\w*|пив\w*|вин(?:о|а|ом|е)?|водк\w*|шампанск\w*|"
            r"коньяк\w*|виски|ром(?:а|ом)?|джин(?:а|ом)?|сидр\w*|коктейл\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "medication",
        re.compile(
            r"\b(?:лекарств\w*|препарат\w*|таблет\w*|капсул\w*|антибиотик\w*|"
            r"инсулин\w*|метформин\w*|оземпик\w*|семаглутид\w*|тирзепатид\w*|"
            r"антидепрессант\w*|дозиров\w*|доз[ауеы]\b|\d+(?:[.,]\d+)?\s*мг\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "diagnosis",
        re.compile(
            r"\b(?:диагноз\w*|диабет\w*|гипертони\w*|гипотиреоз\w*|гастрит\w*|"
            r"панкреатит\w*|аллерги\w*|анорекси\w*|булими\w*|ожирени\w*|"
            r"инсулинорезистент\w*|рпп\b)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "high-risk interpretation",
        re.compile(
            r"(?:\b(?:передоз\w*|обморок\w*|голодов\w*|слабительн\w*|"
            r"неразборчив\w*|не\s+уверен\w*|возможно)\b|"
            r"\bне\s+буду\s+есть\b|\bвызв\w*\s+рвот\w*\b|"
            r"\bбол\w*\s+в\s+груди\b|\bне\s+могу\s+дышать\b)",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class HealthVoiceGuardDecision:
    """A trusted instruction and whether this transcript needs confirmation."""

    instruction: str
    requires_confirmation: bool
    categories: tuple[str, ...]
    confirmation_question: Optional[str] = None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def classify_health_voice_transcript(transcript: str) -> tuple[str, ...]:
    """Return high-impact categories explicitly present in an STT transcript."""
    text = str(transcript or "")
    return tuple(label for label, pattern in _CATEGORY_PATTERNS if pattern.search(text))


def _confirmation_question(transcript: str, categories: tuple[str, ...]) -> str:
    """Build one deterministic, transcript-grounded clarification question."""
    text = re.sub(r"\s+", " ", str(transcript or "")).strip()
    excerpts: list[str] = []
    for label, pattern in _CATEGORY_PATTERNS:
        if label not in categories:
            continue
        match = pattern.search(text)
        if not match:
            continue
        excerpt = re.sub(r"\s+", " ", match.group(0)).strip(" ,.;:!?—-")[:80]
        if excerpt and excerpt.casefold() not in {item.casefold() for item in excerpts}:
            excerpts.append(excerpt)
    quoted = " / ".join(excerpts[:2])
    if quoted:
        question = f"Правильно распознал: «{quoted}»?"
    else:  # pragma: no cover - categories always originate from these patterns
        question = "Правильно ли я распознал неоднозначную часть голосового сообщения?"
    if "person identity" in categories:
        question += " Уточни, пожалуйста, о ком речь и чей дневник имеется в виду."
    else:
        question += " Подтверди или поправь эту фразу, пожалуйста."
    return question


def build_health_voice_guard(
    *,
    platform: Any,
    profile_name: str,
    message_type: Any,
    transcript: str,
) -> Optional[HealthVoiceGuardDecision]:
    """Build a profile-scoped system instruction for a Telegram voice turn.

    Ordinary nutrition dictation gets only the non-invention invariant and is
    explicitly allowed to proceed.  High-impact terms add a confirmation gate.
    Text messages and every other profile/platform are left untouched.
    """
    if (
        _enum_value(platform) != "telegram"
        or str(profile_name or "").strip().lower() != _SUPPORTED_PROFILE
        or _enum_value(message_type) not in _SUPPORTED_MESSAGE_TYPES
    ):
        return None

    categories = classify_health_voice_transcript(transcript)
    base = (
        "[Trusted gateway instruction: this turn came from Telegram speech-to-text. "
        "Use only the verified sender as the food-journal owner. Never invent a "
        "person's name, identity, medication, diagnosis, symptoms, dosage, or other "
        "medical context that is not explicit in the current transcript. Do not "
        "silently repair an ambiguous phrase using guesses from history.]"
    )
    if not categories:
        return HealthVoiceGuardDecision(
            instruction=(
                f"{base}\n"
                "[No high-impact ambiguity was detected. Do not ask for confirmation "
                "solely because the input was spoken; process ordinary food dictation "
                "normally.]"
            ),
            requires_confirmation=False,
            categories=(),
            confirmation_question=None,
        )

    labels = ", ".join(categories)
    confirmation = (
        "[High-impact voice ambiguity detected: "
        f"{labels}. Before writing or correcting any journal entry, attributing data "
        "to another person, changing targets/totals, or giving nutrition or medical "
        "advice based on this interpretation, ask one short confirmation question "
        "that quotes only the uncertain phrase (for example: «Правильно распознал: "
        "„три пива“?»). Do not call a mutating tool and do not give the interpretation-"
        "dependent advice until the user confirms it in a later message. If a person "
        "is mentioned, confirm who they are and whose journal is meant; never create "
        "or infer a participant. If the user corrects the transcript, use the "
        "correction and discard the risky interpretation.]"
    )
    return HealthVoiceGuardDecision(
        instruction=f"{base}\n{confirmation}",
        requires_confirmation=True,
        categories=categories,
        confirmation_question=_confirmation_question(transcript, categories),
    )
