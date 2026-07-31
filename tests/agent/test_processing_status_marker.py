from agent.conversation_loop import _consume_processing_status_marker


def test_consume_processing_status_marker_removes_hidden_marker():
    cleaned, found = _consume_processing_status_marker("[[processing:eyes]]\n\nИщу детали.")

    assert found is True
    assert cleaned == "Ищу детали."


def test_consume_processing_status_marker_leaves_normal_text_unchanged():
    cleaned, found = _consume_processing_status_marker("Быстрый ответ без статуса.")

    assert found is False
    assert cleaned == "Быстрый ответ без статуса."
