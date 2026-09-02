from dataclasses import FrozenInstanceError, asdict
import inspect
import json

import pytest

from gateway.config import Platform
from gateway.memory_access import (
    _opaque_ref,
    decode_memory_opaque_id_key,
    generate_memory_opaque_id_key,
    generate_memory_principal_id,
    parse_memory_access_policy,
    resolve_memory_access_context,
)
from gateway.session import SessionSource


OPAQUE_KEY = b"test-only-memory-opaque-id-key!" * 2


def policy_dict():
    return {
        "version": 1,
        "default_private_zone": "personal",
        "default_group_zone": "general",
        "groups": [
            {
                "id": "family",
                "platform": "telegram",
                "chat_id": "-100-family",
                "zone": "general",
            },
            {
                "id": "boxmap_topic",
                "platform": "telegram",
                "chat_id": "-100-family",
                "thread_id": "6827",
                "zone": "boxmap",
            },
        ],
        "principals": [
            {
                "id": "p_k3m7w2r5t6y4q2zx",
                "identities": [
                    {"platform": "telegram", "user_id": "1001"},
                    {"platform": "signal", "user_id_alt": "mikhail-signal"},
                ],
                "dm_destinations": [
                    {"platform": "telegram", "chat_id": "1001"},
                    {"platform": "signal", "chat_id": "mikhail-signal"},
                ],
                "private_zones": ["personal", "health", "boxmap", "planning"],
                "group_memberships": ["family", "boxmap_topic"],
                "dm_rules": [
                    {
                        "scope": "personal",
                        "private_zones": ["personal", "health", "boxmap", "planning"],
                        "group_memberships": ["family", "boxmap_topic"],
                    }
                ],
            },
            {
                "id": "p_n4v2c7b5x6m3q2rs",
                "identities": [{"platform": "telegram", "user_id": "2002"}],
                "dm_destinations": [{"platform": "telegram", "chat_id": "2002"}],
                "private_zones": ["personal", "health"],
                "group_memberships": ["family"],
                "dm_rules": [],
            },
        ],
    }


def source(
    *,
    chat_id="1001",
    chat_type="dm",
    user_id="1001",
    thread_id=None,
    memory_scope="personal",
    profile_name="Personal",
    message_id="55",
):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        thread_id=thread_id,
        memory_scope=memory_scope,
        profile_name=profile_name,
        message_id=message_id,
    )


def resolve(src, *, authorized=True, verified=True, raw_policy=None):
    return resolve_memory_access_context(
        src,
        parse_memory_access_policy(raw_policy or policy_dict()),
        request_authorized=authorized,
        principal_verified=verified,
        opaque_id_key=OPAQUE_KEY,
    )


def test_same_verified_user_across_profiles_keeps_principal_and_private_zone():
    personal = resolve(source(profile_name="Personal"))
    alternate = resolve(source(profile_name="Planning", memory_scope="planning"))

    assert personal is not None and alternate is not None
    assert personal.principal_id == alternate.principal_id == "p_k3m7w2r5t6y4q2zx"
    assert personal.write_wing == "private.p_k3m7w2r5t6y4q2zx.personal"
    assert alternate.write_wing == "private.p_k3m7w2r5t6y4q2zx.planning"
    assert personal.profile_name != alternate.profile_name


def test_different_users_have_separate_private_memory():
    mikhail = resolve(source())
    anna = resolve(source(chat_id="2002", user_id="2002"))

    assert mikhail is not None and anna is not None
    assert mikhail.write_wing == "private.p_k3m7w2r5t6y4q2zx.personal"
    assert anna.write_wing == "private.p_n4v2c7b5x6m3q2rs.personal"
    assert not set(mikhail.read_wings) & set(anna.read_wings)


def test_personal_dm_federates_declared_private_zones_and_group_memberships():
    context = resolve(source())

    assert context is not None
    assert {wing for wing in context.read_wings if wing.startswith("private.")} == {
        "private.p_k3m7w2r5t6y4q2zx.personal",
        "private.p_k3m7w2r5t6y4q2zx.health",
        "private.p_k3m7w2r5t6y4q2zx.boxmap",
        "private.p_k3m7w2r5t6y4q2zx.planning",
    }
    assert len([wing for wing in context.read_wings if wing.startswith("group.")]) == 2
    assert {(grant.id, grant.type) for grant in context.grants} == {
        ("family", "read_from_group"),
        ("boxmap_topic", "read_from_group"),
    }


def test_non_federated_dm_scope_reads_and_writes_only_its_private_zone():
    context = resolve(source(memory_scope="health", profile_name="Health"))

    assert context is not None
    assert context.read_wings == ("private.p_k3m7w2r5t6y4q2zx.health",)
    assert context.write_wing == "private.p_k3m7w2r5t6y4q2zx.health"
    assert context.grants == ()


def test_group_is_shared_between_users_but_never_contains_private_wings():
    first = resolve(
        source(chat_id="-100-family", chat_type="group", user_id="1001", memory_scope="general")
    )
    second = resolve(
        source(chat_id="-100-family", chat_type="group", user_id="2002", memory_scope="general")
    )

    assert first is not None and second is not None
    assert first.read_wings == second.read_wings
    assert first.write_wing == second.write_wing
    assert first.principal_id == "p_k3m7w2r5t6y4q2zx"
    assert second.principal_id == "p_n4v2c7b5x6m3q2rs"
    assert all(not wing.startswith("private.") for wing in first.read_wings)
    assert all(not wing.startswith("private.") for wing in second.read_wings)


def test_group_without_verified_participant_uses_bound_group_context():
    context = resolve(
        source(
            chat_id="-100-family",
            chat_type="group",
            user_id=None,
            memory_scope="general",
        ),
        verified=False,
    )

    assert context is not None
    assert context.principal_id is None
    assert context.authorization_subject.startswith("group_context:d_")
    assert context.actor_ref == "unknown"
    assert len(context.read_wings) == 1
    assert context.read_wings == (context.write_wing,)


def test_unverified_or_ambiguous_identity_fails_closed_for_dm():
    assert resolve(source(user_id=None), verified=False) is None
    assert resolve(source(user_id="not-configured"), verified=True) is None

    raw = policy_dict()
    raw["principals"][1]["identities"].append(
        {"platform": "telegram", "user_id_alt": "mikhail-alt"}
    )
    ambiguous = source()
    ambiguous.user_id_alt = "mikhail-alt"
    assert resolve(ambiguous, raw_policy=raw) is None


def test_dm_requires_exact_principal_destination_even_when_sender_is_verified():
    mislabeled_group = source(chat_id="-100-family", user_id="1001")

    assert resolve(mislabeled_group) is None
    wrong_platform = source(chat_id="1001", user_id="1001")
    wrong_platform.platform = Platform.SIGNAL
    assert resolve(wrong_platform) is None


def test_unauthorized_or_unconfigured_destination_has_no_context():
    assert resolve(source(), authorized=False) is None
    assert resolve(
        source(chat_id="-100-unknown", chat_type="group", memory_scope="general")
    ) is None


def test_group_topic_and_zone_are_exact_and_profile_cannot_widen_them():
    exact = resolve(
        source(
            chat_id="-100-family",
            chat_type="forum",
            thread_id="6827",
            memory_scope="boxmap",
            profile_name="BoxMap",
        )
    )

    assert exact is not None
    assert exact.destination.thread_id.startswith("t_")
    assert exact.read_wings[0].endswith(".boxmap")
    assert resolve(
        source(
            chat_id="-100-family",
            chat_type="forum",
            thread_id="6827",
            memory_scope="personal",
        )
    ) is None
    assert resolve(
        source(
            chat_id="-100-family",
            chat_type="forum",
            thread_id="wrong",
            memory_scope="boxmap",
        )
    ) is None


def test_message_provenance_is_required_for_write_but_not_read():
    context = resolve(source(message_id=None))

    assert context is not None
    assert context.source_ref is None
    assert context.write_wing is None
    assert context.read_wings


def test_context_contains_opaque_destination_actor_and_source_references():
    context = resolve(source())

    assert context is not None
    rendered = json.dumps(asdict(context), sort_keys=True)
    assert '"1001"' not in rendered
    assert '"55"' not in rendered
    assert context.destination.chat_id.startswith("c_")
    assert context.actor_ref.startswith("a_")
    assert context.source_ref is not None and context.source_ref.startswith("m_")


def test_context_is_frozen_and_cache_fingerprint_tracks_actor_and_policy():
    first = resolve(source())
    second = resolve(source())
    anna = resolve(source(chat_id="2002", user_id="2002"))

    assert first is not None and second is not None and anna is not None
    assert first.cache_fingerprint == second.cache_fingerprint
    assert first.cache_fingerprint != anna.cache_fingerprint
    with pytest.raises(FrozenInstanceError):
        first.principal_id = "p_n4v2c7b5x6m3q2rs"


def test_policy_fingerprint_is_canonical_and_changes_on_membership_revocation():
    raw = policy_dict()
    reordered = dict(reversed(list(raw.items())))
    assert parse_memory_access_policy(raw).fingerprint == parse_memory_access_policy(reordered).fingerprint

    revoked = policy_dict()
    revoked["principals"][0]["dm_rules"][0]["group_memberships"] = ["family"]
    assert parse_memory_access_policy(raw).fingerprint != parse_memory_access_policy(revoked).fingerprint


def test_membership_revocation_removes_federated_read_and_invalidates_context():
    from plugins.memory.mempalace.capability_client import (
        MemPalaceClientConfig,
        MemPalaceClientError,
        build_capability,
    )

    original_policy = parse_memory_access_policy(policy_dict())
    original = resolve_memory_access_context(
        source(),
        original_policy,
        request_authorized=True,
        principal_verified=True,
        opaque_id_key=OPAQUE_KEY,
    )

    revoked_raw = policy_dict()
    revoked_raw["principals"][0]["group_memberships"] = []
    revoked_raw["principals"][0]["dm_rules"][0]["group_memberships"] = []
    revoked_policy = parse_memory_access_policy(revoked_raw)
    revoked = resolve_memory_access_context(
        source(),
        revoked_policy,
        request_authorized=True,
        principal_verified=True,
        opaque_id_key=OPAQUE_KEY,
    )

    assert original is not None and revoked is not None
    assert any(wing.startswith("group.") for wing in original.read_wings)
    assert all(not wing.startswith("group.") for wing in revoked.read_wings)
    assert original.grants
    assert revoked.grants == ()
    assert original.policy_fingerprint != revoked.policy_fingerprint
    assert original.cache_fingerprint != revoked.cache_fingerprint

    current_client_config = MemPalaceClientConfig(
        base_url="https://memory.invalid",
        bearer_token="b" * 32,
        bearer_kid="hermes-memory-v1",
        issuer="hermes",
        audience="mempalace",
        hmac_secret="h" * 32,
        policy_fingerprint=revoked.policy_fingerprint,
    )
    with pytest.raises(MemPalaceClientError, match="stale_policy_fingerprint"):
        build_capability(
            original,
            current_client_config,
            operation="read",
            method="GET",
            raw_target="/wings",
            body=b"",
            now=100,
            jti="r_revocation00000001",
        )


def test_generated_principal_id_uses_documented_opaque_grammar():
    principal_id = generate_memory_principal_id()

    assert principal_id.startswith("p_")
    assert len(principal_id) == 18
    assert set(principal_id[2:]).issubset(set("abcdefghijklmnopqrstuvwxyz234567"))


def test_generated_opaque_key_round_trips_through_strict_decoder():
    encoded = generate_memory_opaque_id_key()

    assert len(decode_memory_opaque_id_key(encoded)) == 32
    with pytest.raises(ValueError, match="32..64 bytes"):
        decode_memory_opaque_id_key("c2hvcnQ")
    with pytest.raises(ValueError, match="canonical base64url"):
        decode_memory_opaque_id_key(f" {encoded}")


def test_default_root_policy_is_valid_and_fail_closed():
    from hermes_cli.config import DEFAULT_CONFIG

    policy = parse_memory_access_policy(DEFAULT_CONFIG["memory"]["access"])

    assert policy.principals == ()
    assert policy.groups == ()
    assert resolve_memory_access_context(
        source(),
        policy,
        request_authorized=True,
        principal_verified=True,
        opaque_id_key=OPAQUE_KEY,
    ) is None


def test_resolver_has_no_caller_supplied_principal_or_wing_parameters():
    parameters = inspect.signature(resolve_memory_access_context).parameters

    assert "principal_id" not in parameters
    assert "read_wings" not in parameters
    assert "write_wing" not in parameters


def test_opaque_references_are_domain_separated_by_reference_type():
    chat_digest = _opaque_ref(OPAQUE_KEY, "c", "same", "parts").split("_", 1)[1]
    actor_digest = _opaque_ref(OPAQUE_KEY, "a", "same", "parts").split("_", 1)[1]

    assert chat_digest != actor_digest


def test_authorization_flags_reject_truthy_non_booleans():
    policy = parse_memory_access_policy(policy_dict())
    assert resolve_memory_access_context(
        source(),
        policy,
        request_authorized=1,
        principal_verified=True,
        opaque_id_key=OPAQUE_KEY,
    ) is None
    assert resolve_memory_access_context(
        source(),
        policy,
        request_authorized=True,
        principal_verified=1,
        opaque_id_key=OPAQUE_KEY,
    ) is None


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda raw: raw.update({"unexpected": True}), "unknown keys"),
        (lambda raw: raw.update({"version": True}), "integer 1"),
        (lambda raw: raw["principals"][0].update({"id": "p_me"}), "opaque principal"),
        (lambda raw: raw["principals"][0]["identities"][0].update({"user_id": "*"}), "wildcards"),
        (
            lambda raw: raw["principals"][1]["identities"].append(
                {"platform": "telegram", "user_id": "1001"}
            ),
            "multiple principals",
        ),
        (
            lambda raw: raw["principals"][0]["group_memberships"].append("unknown"),
            "unknown group membership",
        ),
        (
            lambda raw: raw["principals"][0]["dm_rules"][0]["private_zones"].append("secret"),
            "widens private zones",
        ),
    ],
)
def test_policy_validation_is_strict_and_rejects_widening(mutate, match):
    raw = policy_dict()
    mutate(raw)

    with pytest.raises(ValueError, match=match):
        parse_memory_access_policy(raw)
