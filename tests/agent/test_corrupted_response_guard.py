"""Tests for fail-closed handling of severely damaged provider output."""

from agent.conversation_loop import _looks_like_corrupted_model_output


def test_detects_fragmented_decode_damaged_output():
    damaged = (
        "We need to answer.,im/data?][ayicalai_f/b/profile(--\n"
        "//+$b FIRST false somev the/ [\ufffdical\n\n"
        + "\n".join(["& & &", "[", "]", "==", "� ;"] * 20)
    )

    assert _looks_like_corrupted_model_output(damaged)


def test_keeps_normal_markdown_and_code():
    normal = """Here is the implementation:

```python
def normalize(items):
    return [item.strip() for item in items if item]
```

The function filters empty values and returns a clean list. It remains
readable even though the answer contains Markdown punctuation and code.
"""

    assert not _looks_like_corrupted_model_output(normal)


def test_keeps_short_reasoning_phrase_when_quoted_by_user():
    quoted = 'The phrase "We need to answer" appeared in the provider log.'

    assert not _looks_like_corrupted_model_output(quoted)
