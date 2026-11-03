import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from gateway.station_engineering_reply import forward_reply, EngineeringReplyError

@pytest.fixture
def fixture(tmp_path):
    database=tmp_path/'jobs.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE repair_events(job TEXT,kind TEXT,data TEXT)')
        db.execute('INSERT INTO repair_events VALUES(?,?,?)',('job-1','telegram_notice',json.dumps({'message_id':42,'chat_id':-100,'thread_id':2})))
    config=tmp_path/'engineering-config.json';config.write_text(json.dumps({'database':str(database),'telegram':{'chat_id':-100,'thread_id':2}}))
    source=SimpleNamespace(platform=SimpleNamespace(value='telegram'),chat_id='-100',thread_id='2',user_id='owner')
    event=SimpleNamespace(source=source,reply_to_message_id='42',text='decorated text',authenticated_user_request_text='Свет не выключился; повтори один раз',raw_message=None)
    return config,event

def test_reply_goes_to_exact_job_with_literal_text_and_no_shell(fixture):
    config,event=fixture
    with patch('gateway.station_engineering_reply.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
        assert forward_reply(event,config)
    args=run.call_args.args[0]
    assert args[args.index('--job')+1]=='job-1'
    assert args[args.index('--text')+1]==event.authenticated_user_request_text
    assert not run.call_args.kwargs.get('shell')

@pytest.mark.parametrize('field,value',[('chat_id','-101'),('thread_id','3'),('user_id',None)])
def test_other_scope_never_resumes_engineering(fixture,field,value):
    config,event=fixture;setattr(event.source,field,value)
    with patch('gateway.station_engineering_reply.subprocess.run') as run:
        assert not forward_reply(event,config)
    run.assert_not_called()

def test_unregistered_reply_text_cannot_choose_job(fixture):
    config,event=fixture;event.reply_to_message_id='777';event.text='Проверка Алисы job-1'
    with patch('gateway.station_engineering_reply.subprocess.run') as run:
        assert not forward_reply(event,config)
    run.assert_not_called()

def test_failed_forward_blocks_general_agent_fallback(fixture):
    config,event=fixture
    with patch('gateway.station_engineering_reply.subprocess.run',return_value=SimpleNamespace(returncode=1)):
        with pytest.raises(EngineeringReplyError):forward_reply(event,config)

@pytest.mark.asyncio
async def test_real_dispatch_returns_before_job_router_or_agent(fixture):
    import ast,asyncio,logging
    from pathlib import Path
    config,event=fixture;event.message_id='99';event.is_command=lambda:False
    path=Path(__file__).resolve().parents[2]/'gateway/run.py'
    tree=ast.parse(path.read_text())
    method=next(m for c in tree.body if isinstance(c,ast.ClassDef) for m in c.body if isinstance(m,ast.AsyncFunctionDef) and m.name=='_handle_message')
    cls=ast.ClassDef(name='Gateway',bases=[],keywords=[],body=[method],decorator_list=[])
    namespace={'asyncio':asyncio,'logger':logging.getLogger('fixture'),'MessageEvent':object,'Optional':__import__('typing').Optional}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(path),'exec'),namespace)
    gateway=namespace['Gateway']();gateway.session_store=None;gateway._is_user_authorized=lambda source:True
    with patch('hermes_cli.plugins.invoke_hook',return_value=[]),patch('gateway.station_engineering_reply.forward_reply',return_value=True) as route:
        assert await gateway._handle_message(event) is None
    route.assert_called_once_with(event)

def test_shared_group_source_keeps_authenticated_raw_sender(fixture):
    config,event=fixture;event.source.user_id=None;event.raw_message=SimpleNamespace(from_user=SimpleNamespace(id=123),text='Остался свет')
    with patch('gateway.station_engineering_reply.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
        assert forward_reply(event,config)
    run.assert_called_once()

@pytest.mark.asyncio
async def test_busy_session_routes_reply_without_steering_general_agent(fixture):
    import ast,asyncio,logging
    from pathlib import Path
    _,event=fixture;event.message_id='99';event.is_command=lambda:False
    path=Path(__file__).resolve().parents[2]/'gateway/run.py';tree=ast.parse(path.read_text())
    method=next(m for c in tree.body if isinstance(c,ast.ClassDef) for m in c.body if isinstance(m,ast.AsyncFunctionDef) and m.name=='_handle_active_session_busy_message')
    cls=ast.ClassDef(name='Gateway',bases=[],keywords=[],body=[method],decorator_list=[])
    ns={'asyncio':asyncio,'logger':logging.getLogger('fixture'),'MessageEvent':object}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(path),'exec'),ns)
    gateway=ns['Gateway']();gateway._is_user_authorized=lambda source:True
    with patch('gateway.station_engineering_reply.forward_reply',return_value=True) as route:
        assert await gateway._handle_active_session_busy_message(event,'shared-topic')
    route.assert_called_once_with(event)
