import json
from unittest.mock import patch

from tools import send_message_tool as sm


def test_profile_outbound_allowlist_denies_other_target_before_delivery():
    with patch.object(sm, "_configured_allowed_targets", return_value={"telegram:179555559"}), patch.object(
        sm, "_send_to_platform"
    ) as send:
        result = json.loads(
            sm.send_message_tool(
                {
                    "action": "send",
                    "target": "telegram:-5526305849",
                    "message": "diagnostic",
                }
            )
        )

    assert result == {"error": "Outbound target is not allowed by this profile"}
    send.assert_not_called()


def test_profile_outbound_allowlist_accepts_canonical_owner_target():
    with patch.object(sm, "_configured_allowed_targets", return_value={"telegram:179555559"}):
        assert sm._target_allowed(" TELEGRAM:179555559 ") is True
        assert sm._target_allowed("telegram:179555558") is False


def test_restricted_profile_list_does_not_expose_gateway_directory():
    with patch.object(sm, "_configured_allowed_targets", return_value={"telegram:179555559"}):
        result = json.loads(sm.send_message_tool({"action": "list"}))

    assert result == {"targets": ["telegram:179555559"]}


def test_missing_allowlist_preserves_existing_unrestricted_behavior():
    with patch.object(sm, "_configured_allowed_targets", return_value=None):
        assert sm._target_allowed("telegram:anywhere") is True
