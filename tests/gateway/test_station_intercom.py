import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
import pytest
from gateway import station_intercom as m

@pytest.fixture
def cfg(tmp_path):
    return {'enabled':True,'database':str(tmp_path/'intercom.db'),'family_chat_id':'-100123','default_station':'kitchen','sender_names':{'179555559':'Михаил','367599252':'Наталья'},'stations':{'kitchen':{'name':'Кухня','event_station':'mini2','sensor':'sensor.test','aliases':['на кухне']},'bedroom':{'name':'Детская','event_station':'station2','sensor':'sensor.test2','aliases':['в детской']}}}

def event(text='спроси Алису: когда обед?',chat='-100123',thread='359',message='12',reply=None):
    return N(source=N(platform='telegram',chat_id=chat,thread_id=thread,user_id='179555559',user_name='Mikhail'),text=text,message_id=message,reply_to_message_id=reply)

def run(coro):return asyncio.run(coro)

def test_other_chats_cannot_send(cfg):
    speak=AsyncMock(return_value=True)
    assert run(m.handle_telegram(event(chat='-999'),cfg,speak)) is None
    speak.assert_not_called()

@pytest.mark.parametrize('prefix',['спроси Алису:','спроси его на Алисе','спроси через Алису,','Трипио, спроси у Алисы','@Tripioobot спроси Алису:'])
def test_question_answer_exact_thread_and_followup(cfg,prefix):
    speak=AsyncMock(return_value=True)
    assert 'открыла микрофон' in run(m.handle_telegram(event(prefix+' когда обед?'),cfg,speak))
    send=AsyncMock(return_value='77')
    assert run(m.handle_voice('kitchen','Через полчаса','v1',cfg=cfg,send=send))[0]=='Ответ отправлен в чат.'
    row=send.call_args.args[1]
    assert (row['chat'],row['thread'],row['anchor'],row['question'])==('-100123','359','12','когда обед?')
    assert send.call_args.args[2]=='Через полчаса'
    assert 'открыла микрофон' in run(m.handle_telegram(event('А что приготовить?',message='13',reply='77'),cfg,speak))
    assert speak.call_args.args[1]['question']=='А что приготовить?'

def test_dedup_and_busy(cfg):
    speak=AsyncMock(return_value=True)
    run(m.handle_telegram(event(),cfg,speak))
    assert 'уже принят' in run(m.handle_telegram(event(),cfg,speak))
    assert 'уже ждёт' in run(m.handle_telegram(event(message='13'),cfg,speak))
    assert speak.await_count==1

def test_wrong_station_and_ordinary_dialogue(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    assert run(m.handle_voice('bedroom','Да','v2',cfg=cfg)) is None
    store=m.Store(cfg['database']);row=store.pending('kitchen');store.update(row['id'],auto_until=0)
    assert run(m.handle_voice('kitchen','Какая погода?','v3',cfg=cfg)) is None

def test_late_bare_answer_arms_next_reply(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    store=m.Store(cfg['database']);store.update(store.pending('kitchen')['id'],auto_until=0)
    send=AsyncMock(return_value='77')
    assert run(m.handle_voice('kitchen','ответь','v1',cfg=cfg,send=send))==('Что передать в чат?',True)
    assert run(m.handle_voice('kitchen','Я скоро буду','v2',cfg=cfg,send=send))[1] is False
    assert send.call_args.args[2]=='Я скоро буду'

def test_uncertain_send_persisted_and_not_repeated(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    send=AsyncMock(side_effect=TimeoutError)
    assert 'не удалось' in run(m.handle_voice('kitchen','ответь: буду в пять','v1',cfg=cfg,send=send))[0].lower()
    assert 'не отправляю' in run(m.handle_voice('kitchen','ответь: буду в пять','v2',cfg=cfg,send=send))[0]
    assert 'не отправляю' in run(m.handle_voice('kitchen','ответь: буду в пять','v1',cfg=cfg,send=send))[0]
    assert send.await_count==1
    assert m.Store(cfg['database']).pending('kitchen')['answer']=='буду в пять'

def test_classifier_has_no_effect_and_returns_route(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    send=AsyncMock(return_value='77')
    assert run(m.handle_voice('kitchen','Да','v1',classifier=True,cfg=cfg,send=send))[0]=='{"route":"llm"}'
    send.assert_not_called()
    assert m.Store(cfg['database']).pending('kitchen')['state']=='waiting'

def test_cancel_and_expiry(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    assert 'закрыта' in run(m.handle_voice('kitchen','ответь отмена','v1',cfg=cfg))[0]
    assert m.Store(cfg['database']).pending('kitchen') is None
    store=m.Store(cfg['database']);row,fresh=store.reserve('kitchen','-100123','359','19','Тест',ttl=-1)
    assert store.pending('kitchen') is None

def test_uncertain_announcement_accepts_only_explicit_answer(cfg):
    speak=AsyncMock(side_effect=TimeoutError)
    assert 'Не удалось подтвердить' in run(m.handle_telegram(event(),cfg,speak))
    assert run(m.handle_voice('kitchen','обычная речь','v1',cfg=cfg)) is None
    send=AsyncMock(return_value='77')
    assert 'отправлен' in run(m.handle_voice('kitchen','ответь: да','v2',cfg=cfg,send=send))[0]

def test_concurrent_answers_send_once(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    send=AsyncMock(return_value='77')
    async def both():
        return await asyncio.gather(*(m.handle_voice('kitchen','ответь: да',f'v{i}',cfg=cfg,send=send) for i in range(2)))
    run(both());assert send.await_count==1

def test_target_and_topic_isolation(cfg):
    speak=AsyncMock(return_value=True)
    run(m.handle_telegram(event('спроси Алису в детской: Ты дома?'),cfg,speak))
    assert speak.call_args.args[1]['station']=='bedroom'
    run(m.handle_voice('bedroom','Да','v1',cfg=cfg,send=AsyncMock(return_value='77')))
    assert run(m.handle_telegram(event('Продолжение',thread='100',reply='77'),cfg,speak)) is None


def test_successful_voice_replay_does_not_send_twice(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(return_value=True)))
    send=AsyncMock(return_value='77')
    run(m.handle_voice('kitchen','да','v1',cfg=cfg,send=send))
    assert run(m.handle_voice('kitchen','да','v1',cfg=cfg,send=send))[0]=='Ответ отправлен в чат.'
    assert send.await_count==1

def test_ha_acknowledgement_and_single_post(cfg,monkeypatch):
    cfg['ha_environment']='unused'
    row,_=m.Store(cfg['database']).reserve('kitchen','-100123','359','12','Ты дома?',sender_id='179555559',sender_name='Михаил')
    request=AsyncMock(side_effect=[{'attributes':{'active_request_id':'','queue_size':0}}, {}, {'attributes':{'last_request_id':'intercom-'+row['id'],'last_success':True,'awaiting_reply':True}}])
    monkeypatch.setattr(m,'ha_request',request)
    assert run(m.ask(cfg,row)) is True
    payload=request.call_args_list[1].args[3]
    assert payload['await_reply'] is True and payload['station']=='mini2'
    assert [call.args[1] for call in request.call_args_list]==['GET','POST','GET']

def test_ha_failure_is_not_replayed(cfg,monkeypatch):
    request=AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(m,'ha_request',request)
    run(m.handle_telegram(event(),cfg))
    run(m.handle_telegram(event(),cfg))
    assert request.await_count==1


def test_late_bare_answer_after_uncertain_speech(cfg):
    run(m.handle_telegram(event(),cfg,AsyncMock(side_effect=TimeoutError)))
    assert run(m.handle_voice('kitchen','ответь','v1',cfg=cfg))[1]
    send=AsyncMock(return_value='77')
    run(m.handle_voice('kitchen','Буду дома','v2',cfg=cfg,send=send))
    assert send.await_count==1


def test_busy_station_has_no_speech_effect(cfg,monkeypatch):
    request=AsyncMock(return_value={'attributes':{'active_request_id':'other','queue_size':0}})
    monkeypatch.setattr(m,'ha_request',request)
    assert 'не отправлен' in run(m.handle_telegram(event(),cfg))
    assert [c.args[1] for c in request.call_args_list]==['GET']
    assert m.Store(cfg['database']).pending('kitchen') is None

@pytest.mark.asyncio
async def test_production_station_dispatch_intercepts_before_models_and_memory(cfg,monkeypatch):
    import ast,contextvars
    from pathlib import Path
    path=Path('/home/hermes/.hermes/quasar-bridge/src/hermes_adapter/backend.py')
    tree=ast.parse(path.read_text())
    method=next(m for c in tree.body if isinstance(c,ast.ClassDef) for m in c.body if isinstance(m,ast.AsyncFunctionDef) and m.name=='_run_agent')
    cls=ast.ClassDef(name='API',bases=[],keywords=[],body=[method],decorator_list=[])
    expectation=contextvars.ContextVar('reply',default=False)
    ns={'station_context':contextvars.ContextVar('station',default=N(station_id='kitchen')),'request_context':contextvars.ContextVar('request',default='voice-1'),'ROUTER_PROMPT':'classifier','playback_scope':N(expect_reply=expectation)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(path),'exec'),ns)
    monkeypatch.setattr(m,'settings',lambda:cfg)
    await m.handle_telegram(event(),cfg,AsyncMock(return_value=True))
    api=ns['API']()
    assert (await api._run_agent('ответь',[],ephemeral_system_prompt='classifier'))[0]['final_response']=='{"route":"llm"}'
    assert (await api._run_agent('ответь',[]))[0]['final_response']=='Что передать в чат?'
    assert expectation.get() is True

@pytest.mark.asyncio
async def test_production_gateway_dispatch_stays_before_general_agent(cfg,monkeypatch):
    import ast,logging
    from pathlib import Path
    from unittest.mock import patch
    path=Path(__file__).resolve().parents[2]/'gateway/run.py';tree=ast.parse(path.read_text())
    method=next(m for c in tree.body if isinstance(c,ast.ClassDef) for m in c.body if isinstance(m,ast.AsyncFunctionDef) and m.name=='_handle_message')
    cls=ast.ClassDef(name='Gateway',bases=[],keywords=[],body=[method],decorator_list=[])
    ns={'asyncio':asyncio,'logger':logging.getLogger('fixture'),'MessageEvent':object,'Optional':__import__('typing').Optional}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(path),'exec'),ns)
    gateway=ns['Gateway']();gateway.session_store=None;gateway._is_user_authorized=lambda source:True
    incoming=event();incoming.source.user_id='123';incoming.is_command=lambda:False
    with patch('hermes_cli.plugins.invoke_hook',return_value=[]),patch.object(m,'handle_telegram',new=AsyncMock(return_value='Вопрос принят')) as route:
        assert await gateway._handle_message(incoming)=='Вопрос принят'
    route.assert_awaited_once_with(incoming)


def test_authenticated_sender_is_quoted_and_persisted(cfg):
    speak=AsyncMock(return_value=True)
    run(m.handle_telegram(event('спроси Алису: Наталья дома?'),cfg,speak))
    row=speak.call_args.args[1]
    assert (row['sender_id'],row['sender_name'])==('179555559','Михаил')
    assert m.spoken_question(row)=='Михаил спрашивает: «Наталья дома?»'
    assert m.Store(cfg['database']).pending('kitchen')['sender_name']=='Михаил'

def test_shared_session_uses_real_telegram_sender_and_followup_author(cfg):
    speak=AsyncMock(return_value=True)
    run(m.handle_telegram(event('спроси Алису в детской: Ты дома?'),cfg,speak))
    run(m.handle_voice('bedroom','Да','v1',cfg=cfg,send=AsyncMock(return_value='77')))
    incoming=event('спроси Алису: А когда ужин?',message='13',reply='77')
    incoming.source.user_id=None;incoming.source.user_name=None
    incoming.raw_message=N(from_user=N(id=367599252,first_name='Наталия'),text=incoming.text)
    run(m.handle_telegram(incoming,cfg,speak))
    row=speak.call_args.args[1]
    assert row['station']=='bedroom'
    assert m.spoken_question(row)=='Наталья уточняет: «А когда ужин?»'
    assert row['sender_id']=='367599252'

def test_unmapped_member_name_comes_from_sender_not_quoted_text(cfg):
    incoming=event('спроси Алису: Михаил спрашивает где ключи?')
    incoming.raw_message=N(from_user=N(id=123456,first_name='Елена'),text=incoming.text)
    speak=AsyncMock(return_value=True)
    run(m.handle_telegram(incoming,cfg,speak))
    assert m.spoken_question(speak.call_args.args[1])=='Елена спрашивает: «Михаил спрашивает где ключи?»'

@pytest.mark.parametrize('text',['ответь','ответь: привет','Алиса, ответь: привет','передай ответ: привет','Привет'])
def test_voice_cannot_initiate_telegram_dialogue(cfg,text):
    send=AsyncMock(return_value='77')
    result=run(m.handle_voice('kitchen',text,'voice-first',cfg=cfg,send=send))
    send.assert_not_called()
    with m.Store(cfg['database']).db() as db:assert db.execute('SELECT COUNT(*) FROM intercom').fetchone()[0]==0
    if m.ANSWER.match(text):assert result==('Сейчас нет вопроса из Telegram.',False)
    else:assert result is None

@pytest.mark.parametrize('anonymous',['missing','channel','bot'])
def test_unknown_author_never_speaks_under_invented_name(cfg,anonymous):
    incoming=event();incoming.source.user_id=None;incoming.source.user_name=None
    if anonymous=='channel':incoming.raw_message=N(sender_chat=N(id=-1001),from_user=N(id=777,first_name='Family'))
    if anonymous=='bot':incoming.raw_message=N(from_user=N(id=777,first_name='Bot',is_bot=True))
    speak=AsyncMock(return_value=True)
    assert 'Не удалось определить автора' in run(m.handle_telegram(incoming,cfg,speak))
    speak.assert_not_called()

def test_legacy_database_migrates_without_losing_question(cfg):
    import sqlite3
    with sqlite3.connect(cfg['database']) as db:
        db.execute("CREATE TABLE intercom (id TEXT PRIMARY KEY, station TEXT, chat TEXT, thread TEXT, anchor TEXT, question TEXT, created REAL, expires REAL, auto_until REAL, state TEXT, answer TEXT, voice_request TEXT UNIQUE, telegram_id TEXT)")
        db.execute("INSERT INTO intercom VALUES ('old','kitchen','-100123','359','12','Старый вопрос',0,9999999999,0,'waiting',NULL,NULL,NULL)")
    store=m.Store(cfg['database'])
    assert store.pending('kitchen')['question']=='Старый вопрос'
    assert store.pending('kitchen')['sender_name']==''
    with pytest.raises(ValueError):m.spoken_question(store.pending('kitchen'))

def test_reply_label_does_not_claim_identity_of_person_near_station(cfg,monkeypatch):
    import httpx,dotenv
    from unittest.mock import Mock
    cfg['telegram_environment']='isolated.env'
    monkeypatch.setattr(dotenv,'dotenv_values',lambda path:{'TELEGRAM_BOT_TOKEN':'test-only'})
    response=Mock();response.json.return_value={'ok':True,'result':{'message_id':77}}
    client=AsyncMock();client.post.return_value=response;client.__aenter__.return_value=client
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client)
    row={'chat':'-100123','thread':'359','anchor':'12','station':'kitchen','sender_name':'Михаил'}
    assert run(m.send_answer(cfg,row,'Буду к восьми'))=='77'
    payload=client.post.call_args.kwargs['json']
    assert payload['text']=='Алиса ответила (Кухня):\nБуду к восьми'
    assert 'Михаил' not in payload['text']
    assert payload['reply_parameters']['message_id']==12


def test_real_family_observation_wrapper_does_not_hide_intercom_intent(cfg):
    text='@TripiooBot спроси на Алисе, а хлеб мы случайно не забыли купить? Может купить свежий хлеб в магните?'
    incoming=event(text)
    incoming.source.user_id=None;incoming.source.user_name=None
    incoming.authenticated_user_request_text='[Mikhail|179555559]\n'+text
    incoming.text=incoming.authenticated_user_request_text
    incoming.raw_message=N(from_user=N(id=179555559,first_name='Mikhail'),text=text)
    speak=AsyncMock(return_value=True)
    assert 'открыла микрофон' in run(m.handle_telegram(incoming,cfg,speak))
    assert m.spoken_question(speak.call_args.args[1])=='Михаил спрашивает: «а хлеб мы случайно не забыли купить? Может купить свежий хлеб в магните?»'

def test_voice_transcript_wrapper_matches_authenticated_sender(cfg):
    incoming=event('[Mikhail|179555559]\n@TripiooBot спроси на Алисе, когда обед?')
    incoming.source.user_id=None
    incoming.raw_message=N(from_user=N(id=179555559,first_name='Mikhail'),text=None)
    speak=AsyncMock(return_value=True)
    assert 'открыла микрофон' in run(m.handle_telegram(incoming,cfg,speak))
    assert speak.call_args.args[1]['question']=='когда обед?'

def test_unrelated_attribution_header_is_not_treated_as_sender(cfg):
    incoming=event('[Наталья|367599252]\nспроси Алису: привет')
    speak=AsyncMock(return_value=True)
    assert run(m.handle_telegram(incoming,cfg,speak)) is None
    speak.assert_not_called()


@pytest.mark.parametrize('chat,thread,profile',[('179555559','','personal'),('-100456','42','personal'),('-100123','359','family-chat')])
def test_all_authorized_sources_return_answer_to_original_destination(cfg,chat,thread,profile):
    cfg['telegram_sources']=[{'chat_id':'-100123','profile':'family-chat'},{'chat_id':'179555559','profile':'personal','user_ids':['179555559']},{'chat_id':'-100456','profile':'personal'}]
    incoming=event('спроси Алису: когда ужин?',chat=chat,thread=thread)
    incoming.source.profile_name=profile
    speak=AsyncMock(return_value=True)
    assert 'открыла микрофон' in run(m.handle_telegram(incoming,cfg,speak))
    send=AsyncMock(return_value='77')
    assert 'отправлен' in run(m.handle_voice('kitchen','В восемь','v1',cfg=cfg,send=send))[0]
    row=send.call_args.args[1]
    assert (row['chat'],row['thread'],row['anchor'])==(chat,thread,'12')
    assert 'открыла микрофон' in run(m.handle_telegram(event('Что приготовить?',chat=chat,thread=thread,message='13',reply='77'),cfg,speak))
    assert run(m.handle_telegram(event('Чужое продолжение',chat='-100999',thread=thread,message='14',reply='77'),cfg,speak)) is None

def test_private_chat_rejects_foreign_sender(cfg):
    cfg['telegram_sources']=[{'chat_id':'179555559','profile':'personal','user_ids':['179555559']}]
    incoming=event(chat='179555559',thread='');incoming.source.profile_name='personal';incoming.source.user_id='367599252'
    speak=AsyncMock(return_value=True)
    assert run(m.handle_telegram(incoming,cfg,speak)) is None
    speak.assert_not_called()

def test_allowed_chat_does_not_allow_unrelated_profile(cfg):
    cfg['telegram_sources']=[{'chat_id':'-100456','profile':'personal'}]
    incoming=event(chat='-100456');incoming.source.profile_name='boxmap'
    speak=AsyncMock(return_value=True)
    assert run(m.handle_telegram(incoming,cfg,speak)) is None
    speak.assert_not_called()
