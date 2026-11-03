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

@pytest.mark.parametrize('action,param', [('add-menu-entries','dishes_c1003966683704'),('list-menu-entries','menu_c1003966683704'),('add-shopping-items','shopping_buy_g11111111111141118111111111111111_c1003966683704')])
def test_family_food_actions_offer_both_destinations_without_losing_exact_list(action,param):
    collector=MiniappReplyCollector()
    collector.record('mcp_health_actions_'+action.replace('-','_'),json.dumps({'ok':True,'action':action,'_miniapp':{'start_param':param}}))
    records=collector.snapshot()
    params=miniapp_reply_params(records,chat_id='-1003966683704')
    assert param in params
    assert any(p.startswith(('menu','dishes')) for p in params)
    assert any(p.startswith('shopping') for p in params)
    assert miniapp_reply_params(records,chat_id='-999999')==[param]


def test_failed_family_food_action_does_not_claim_success_or_add_navigation():
    collector=MiniappReplyCollector()
    collector.record('mcp_health_actions_add_menu_entries',json.dumps({'ok':False,'error':'unavailable'}))
    assert miniapp_reply_params(collector.snapshot(),chat_id='-1003966683704')==[]

@pytest.mark.parametrize('thread', ['', '359', '1636', '796', '7', '99999'])
@pytest.mark.asyncio
async def test_family_food_reply_renders_both_buttons_in_original_topic(monkeypatch,thread):
    from gateway.platforms import telegram
    from gateway.miniapp_reply_contract import apply_miniapp_reply_contract
    monkeypatch.setattr(telegram,'InlineKeyboardButton',_Button)
    monkeypatch.setattr(telegram,'InlineKeyboardMarkup',_Markup)
    collector=MiniappReplyCollector()
    exact='shopping_buy_g11111111111141118111111111111111_c1003966683704'
    collector.record('mcp_health_actions_add_shopping_items',json.dumps({'ok':True,'action':'add-shopping-items','_miniapp':{'start_param':exact}}))
    text,params=apply_miniapp_reply_contract('Продукты сохранены.',collector.snapshot(),chat_id='-1003966683704')
    calls=[];adapter=_adapter(calls)
    result=await adapter.send('-1003966683704',text,metadata={'miniapp_start_params':params,'thread_id':thread})
    assert result.success
    buttons=[button for row in calls[-1]['reply_markup'].inline_keyboard for button in row]
    assert [button.url.split('startapp=')[1] for button in buttons]==[exact,'menu_c1003966683704']
    if thread:
        assert calls[-1]['message_thread_id']==int(thread)
