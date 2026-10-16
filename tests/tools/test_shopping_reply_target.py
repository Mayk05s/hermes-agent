import json
from types import SimpleNamespace as NS
from unittest.mock import patch
from contextlib import contextmanager
import pytest
from gateway.session_context import set_session_vars,clear_session_vars,override_session_env
from tools.mcp_tool import _bind_shopping_reply_target

@contextmanager
def origin(receipt):
    tokens=set_session_vars(platform='telegram',chat_id='-100',thread_id='7',requester_user_id='42')
    try:
        with override_session_env('HERMES_SESSION_SHOPPING_RECEIPT',json.dumps(receipt)):yield
    finally:clear_session_vars(tokens)

@pytest.mark.parametrize('action,key',[('add-shopping-items','liveListId'),('list-shopping-items','liveListId'),('move-shopping-items','targetLiveListId')])
def test_reply_resolver_controls_exact_tool_target(monkeypatch,action,key):
    import urllib.request
    monkeypatch.setenv('HEALTH_ACTIONS_API_URL','https://example.test/api/admin/health-actions');monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','test')
    receipt={'context':{'chatId':'-100','threadId':'7','actorId':'42','replyMessageId':'9'},'signature':'sealed'}
    target='11111111-1111-4111-8111-111111111111'
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'ok':True,'liveListId':target,'listName':'Основной'}).encode()
    def lookup(req,timeout):
        assert req.full_url=='https://example.test/api/admin/shopping-message-target'
        assert json.loads(req.data)=={'identityReceipt':receipt}
        return Response()
    with origin(receipt),patch.object(urllib.request,'urlopen',side_effect=lookup):
        args={'telegramUserId':'42'}
        assert _bind_shopping_reply_target('health-actions',action,args) is None
        assert args[key]==target
        assert _bind_shopping_reply_target('health-actions',action,{'telegramUserId':'other'}) is not None
        assert _bind_shopping_reply_target('health-actions',action,{'telegramUserId':'42',key:'22222222-2222-4222-8222-222222222222'}) is not None

def test_no_reply_never_uses_model_supplied_card_or_calls_network():
    import urllib.request
    with origin({}),patch.object(urllib.request,'urlopen') as call:
        assert _bind_shopping_reply_target('health-actions','add-shopping-items',{'replyMessageId':'999','telegramUserId':'42'}) is None
        call.assert_not_called()
