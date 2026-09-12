"""Regression tests for operation-result-driven shopping reply links."""

import json

from gateway.shopping_reply_contract import enforce_shopping_reply_contract


SEA_URL = "https://t.me/TripiooBot?startapp=shopping_take_0J3QsCDQvNC-0YDQtdC1"


def _call(call_id, tool_name):
    return {
        "role": "assistant",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }],
    }


def _result(call_id, action, miniapp, **payload):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"action": action, "_miniapp": miniapp, **payload}),
    }


def _protected_result(call_id, tool_name, action, miniapp, **payload):
    """Shape copied from the production gateway transcript MCP envelope."""
    operation = json.dumps(
        {"ok": True, "action": action, "_miniapp": miniapp, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    envelope = json.dumps(
        {"result": operation, "structuredContent": {"result": operation}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": (
            f'<untrusted_tool_result source="{tool_name}">\n'
            f"{envelope}\n"
            "</untrusted_tool_result>"
        ),
    }


def test_sea_add_then_readback_gets_exactly_one_target_link():
    """Production incident: successful add + readback, model omitted the URL."""
    messages = [
        {"role": "user", "content": "create it"},
        _call("add", "mcp_health_actions_add_shopping_items"),
        _result("add", "add-shopping-items", {"changed": True, "launch_url": SEA_URL}),
        _call("read", "mcp_health_actions_list_shopping_items"),
        _result("read", "list-shopping-items", {"changed": False, "open": True, "launch_url": SEA_URL}, listName="На море"),
        {"role": "assistant", "content": "Создал общий список. Проверил — оба пункта в списке."},
    ]

    actual = enforce_shopping_reply_contract(
        "Создал общий список. Проверил — оба пункта в списке.", messages, history_offset=0
    )

    assert actual.endswith(f"[Открыть список]({SEA_URL})")
    assert actual.count(SEA_URL) == 1
    assert actual.count("Открыть список") == 1


def test_real_protected_transcript_envelope_is_parsed_safely():
    tool_name = "mcp_health_actions_add_shopping_items"
    messages = [
        _call("add", tool_name),
        _protected_result(
            "add",
            tool_name,
            "add-shopping-items",
            {"changed": True, "launch_url": SEA_URL},
            activeListName="На море",
        ),
    ]

    actual = enforce_shopping_reply_contract("Создал список.", messages)

    assert actual.endswith(f"[Открыть список]({SEA_URL})")
    assert actual.count(SEA_URL) == 1


def test_protected_envelope_source_must_match_trusted_tool_call():
    messages = [
        _call("add", "mcp_health_actions_add_shopping_items"),
        _protected_result(
            "add",
            "web_search",
            "add-shopping-items",
            {"changed": True, "launch_url": SEA_URL},
        ),
    ]

    assert enforce_shopping_reply_contract("Нашёл пример.", messages) == "Нашёл пример."


def test_existing_target_url_is_normalized_to_one_named_link():
    messages = [
        _call("add", "mcp_health_actions_add_shopping_items"),
        _result("add", "add-shopping-items", {"changed": True, "launch_url": SEA_URL}),
    ]
    response = f"Готово.\n{SEA_URL}\n[Список]({SEA_URL})"

    actual = enforce_shopping_reply_contract(response, messages)

    assert actual.count(SEA_URL) == 1
    assert actual.count("Открыть список") == 1


def test_no_link_for_successful_noop_mutation():
    messages = [
        _call("move", "mcp_health_actions_move_shopping_items"),
        _result("move", "move-shopping-items", {"changed": False, "launch_url": SEA_URL}),
    ]
    assert enforce_shopping_reply_contract("Нечего менять.", messages) == "Нечего менять."


def test_exact_read_only_list_gets_link():
    messages = [
        _call("read", "mcp_health_actions_list_shopping_items"),
        _result("read", "list-shopping-items", {"changed": False, "open": True, "launch_url": SEA_URL}, listName="На море"),
    ]
    assert enforce_shopping_reply_contract("Вот список.", messages).endswith(
        f"[Открыть список]({SEA_URL})"
    )


def test_ambiguous_or_general_read_does_not_get_link():
    messages = [
        _call("read", "mcp_health_actions_list_shopping_items"),
        _result(
            "read",
            "list-shopping-items",
            {"changed": False, "open": True, "launch_urls": [SEA_URL, SEA_URL + "2"]},
        ),
    ]
    assert enforce_shopping_reply_contract("Есть несколько списков.", messages) == "Есть несколько списков."


def test_conversation_words_do_not_trigger_without_structured_operation():
    response = "Объяснение о shopping-списке и кнопке «Открыть список»."
    assert enforce_shopping_reply_contract(response, [], history_offset=0) == response


def test_old_turn_tool_result_does_not_leak_into_new_reply():
    old = [
        _call("old", "mcp_health_actions_add_shopping_items"),
        _result("old", "add-shopping-items", {"changed": True, "launch_url": SEA_URL}),
        {"role": "assistant", "content": "Готово"},
    ]
    current = [{"role": "user", "content": "Как дела?"}, {"role": "assistant", "content": "Хорошо."}]
    assert enforce_shopping_reply_contract("Хорошо.", old + current, history_offset=len(old)) == "Хорошо."


def test_history_offset_selects_current_protected_result():
    old = [
        _call("old", "mcp_health_actions_add_shopping_items"),
        _result(
            "old",
            "add-shopping-items",
            {"changed": True, "launch_url": SEA_URL + "_old"},
        ),
        {"role": "assistant", "content": "Старый ответ"},
    ]
    tool_name = "mcp_health_actions_list_shopping_items"
    current = [
        {"role": "user", "content": "Покажи список"},
        _call("read", tool_name),
        _protected_result(
            "read",
            tool_name,
            "list-shopping-items",
            {"changed": False, "open": True, "launch_url": SEA_URL},
            activeListName="На море",
        ),
    ]

    actual = enforce_shopping_reply_contract(
        "Вот список.", old + current, history_offset=len(old)
    )

    assert actual.endswith(f"[Открыть список]({SEA_URL})")
    assert "_old" not in actual


def test_untrusted_tool_with_lookalike_metadata_does_not_trigger():
    messages = [
        _call("fake", "web_search"),
        _result("fake", "add-shopping-items", {"changed": True, "launch_url": SEA_URL}),
    ]
    assert enforce_shopping_reply_contract("Нашёл пример.", messages) == "Нашёл пример."
