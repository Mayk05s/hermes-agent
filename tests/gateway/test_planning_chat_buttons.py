import json
import pytest
from gateway.miniapp_reply_contract import MiniappReplyCollector, miniapp_reply_params
from tests.gateway.test_shopping_inline_button_contract import _adapter, _Button, _Markup

@pytest.mark.parametrize('action,param', [('list-planning-tasks','planning_c4534774626'),('create-planning-task','planning_c4534774626'),('list-menu-entries','menu_c4534774626'),('add-menu-entries','dishes_c4534774626'),('list-shopping-items','shopping_buy_c4534774626')])
def test_executed_scope_destination_survives_collector(action,param):
    collector=MiniappReplyCollector()
    collector.record('mcp_health_actions_'+action.replace('-','_'),json.dumps({'ok':True,'action':action,'_miniapp':{'start_param':param}}))
    assert miniapp_reply_params(collector.snapshot())==[param]

@pytest.mark.parametrize('param', ['planning_c4534774626','menu_c4534774626','dishes_c4534774626','shopping_buy_c4534774626'])
@pytest.mark.asyncio
async def test_telegram_button_preserves_exact_chat_url(monkeypatch,param):
    from gateway.platforms import telegram
    monkeypatch.setattr(telegram,'InlineKeyboardButton',_Button)
    monkeypatch.setattr(telegram,'InlineKeyboardMarkup',_Markup)
    calls=[];adapter=_adapter(calls)
    result=await adapter.send('-4534774626','Открыть список',metadata={'miniapp_start_params':[param]})
    assert result.success
    assert calls[-1]['reply_markup'].inline_keyboard[0][0].url.endswith('startapp='+param)

def test_chat_suffix_never_authorizes_a_different_section():
    collector=MiniappReplyCollector()
    collector.record('mcp_health_actions_list_planning_tasks',json.dumps({'ok':True,'action':'list-planning-tasks','_miniapp':{'start_param':'menu_c4534774626'}}))
    assert miniapp_reply_params(collector.snapshot())==['planning']
