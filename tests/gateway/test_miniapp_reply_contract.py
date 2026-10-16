import json

import pytest

from gateway.miniapp_reply_contract import (
    MiniappReplyCollector, apply_miniapp_reply_contract, miniapp_reply_params,
    miniapp_reply_records,
)


LIST = "shopping_buy_0J_QvtC60YPQv9C60Lg"
OTHER = "shopping_take_0J3QsCDQvNC-0YDQtdC1"


def result(action, param=LIST, **extra):
    return {"ok": True, "action": action,
            "_miniapp": {"start_param": param, "changed": True}, **extra}


def record(collector, action, data=None, **kwargs):
    collector.record("mcp_health_actions_" + action.replace("-", "_"),
                     json.dumps(data if data is not None else result(action)), **kwargs)


def test_failed_pre_read_and_failed_mutation_do_not_cancel_successful_retry():
    collector = MiniappReplyCollector()
    record(collector, "list-shopping-items", {"error": "truncated"})
    record(collector, "delete-shopping-items", {"error": "planning_group_context_missing"}, is_error=True)
    record(collector, "delete-shopping-items")
    text, params = apply_miniapp_reply_contract("Удалил муку.", collector.snapshot())
    assert text == "Удалил муку."
    assert params == [LIST]


def test_mutations_keep_each_list_and_ignore_unrelated_discovery_read():
    collector = MiniappReplyCollector()
    record(collector, "list-shopping-items", result("list-shopping-items", OTHER))
    record(collector, "delete-shopping-items")
    record(collector, "add-shopping-items", result("add-shopping-items", OTHER))
    record(collector, "list-shopping-items", result("list-shopping-items", "shopping"))
    assert miniapp_reply_params(collector.snapshot()) == [LIST, OTHER]


def test_multi_list_result_gets_all_buttons():
    collector = MiniappReplyCollector()
    record(collector, "move-shopping-items", result("move-shopping-items", _miniapp={
        "launch_urls": [f"https://t.me/TripiooBot?startapp={p}" for p in [LIST, OTHER]],
    }))
    assert miniapp_reply_params(collector.snapshot()) == [LIST, OTHER]


def test_latest_successful_exact_read_wins_over_initial_search():
    collector = MiniappReplyCollector()
    record(collector, "list-shopping-items", result("list-shopping-items", OTHER))
    record(collector, "list-shopping-items")
    record(collector, "list-shopping-items", {"error": "transient"})
    assert miniapp_reply_params(collector.snapshot()) == [LIST]


@pytest.mark.parametrize("action,param", [
    ("list-planning-tasks", "planning"), ("create-planning-task", "planning"),
    ("update-planning-task", "planning"), ("delete-planning-task", "planning"),
    ("list-nutrition-journal", "nutrition_history"),
    ("record-nutrition-meal", "nutrition_history"),
    ("get-nutrition-statistics", "stats"), ("record-body-weight", "stats"),
    ("record-cardio-activity", "fitness"), ("create-workout-slots", "fitness"),
    ("list-menu-entries", "menu"), ("replace-menu-entries", "menu"),
    ("list-wishlist-items", "wishlist"), ("record-medication-intake", "meds"),
    ("set-shopping-items-bought", "shopping"),
    ("refine-shopping-item", "shopping"),
])
def test_reads_and_idempotent_writes_get_section_from_successful_action(action, param):
    collector = MiniappReplyCollector()
    record(collector, action, {"ok": True, "action": action, "changedCount": 0})
    assert miniapp_reply_params(collector.snapshot()) == [param]


@pytest.mark.parametrize("data", [
    {"ok": False, "action": "add-shopping-items"},
    {"ok": True, "action": "delete-shopping-items"},
    {"isError": True, "structuredContent": result("add-shopping-items")},
    {"error": "failed", "result": result("add-shopping-items")},
    {"success": True, "_miniapp": {"start_param": LIST}},
])
def test_unsuccessful_mismatched_or_unverified_results_do_not_authorize_button(data):
    collector = MiniappReplyCollector()
    record(collector, "add-shopping-items", data)
    assert miniapp_reply_params(collector.snapshot()) == []


def test_other_tools_cannot_forge_buttons_and_collectors_do_not_share_state():
    first, second = MiniappReplyCollector(), MiniappReplyCollector()
    record(first, "add-shopping-items")
    second.record("terminal", json.dumps(result("add-shopping-items")))
    assert miniapp_reply_params(first.snapshot()) == [LIST]
    assert second.snapshot() == []


def test_payload_wrappers_and_large_results_are_collected_before_transcript_loss():
    collector = MiniappReplyCollector()
    data = result("add-shopping-items", items=[{"title": "x" * 400000}])
    record(collector, "add-shopping-items", {"result": json.dumps(data), "structuredContent": data})
    records = collector.snapshot()
    assert len(json.dumps(records)) < 300
    assert miniapp_reply_params(records) == [LIST]


def test_history_requires_same_turn_call_binding_and_never_uses_result_authored_name():
    call = {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "mcp_health_actions_add_shopping_items"}}]}
    output = {"role": "tool", "tool_call_id": "a", "content": json.dumps(result("add-shopping-items"))}
    messages = [call, output, {"role": "user", "content": "Привет"}]
    assert miniapp_reply_params(miniapp_reply_records(messages)) == [LIST]
    assert miniapp_reply_records(messages, 2) == []
    assert miniapp_reply_records([dict(output, name="mcp_health_actions_add_shopping_items")]) == []


def test_model_links_removed_but_no_old_rows_appended_after_delete():
    collector = MiniappReplyCollector()
    record(collector, "list-shopping-items", result("list-shopping-items", items=[{"title": "Мука"}]))
    record(collector, "delete-shopping-items")
    text, params = apply_miniapp_reply_contract(
        f"Удалил.\n[Список](https://t.me/TripiooBot?startapp={OTHER})", collector.snapshot())
    assert text == "Удалил."
    assert params == [LIST]


def test_rollover_handoff_only_projects_new_generation_rows():
    from gateway.shopping_message_projection import projection_row_ids
    collector = MiniappReplyCollector()
    generation = '11111111-1111-1111-1111-111111111111'
    record(collector, 'rollover-shopping-list', result('rollover-shopping-list',
        'shopping_buy_g' + generation.replace('-', ''),
        oldListId='22222222-2222-2222-2222-222222222222',
        liveListId=generation, archivedCount=50, completedCount=45,
        items=[{'shoppingId': 101, 'liveListId': generation},
               {'shoppingId': 102, 'liveListId': generation}]))
    operation = collector.snapshot()[0]
    assert operation['success'] and operation['mutation'] and not operation['display']
    assert projection_row_ids(collector.snapshot()) == [101, 102]
