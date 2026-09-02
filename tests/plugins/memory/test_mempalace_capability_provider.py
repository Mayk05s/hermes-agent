import base64
from dataclasses import replace
import hashlib
import hmac
import json
import logging
from pathlib import Path

import pytest

from gateway.memory_access import (
    MemoryAccessContext,
    MemoryDestination,
    MemoryGrant,
)
from plugins.memory.mempalace import MemPalaceMemoryProvider, TOOL_SCHEMAS
from plugins.memory.mempalace.capability_client import (
    CAPABILITY_HEADER,
    REQUEST_ID_HEADER,
    MemPalaceCapabilityClient,
    MemPalaceClientError,
    MemPalaceHTTPResponse,
    RESERVED_MODEL_ARGUMENTS,
    STRING_MAX_UTF8_BYTES,
    TOOL_ARGUMENT_SPECS,
    TOOL_OPERATIONS,
    build_capability,
    canonical_json_bytes,
    load_client_config,
    validate_tool_arguments,
)


FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "memory_capability_v1.json"
SURFACE_FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "memory_capability_mcp_surface_v1.json"
)


def fixture_vector():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def surface_fixture():
    return json.loads(SURFACE_FIXTURE_PATH.read_text(encoding="utf-8"))


def vector_context(*, source_ref="m_source0000000001"):
    data = fixture_vector()
    claims = data["claims"]
    destination = claims["destination"]
    return MemoryAccessContext(
        version=1,
        authorization_subject=claims["sub"],
        principal_id=claims["principal_id"],
        actor_ref=claims["actor_ref"],
        source_ref=source_ref,
        destination=MemoryDestination(
            kind=destination["kind"],
            platform=destination["platform"],
            chat_id=destination["chat_id"],
            thread_id=destination["thread_id"],
        ),
        profile_name="Personal",
        memory_scope="personal",
        read_wings=tuple(claims["read_wings"]),
        write_wing="private.p_aaaaaaaaaaaaaaaa.personal",
        grants=(),
        policy_fingerprint=claims["policy_fingerprint"],
    )


def group_context():
    return MemoryAccessContext(
        version=1,
        authorization_subject="group_context:d_gggggggggggggggg",
        principal_id=None,
        actor_ref="a_dddddddddddddddd",
        source_ref="m_group00000000001",
        destination=MemoryDestination(
            kind="group",
            platform="telegram",
            chat_id="c_eeeeeeeeeeeeeeee",
            thread_id="t_ffffffffffffffff",
        ),
        profile_name="Planning",
        memory_scope="general",
        read_wings=(
            "group.telegram.c_eeeeeeeeeeeeeeee.t_ffffffffffffffff.general",
        ),
        write_wing=(
            "group.telegram.c_eeeeeeeeeeeeeeee.t_ffffffffffffffff.general"
        ),
        grants=(),
        policy_fingerprint=fixture_vector()["claims"]["policy_fingerprint"],
    )


def decode_capability(header):
    version, payload_segment, signature_segment = header.split(".")
    assert version == "v1"
    payload_bytes = base64.urlsafe_b64decode(
        payload_segment + "=" * (-len(payload_segment) % 4)
    )
    signature = base64.urlsafe_b64decode(
        signature_segment + "=" * (-len(signature_segment) % 4)
    )
    return payload_segment, json.loads(payload_bytes), signature


class RecordingTransport:
    def __init__(self, *, status=200, response=None):
        self.status = status
        self.response = response
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        response = self.response
        if response is None and url.endswith("/mcp"):
            request = json.loads(body)
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"matches": []})}
                    ]
                },
            }
        elif response is None and url.endswith("/ingest"):
            response = {"queued": len(json.loads(body)["messages"])}
        elif response is None:
            response = {}
        return MemPalaceHTTPResponse(
            status=self.status,
            body=canonical_json_bytes(response),
            content_type="application/json",
        )


def test_shared_cross_repo_signer_vector_matches_exact_bytes_and_header():
    vector = fixture_vector()
    config = load_client_config(vector["environment"])
    request = vector["request"]
    body = request["body_utf8"].encode("utf-8")

    header, request_id, claims = build_capability(
        vector_context(source_ref=None),
        config,
        operation="read",
        method=request["method"],
        raw_target=request["raw_target"],
        body=body,
        now=vector["claims"]["iat"],
        jti=request["request_id"],
    )

    assert request_id == request["request_id"]
    assert hashlib.sha256(body).hexdigest() == request["body_sha256"]
    assert claims == vector["claims"]
    assert canonical_json_bytes(claims).decode("utf-8") == vector["canonical_payload_utf8"]
    assert canonical_json_bytes(claims).hex() == vector["canonical_payload_hex"]
    assert header == vector["capability_header"]
    assert hashlib.sha256(header.encode("ascii")).hexdigest() == vector[
        "capability_header_sha256"
    ]

    payload_segment, decoded, signature = decode_capability(header)
    expected_signature = hmac.new(
        vector["environment"]["MEMPALACE_CAPABILITY_HMAC_SECRET_HERMES"].encode(),
        f"v1.{payload_segment}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    assert decoded == vector["claims"]
    assert hmac.compare_digest(signature, expected_signature)


def test_client_binds_exact_raw_target_body_and_required_headers():
    vector = fixture_vector()
    transport = RecordingTransport()
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        clock=lambda: vector["claims"]["iat"],
        jti_factory=lambda: vector["request"]["request_id"],
        transport=transport,
    )

    assert client.request(
        vector_context(source_ref=None),
        operation="read",
        method="GET",
        raw_target=vector["request"]["raw_target"],
    ) == {}

    call = transport.calls[0]
    assert call["url"] == "https://memory.invalid/wings?limit=2&offset=1"
    assert call["body"] == b""
    assert call["headers"]["Authorization"] == vector["request"]["authorization"]
    assert call["headers"][CAPABILITY_HEADER] == vector["capability_header"]
    assert call["headers"][REQUEST_ID_HEADER] == vector["request"]["request_id"]


def test_every_request_gets_fresh_one_shot_jti_and_context_snapshot():
    vector = fixture_vector()
    ids = iter(["r_first00000000001", "r_second0000000001"])
    rpc_ids = iter(["rpc_first", "rpc_second"])
    transport = RecordingTransport()
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        clock=lambda: vector["claims"]["iat"],
        jti_factory=lambda: next(ids),
        rpc_id_factory=lambda: next(rpc_ids),
        transport=transport,
    )
    provider = MemPalaceMemoryProvider(client=client)
    provider.set_access_context(vector_context())
    assert "matches" in provider.handle_tool_call("mempalace_search", {"query": "first"})
    provider.set_access_context(group_context())
    assert "matches" in provider.handle_tool_call("mempalace_search", {"query": "second"})

    first_header = transport.calls[0]["headers"][CAPABILITY_HEADER]
    second_header = transport.calls[1]["headers"][CAPABILITY_HEADER]
    _, first_claims, _ = decode_capability(first_header)
    _, second_claims, _ = decode_capability(second_header)
    assert first_claims["jti"] != second_claims["jti"]
    assert first_claims["destination"]["kind"] == "dm"
    assert second_claims["destination"]["kind"] == "group"
    assert first_claims["source_ref"] is None
    assert second_claims["source_ref"] is None
    assert second_claims["principal_id"] is None
    assert not any(wing.startswith("private.") for wing in second_claims["read_wings"])


def test_ingest_transport_has_only_semantic_body_and_write_only_capability():
    vector = fixture_vector()
    transport = RecordingTransport()
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        clock=lambda: vector["claims"]["iat"],
        jti_factory=lambda: "r_ingest0000000001",
        transport=transport,
    )
    provider = MemPalaceMemoryProvider(client=client)
    provider.set_access_context(vector_context())

    result = provider.ingest_messages(
        [
            {"sender": "user", "content": "remember this"},
            {"sender": "assistant", "content": "acknowledged"},
        ]
    )

    assert result == {"queued": 2}
    call = transport.calls[0]
    assert json.loads(call["body"]) == {
        "kind": "chunk",
        "messages": [
            {"content": "remember this", "sender": "user"},
            {"content": "acknowledged", "sender": "assistant"},
        ],
    }
    _, claims, _ = decode_capability(call["headers"][CAPABILITY_HEADER])
    assert claims["read_wings"] == []
    assert claims["write_wing"] == "private.p_aaaaaaaaaaaaaaaa.personal"
    assert claims["source_ref"] == "m_source0000000001"
    assert not set(json.loads(call["body"])) & {
        "wing",
        "principal_id",
        "source_ref",
        "grants",
        "capability",
    }


def test_sync_turn_remains_disabled_until_daemon_scope_is_accepted():
    vector = fixture_vector()
    transport = RecordingTransport()
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        transport=transport,
    )
    provider = MemPalaceMemoryProvider(client=client)
    provider.set_access_context(vector_context())

    provider.sync_turn("private user text", "assistant text")

    assert transport.calls == []


def test_absent_context_and_acl_model_args_fail_before_transport():
    vector = fixture_vector()
    transport = RecordingTransport()
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        transport=transport,
    )
    provider = MemPalaceMemoryProvider(client=client)

    assert provider.prefetch("private question") == ""
    assert "unavailable" in provider.handle_tool_call(
        "mempalace_search", {"query": "x"}
    )
    assert "unavailable" in provider.handle_tool_call(
        "mempalace_search", {"query": "x", "wing": "private.fake"}
    )
    with pytest.raises(MemPalaceClientError, match="memory_access_denied"):
        provider.ingest_messages([{"sender": "user", "content": "x"}])
    assert transport.calls == []


def test_tool_surface_matches_shared_fixture_and_contains_no_acl_selectors():
    fixture = surface_fixture()
    safe_tools = fixture["tools_list"]["safe_tools"]
    fixture_by_name = {tool["name"]: tool for tool in safe_tools}
    assert set(fixture_by_name) == set(TOOL_ARGUMENT_SPECS) == set(TOOL_OPERATIONS)
    assert RESERVED_MODEL_ARGUMENTS == frozenset(
        fixture["argument_constraints"]["reserved_arguments_rejected"]
    )
    assert STRING_MAX_UTF8_BYTES == fixture["argument_constraints"][
        "string_max_utf8_bytes"
    ]

    schemas_by_name = {schema["name"]: schema for schema in TOOL_SCHEMAS}
    assert set(schemas_by_name) == set(fixture_by_name)
    for name, expected in fixture_by_name.items():
        allowed, required = TOOL_ARGUMENT_SPECS[name]
        assert allowed == frozenset(expected["allowed_arguments"])
        assert required == frozenset(expected["required_arguments"])
        assert TOOL_OPERATIONS[name] == expected["operation"]

        schema = schemas_by_name[name]
        parameters = schema["parameters"]
        assert set(parameters["properties"]) == allowed
        assert set(parameters["required"]) == required
        assert parameters["additionalProperties"] is False
        assert not RESERVED_MODEL_ARGUMENTS & set(parameters["properties"])


def test_every_reserved_acl_argument_is_rejected_before_transport():
    for forbidden in RESERVED_MODEL_ARGUMENTS:
        with pytest.raises(MemPalaceClientError, match="invalid_tool_arguments"):
            validate_tool_arguments("mempalace_search", {"query": "x", forbidden: "x"})


def test_tool_argument_edge_constraints_match_frozen_surface():
    assert validate_tool_arguments(
        "mempalace_list_drawers", {"limit": 1, "offset": 49}
    ) == {"limit": 1, "offset": 49}
    with pytest.raises(MemPalaceClientError, match="invalid_tool_arguments"):
        validate_tool_arguments(
            "mempalace_list_drawers", {"limit": 2, "offset": 49}
        )
    with pytest.raises(MemPalaceClientError, match="invalid_tool_arguments"):
        validate_tool_arguments("mempalace_update_drawer", {"drawer_id": "d_123"})
    assert validate_tool_arguments(
        "mempalace_update_drawer", {"drawer_id": "d_123", "content": ""}
    ) == {"drawer_id": "d_123", "content": ""}


def test_authorized_mcp_tool_failure_is_not_returned_as_memory_content():
    vector = fixture_vector()
    failure = surface_fixture()["envelopes"]["authorized_tool_failure"]["body"]
    transport = RecordingTransport(response=failure)
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        clock=lambda: vector["claims"]["iat"],
        jti_factory=lambda: "r_toolerror00000001",
        rpc_id_factory=lambda: "fixture-call",
        transport=transport,
    )

    with pytest.raises(MemPalaceClientError, match="rpc_tool_error"):
        client.mcp_call(
            vector_context(source_ref=None),
            "mempalace_search",
            {"query": "x"},
            operation="read",
        )
    assert len(transport.calls) == 1


def test_body_tamper_changes_bound_hash_and_capability():
    vector = fixture_vector()
    config = load_client_config(vector["environment"])
    kwargs = {
        "context": vector_context(),
        "config": config,
        "operation": "write",
        "method": "POST",
        "raw_target": "/mcp",
        "now": vector["claims"]["iat"],
        "jti": "r_tamper0000000001",
    }
    first, _, first_claims = build_capability(body=b"{}", **kwargs)
    second, _, second_claims = build_capability(body=b'{"x":1}', **kwargs)

    assert first != second
    assert first_claims["body_sha256"] != second_claims["body_sha256"]


def test_get_body_and_fragment_target_are_rejected_before_signing():
    vector = fixture_vector()
    config = load_client_config(vector["environment"])
    for body, target, error in (
        (b"unexpected", "/wings", "invalid_body"),
        (b"", "/wings#fragment", "invalid_target"),
    ):
        with pytest.raises(MemPalaceClientError, match=error):
            build_capability(
                vector_context(source_ref=None),
                config,
                operation="read",
                method="GET",
                raw_target=target,
                body=body,
                now=100,
                jti="r_binding000000001",
            )


def test_capability_expiry_is_short_and_bound_to_configured_ttl():
    vector = fixture_vector()
    config = load_client_config(vector["environment"])
    _, _, claims = build_capability(
        vector_context(source_ref=None),
        config,
        operation="read",
        method="GET",
        raw_target="/wings",
        body=b"",
        now=100,
        jti="r_expiry0000000001",
    )

    assert claims["iat"] == 100
    assert claims["exp"] == 100 + config.ttl_seconds
    assert 1 <= claims["exp"] - claims["iat"] <= 60


def test_group_capability_cannot_gain_private_read_through_generic_grant():
    vector = fixture_vector()
    forged = replace(
        group_context(),
        read_wings=("private.p_aaaaaaaaaaaaaaaa.personal",),
        grants=(MemoryGrant(id="family", type="read_from_group"),),
    )

    with pytest.raises(MemPalaceClientError, match="memory_access_denied"):
        build_capability(
            forged,
            load_client_config(vector["environment"]),
            operation="read",
            method="GET",
            raw_target="/wings",
            body=b"",
            now=100,
            jti="r_groupprivate000001",
        )


def test_disclosure_grant_is_not_a_capability_shortcut():
    vector = fixture_vector()
    forged = replace(
        vector_context(source_ref=None),
        grants=(
            MemoryGrant(id="family", type="disclose_private_to_group"),
        ),
    )

    with pytest.raises(MemPalaceClientError, match="memory_access_denied"):
        build_capability(
            forged,
            load_client_config(vector["environment"]),
            operation="read",
            method="GET",
            raw_target="/wings",
            body=b"",
            now=100,
            jti="r_disclosure0000001",
        )


def test_tool_schemas_contain_no_acl_or_palace_selectors():
    for schema in TOOL_SCHEMAS:
        parameters = schema["parameters"]
        assert parameters["additionalProperties"] is False
        assert not RESERVED_MODEL_ARGUMENTS & set(parameters["properties"])


def test_fixed_tool_surface_does_not_enable_write_without_current_provenance():
    vector = fixture_vector()
    transport = RecordingTransport()
    provider = MemPalaceMemoryProvider(
        client=MemPalaceCapabilityClient(
            environ=vector["environment"],
            transport=transport,
        )
    )
    provider.set_access_context(vector_context(source_ref=None))
    assert "mempalace_add_drawer" in {
        schema["name"] for schema in provider.get_tool_schemas()
    }
    assert "unavailable" in provider.handle_tool_call(
        "mempalace_add_drawer", {"room": "notes", "content": "private"}
    )
    assert transport.calls == []


def test_frozen_post_init_write_surface_fails_closed_after_context_is_cleared():
    vector = fixture_vector()
    transport = RecordingTransport()
    provider = MemPalaceMemoryProvider(
        client=MemPalaceCapabilityClient(
            environ=vector["environment"],
            transport=transport,
        )
    )
    provider.set_access_context(vector_context())
    assert "mempalace_add_drawer" in {
        schema["name"] for schema in provider.get_tool_schemas()
    }

    # AIAgent freezes its tool definitions after provider initialization. If
    # the next gateway event clears the context, execution still re-checks the
    # current typed context and cannot use the formerly advertised write.
    provider.set_access_context(None)
    assert "unavailable" in provider.handle_tool_call(
        "mempalace_add_drawer", {"room": "notes", "content": "private"}
    )
    assert transport.calls == []


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_denied_expired_or_replayed_response_is_not_retried(status):
    vector = fixture_vector()
    transport = RecordingTransport(status=status, response={"error": "private detail"})
    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        clock=lambda: vector["claims"]["iat"],
        jti_factory=lambda: "r_repeated000000001",
        transport=transport,
    )

    with pytest.raises(MemPalaceClientError, match="request_denied"):
        client.request(
            vector_context(source_ref=None),
            operation="read",
            method="GET",
            raw_target="/wings",
        )
    assert len(transport.calls) == 1


def test_invalid_or_sensitive_client_config_fails_closed():
    vector = fixture_vector()
    env = dict(vector["environment"])
    env["MEMPALACE_CAPABILITY_HMAC_SECRET_HERMES"] = env[
        "MEMPALACE_BEARER_TOKEN"
    ]
    with pytest.raises(
        MemPalaceClientError,
        match="credential_classes_not_independent",
    ):
        load_client_config(env)

    env = dict(vector["environment"])
    env["MEMPALACE_BASE_URL"] = "http://memory.example"
    with pytest.raises(MemPalaceClientError, match="insecure_base_url"):
        load_client_config(env)

    env = dict(vector["environment"])
    env["MEMPALACE_POLICY_FINGERPRINT"] = "sha256:" + "f" * 64
    with pytest.raises(MemPalaceClientError, match="stale_policy_fingerprint"):
        build_capability(
            vector_context(source_ref=None),
            load_client_config(env),
            operation="read",
            method="GET",
            raw_target="/wings",
            body=b"",
            now=1,
            jti="r_config0000000001",
        )


def test_network_failure_does_not_leak_exception_or_credentials(caplog):
    vector = fixture_vector()

    def fail_transport(*_args):
        raise RuntimeError("token=super-secret-network-detail")

    client = MemPalaceCapabilityClient(
        environ=vector["environment"],
        transport=fail_transport,
    )
    provider = MemPalaceMemoryProvider(client=client)
    provider.set_access_context(vector_context())

    with caplog.at_level(logging.WARNING):
        result = provider.handle_tool_call("mempalace_search", {"query": "x"})

    assert "unavailable" in result
    assert "super-secret-network-detail" not in caplog.text
    assert vector["environment"]["MEMPALACE_BEARER_TOKEN"] not in caplog.text


def test_arbitrary_exception_code_is_not_logged(caplog):
    vector = fixture_vector()

    class SensitiveError(RuntimeError):
        code = "token=secret-in-code-field"

    def fail_transport(*_args):
        raise SensitiveError("another secret")

    provider = MemPalaceMemoryProvider(
        client=MemPalaceCapabilityClient(
            environ=vector["environment"],
            transport=fail_transport,
        )
    )
    provider.set_access_context(vector_context())

    with caplog.at_level(logging.WARNING):
        result = provider.handle_tool_call("mempalace_search", {"query": "x"})

    assert "unavailable" in result
    assert "secret" not in caplog.text
    assert "network_error" in caplog.text
