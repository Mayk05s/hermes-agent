"""Station lesson mode through the real station voice entry point
(gateway.station_intercom.handle_voice, as called by the station backend).
No speech, HA, Telegram or model: all transports are mocked."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway import station_intercom as intercom
from gateway import station_lesson as lesson_mod

REAL_PLUGINS = Path.home() / ".hermes" / "plugins"
FILE = "doc_fb488d5af0bd"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def home(tmp_path, monkeypatch):
    if not (REAL_PLUGINS / "study-tasks").is_dir():
        pytest.skip("study-tasks plugin not installed")
    monkeypatch.setenv("HERMES_STUDY_HOME", str(tmp_path))
    (tmp_path / "plugins").symlink_to(REAL_PLUGINS)
    docs = tmp_path / "cache" / "documents"
    docs.mkdir(parents=True)
    fitz = pytest.importorskip("fitz")
    if not os.path.exists(FONT):
        pytest.skip("no Cyrillic font for the fixture PDF")
    pdf = fitz.open()
    for text in ("Белый пояс 10 гып: что такое тхэквондо, путь кулака и ноги",
                 "Белый пояс с жёлтой полосой 9 гып: тхэквондо как вид спорта",
                 "Жёлтый пояс 8 гып: клуб Грандмастер зарегистрирован 18 апреля 2000 года в Ростове-на-Дону"):
        pdf.new_page().insert_text((40, 72), text, fontname="dejavu", fontfile=FONT)
    pdf.save(str(docs / f"{FILE}_Теория 2026.pdf"))
    store = lesson_mod._plugin("t_cd_store", "chat-documents/store.py")
    store.record({"path": str(docs / f"{FILE}_Теория 2026.pdf"), "chat_id": "-100123", "thread_id": "576"}, home=tmp_path)
    return tmp_path


@pytest.fixture
def cfg(tmp_path):
    return {"enabled": True, "database": str(tmp_path / "intercom.db"), "family_chat_id": "-100123", "default_station": "kitchen",
            "sender_names": {"179555559": "Михаил"}, "telegram_environment": "/nonexistent",
            "stations": {"kitchen": {"name": "Кухня", "event_station": "mini2", "sensor": "sensor.test", "aliases": ["на кухне"]},
                         "bedroom": {"name": "Детская", "event_station": "station2", "sensor": "sensor.test2", "aliases": ["в детской"]}}}


@pytest.fixture
def task(home):
    st = lesson_mod.study_store()
    t, _ = st.create(profile="family-chat", file={"file_id": FILE, "name": "Теория 2026.pdf", "chat_id": "-100123", "thread_id": "576"},
                     fragment="8 гып (жёлтый пояс)", pages="3", learner="Артём", created_by="179555559")
    return t


class FakeModel:
    def __init__(self):
        self.calls = []
        self.queue = []

    async def __call__(self, system, user):
        self.calls.append((system, json.loads(user.split("\n", 1)[1])))
        if self.queue:
            return self.queue.pop(0)
        return json.dumps({"say": "Когда зарегистрирован клуб Грандмастер?", "question": "Когда зарегистрирован клуб Грандмастер?"})


@pytest.fixture
def fakes(monkeypatch):
    model, sent = FakeModel(), []

    async def send(cfg, chat, thread, text):
        sent.append((chat, thread, text))
        return 1

    monkeypatch.setattr(lesson_mod, "default_infer", model)
    monkeypatch.setattr(lesson_mod, "tg_send", send)
    return model, sent


def voice(cfg, text, request, station="bedroom", classifier=False):
    return run(intercom.handle_voice(station, text, request, classifier=classifier, cfg=cfg))


def test_full_lesson_on_station_path(home, cfg, task, fakes):
    model, sent = fakes
    speak = AsyncMock(return_value=True)
    started = run(lesson_mod.start("детская", origin={"chat_id": "-100123", "thread_id": "576"}, cfg=cfg, speak=speak))
    assert started["ok"] and started["station"] == "Детская"
    assert speak.await_args.args[1] == "bedroom" and "8 гып" in speak.await_args.args[3]
    # material snapshot = page 3 only, from the task's file
    material = lesson_mod.load("bedroom")["material"]
    assert "Грандмастер" in material and "путь кулака" not in material

    assert voice(cfg, "да", "r0", classifier=True) == ('{"route":"llm"}', False)
    say, more = voice(cfg, "Да, готов", "r1")
    assert more and "Грандмастер" in say
    system, data = model.calls[-1]
    assert "2000 года" in system and data["реплика_ученика"] == "Да, готов"

    for repeat in ("Алиса, повтори", "Алис", "повтори ещё раз то что было предыдущее"):
        say, more = voice(cfg, repeat, "rep-" + repeat)
        assert more and say == "Повторяю. Когда зарегистрирован клуб Грандмастер?"
    assert len(model.calls) == 1  # repeats never reach the model

    model.queue.append(json.dumps({"say": "Верно! А в каком городе?", "question": "В каком городе зарегистрирован клуб?",
                                   "mastered": ["дата регистрации клуба"], "confused": [], "position": "город клуба"}))
    say, more = voice(cfg, "восемнадцатого апреля двухтысячного", "r2")
    assert more and say.startswith("Верно")
    assert voice(cfg, "восемнадцатого апреля двухтысячного", "r2") == (say, True)  # duplicate delivery
    prog = lesson_mod.study_store().get("family-chat", task["id"])["progress"]
    assert prog["mastered"] == ["дата регистрации клуба"] and prog["position"] == "город клуба"

    model.queue.append(json.dumps({"say": "Не совсем. Клуб из Ростова-на-Дону. Повтори: в каком городе?", "question": "В каком городе?",
                                   "confused": ["город клуба"]}))
    say, more = voice(cfg, "в Москве", "r3")
    assert more and "Ростова" in say

    say, more = voice(cfg, "Хватит", "r4")
    assert not more and "заканчиваем" in say
    assert lesson_mod.load("bedroom")["state"] == "ended"
    chat, thread, summary = sent[-1]
    assert (chat, thread) == ("-100123", "576")
    assert "Пересказал сам: дата регистрации клуба" in summary and "Пока не может пересказать: город клуба" in summary
    prog = lesson_mod.study_store().get("family-chat", task["id"])["progress"]
    assert prog["sessions"][-1]["note"].startswith("Занятие на колонке «Детская»")
    # after the lesson ordinary dialogue is not intercepted
    assert voice(cfg, "какая погода", "r5") is None


def test_lesson_does_not_touch_other_station(home, cfg, task, fakes):
    run(lesson_mod.start("в детской", origin={"chat_id": "-100123"}, cfg=cfg, speak=AsyncMock(return_value=True)))
    assert voice(cfg, "повтори", "k1", station="kitchen") is None
    assert voice(cfg, "повтори", "k2", station="unknown-station") is None


def test_silence_timeout_ends_lesson(home, cfg, task, fakes, monkeypatch):
    _, sent = fakes
    run(lesson_mod.start("детская", origin={"chat_id": "-100123", "thread_id": "576"}, cfg=cfg, speak=AsyncMock(return_value=True)))
    state = lesson_mod.load("bedroom")
    state["last_activity"] -= lesson_mod.SILENCE_TIMEOUT + 5
    lesson_mod.save(state)
    assert voice(cfg, "какая погода", "s1") is None
    assert lesson_mod.load("bedroom")["state"] == "ended" and "тишина" in sent[-1][2]


def test_start_refused_when_intercom_waiting_or_busy(home, cfg, task, fakes):
    speak = AsyncMock(return_value=True)
    ev = type("E", (), {})()
    from types import SimpleNamespace as N
    ev = N(source=N(platform="telegram", chat_id="-100123", thread_id="359", user_id="179555559", user_name="M"),
           text="спроси Алису в детской: ты где?", message_id="12", reply_to_message_id=None)
    run(intercom.handle_telegram(ev, cfg, speak))
    out = run(lesson_mod.start("детская", origin={"chat_id": "-100123"}, cfg=cfg, speak=speak))
    assert not out["ok"] and "вопрос из чата" in out["message"]
    busy = AsyncMock(side_effect=intercom.StationBusy())
    out = run(lesson_mod.start("кухня", origin={"chat_id": "-100123"}, cfg=cfg, speak=busy))
    assert not out["ok"] and lesson_mod.active_lesson("kitchen") is None


def test_voice_start_uses_active_task(home, cfg, task, fakes):
    model, _ = fakes
    say, more = voice(cfg, "Алиса, давай позанимаемся", "v1")
    assert more and "Грандмастер" in say
    assert lesson_mod.active_lesson("bedroom")["task_id"] == task["id"]


def test_no_task_no_lesson(home, cfg, fakes):
    out = run(lesson_mod.start("детская", origin={}, cfg=cfg, speak=AsyncMock(return_value=True)))
    assert not out["ok"] and "нет" in out["message"]
    assert voice(cfg, "повтори", "n1") is None


# ---------------------------------------------------------------------------
# Intercom: bare wake word / "повтори" is not an answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filler", ["Алис", "Алиса", "Алиса, повтори", "повтори", "Что?"])
def test_intercom_filler_repeats_question_and_keeps_waiting(home, cfg, filler):
    from types import SimpleNamespace as N
    ev = N(source=N(platform="telegram", chat_id="-100123", thread_id="576", user_id="179555559", user_name="M"),
           text="спроси Алису в детской: что выучил?", message_id="20", reply_to_message_id=None)
    run(intercom.handle_telegram(ev, cfg, AsyncMock(return_value=True)))
    send = AsyncMock(return_value="88")
    text, more = run(intercom.handle_voice("bedroom", filler, "f1", cfg=cfg, send=send))
    assert more and text == "Повторяю вопрос. Михаил спрашивает: «что выучил?»"
    send.assert_not_called()
    assert run(intercom.handle_voice("bedroom", "Про жёлтый пояс", "f2", cfg=cfg, send=send))[0] == "Ответ отправлен в чат."
    assert send.await_args.args[2] == "Про жёлтый пояс"


@pytest.mark.parametrize("text,repeat", [("повторим", False), ("что это", False), ("всё понятно", False), ("Алиса, повтори вопрос", True), ("а?", True)])
def test_repeat_and_stop_patterns(text, repeat):
    assert lesson_mod.is_repeat(text) is repeat
    assert not lesson_mod.is_stop("все пояса разные")
    assert lesson_mod.is_stop("Алиса, хватит")


@pytest.mark.parametrize("learner,expect", [("ребёнок", "Давай позанимаемся"), ("", "Давай позанимаемся"), ("Артём", "Артём, давай позанимаемся")])
def test_opening_addresses_only_real_name(learner, expect):
    text = lesson_mod.opening_text(learner, "8 гып")
    assert text.startswith(expect) and "Ребёнок" not in text and "Трипио" not in text
    assert "расскажу" in text


def test_unclear_asr_is_not_graded(home, cfg, task, fakes):
    model, _ = fakes
    run(lesson_mod.start("детская", origin={"chat_id": "-100123"}, cfg=cfg, speak=AsyncMock(return_value=True)))
    voice(cfg, "да", "u0")
    calls = len(model.calls)
    for junk in ("Гптг", "Это", "Алиса, это"):
        say, more = voice(cfg, junk, "u-" + junk)
        assert more and say == "Не расслышал. Когда зарегистрирован клуб Грандмастер?"
    assert len(model.calls) == calls
    for real in ("1", "да", "жёлтый"):
        assert not lesson_mod.is_unclear(real)


def test_prompt_teaches_before_asking(home, cfg, task, fakes):
    model, _ = fakes
    run(lesson_mod.start("детская", origin={"chat_id": "-100123"}, cfg=cfg, speak=AsyncMock(return_value=True)))
    voice(cfg, "да", "t0")
    system = model.calls[-1][0]
    assert "рассказать" in system and "ПО-ДРУГОМУ" in system and "бессмысленная" in system
    assert "не викторина" in system.lower() and "мелкие числа" in system


# ---------------------------------------------------------------------------
# Silence: empty transcripts never reach handle_voice, the watcher reprompts
# ---------------------------------------------------------------------------

def _started(cfg):
    speak = AsyncMock(return_value=True)
    assert run(lesson_mod.start("детская", origin={"chat_id": "-100123", "thread_id": "576"}, cfg=cfg, speak=speak))["ok"]
    return speak


def test_silence_reprompts_then_pauses_and_notifies(home, cfg, task, fakes):
    model, sent = fakes
    _started(cfg)
    import time as _t
    speak = AsyncMock(return_value=True)
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + 5, speak=speak)) == []  # still within the listening window
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + lesson_mod.REPROMPT_AFTER + 1, speak=speak)) == [("bedroom", "reprompted")]
    first = speak.await_args.args[3]
    assert first.startswith("Повторю. Артём, давай позанимаемся") and speak.await_args.args[1] == "bedroom"
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + lesson_mod.REPROMPT_AFTER + 1, speak=speak)) == [("bedroom", "reprompted")]
    assert speak.await_args.args[3].startswith("Ты здесь?")
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + lesson_mod.REPROMPT_AFTER + 1, speak=speak)) == [("bedroom", "paused")]
    assert speak.await_count == 2 and len(model.calls) == 0  # no model calls for reprompts
    assert lesson_mod.load("bedroom")["state"] == "paused"
    chat, thread, note = sent[-1]
    assert (chat, thread) == ("-100123", "576") and "Нет реакции на колонке «Детская»" in note and "пауз" in note
    # paused: ordinary speech is not intercepted, the watcher stays quiet
    assert voice(cfg, "какая погода", "p1") is None
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + 100, speak=speak)) == []
    # voice resume
    assert voice(cfg, "давай дальше", "p2", classifier=True) == ('{"route":"llm"}', False)
    say, more = voice(cfg, "Алиса, давай дальше", "p3")
    assert more and say.startswith("Продолжаем. Артём, давай позанимаемся")
    state = lesson_mod.load("bedroom")
    assert state["state"] == "active" and state["silent_count"] == 0


def test_reply_resets_silence_and_turn_in_progress_is_not_reprompted(home, cfg, task, fakes):
    _started(cfg)
    import time as _t
    speak = AsyncMock(return_value=True)
    run(lesson_mod.watch_tick(cfg, now=_t.time() + lesson_mod.REPROMPT_AFTER + 1, speak=speak))
    assert lesson_mod.load("bedroom")["silent_count"] == 1
    voice(cfg, "да", "a0", classifier=True)  # ASR arrived, model still working
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + lesson_mod.REPROMPT_AFTER + 1, speak=speak)) == []
    voice(cfg, "да", "a1")
    state = lesson_mod.load("bedroom")
    assert state["silent_count"] == 0 and state["listen_from"] > _t.time()  # waits for the reply to be spoken


def test_busy_station_does_not_count_as_reprompt(home, cfg, task, fakes):
    _started(cfg)
    import time as _t
    busy = AsyncMock(side_effect=intercom.StationBusy())
    assert run(lesson_mod.watch_tick(cfg, now=_t.time() + lesson_mod.REPROMPT_AFTER + 1, speak=busy)) == [("bedroom", "busy")]
    assert lesson_mod.load("bedroom").get("silent_count", 0) == 0


def test_telegram_resume_and_long_pause_ends(home, cfg, task, fakes):
    _, sent = fakes
    _started(cfg)
    state = lesson_mod.load("bedroom")
    state.update(state="paused", paused_at=state["started_at"])
    lesson_mod.save(state)
    speak = AsyncMock(return_value=True)
    out = run(lesson_mod.start("детская", origin={"chat_id": "-100123"}, cfg=cfg, speak=speak))
    assert out["ok"] and "продолжено" in out["message"] and speak.await_args.args[3].startswith("Продолжаем.")
    assert lesson_mod.load("bedroom")["state"] == "active"
    state = lesson_mod.load("bedroom")
    state.update(state="paused", paused_at=state["started_at"] - lesson_mod.PAUSE_TIMEOUT - 10)
    lesson_mod.save(state)
    import time as _t
    assert run(lesson_mod.watch_tick(cfg, now=_t.time(), speak=speak)) == [("bedroom", "ended")]
    assert "пауза дольше 3 часов" in sent[-1][2]


def test_first_turn_is_start_and_block_attempts_counted(home, cfg, task, fakes):
    model, _ = fakes
    _started(cfg)
    model.queue += [json.dumps({"say": "Клуб есть в Ростове. Расскажи, где клуб?", "question": "Где клуб?", "block": "Клуб"}),
                    json.dumps({"say": "Скажу иначе. Расскажи про город.", "question": "Где клуб?", "block": "Клуб"}),
                    json.dumps({"say": "Ещё проще. Клуб в Ростове. Где он?", "question": "Где клуб?", "block": "клуб"}),
                    json.dumps({"say": "Молодец! Дальше.", "question": "Кто основал?", "block": "Основатель", "mastered": ["Клуб"]})]
    voice(cfg, "Жёлтый пояс", "b1")
    assert model.calls[-1][1]["первый_ход"] is True
    voice(cfg, "Белый", "b2")
    assert model.calls[-1][1]["первый_ход"] is False and model.calls[-1][1]["попыток_по_блоку"] == 0
    voice(cfg, "не знаю", "b3")
    assert model.calls[-1][1]["попыток_по_блоку"] == 1
    voice(cfg, "в Ростове", "b4")
    assert model.calls[-1][1]["попыток_по_блоку"] == 2
    state = lesson_mod.load("bedroom")
    assert state["block"] == "Основатель" and state["block_attempts"] == 0


@pytest.mark.parametrize("text", ["Белый", "Гып алиса гып", "жёлтый", "Присматривают спортсменам"])
def test_imprecise_on_topic_answers_reach_the_model(text):
    assert not lesson_mod.is_unclear(lesson_mod.strip_wake(text))


def test_prompt_child_language_rules():
    system, _ = lesson_mod._prompt({"learner": "", "fragment": "x", "material": "m", "turns": []}, "да", {})
    for phrase in ("ПЕРВЫЙ ХОД", "1–2 коротких предложения", "две вещи сразу", "ПО-ДРУГОМУ", "это попытка"):
        assert phrase in system


def test_turn_always_ends_with_a_request():
    assert lesson_mod.with_request("Клуб в Ростове.", "Где клуб?") == "Клуб в Ростове. Где клуб?"
    assert lesson_mod.with_request("Где клуб?", "Где клуб?") == "Где клуб?"
    assert lesson_mod.with_request("Расскажи, где клуб?", "Расскажи про город") == "Расскажи, где клуб?"
    assert lesson_mod.with_request("Молодец, всё!", "") == "Молодец, всё!"
