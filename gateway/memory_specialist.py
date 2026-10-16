"""Narrow trusted participant-private purpose; never a model route selector."""
from dataclasses import replace
import json
from pathlib import Path

from gateway.memory_access import MemoryGrant, parse_memory_access_policy, _principal_for_source


def resolve_nutrition_route(source, config, *, parent_route):
    settings = ((config.get("memory") or {}).get("specialist_routes") or {}).get("nutrition") or {}
    if settings.get("enabled") is not True or parent_route is None:
        return None
    context = parent_route.context
    allowed = settings.get("participants") or {}
    principal_id = allowed.get(str(source.user_id or ""))
    if not principal_id or context.actor_ref == "unknown" or not context.source_ref or context.destination_kind != "group" or context.write_wing is None or context.grants:
        return None
    destinations = settings.get("destinations") or []
    actual = {"platform": source.platform.value, "chat_id": str(source.chat_id), "thread_id": str(source.thread_id or "")}
    if actual not in destinations:
        return None
    policy = parse_memory_access_policy(json.loads(Path(settings["policy_file"]).read_text()))
    principal = _principal_for_source(policy, source, source.platform.value)
    if principal is None or principal.id != principal_id or "nutrition" not in principal.private_zones or policy.fingerprint != context.policy_fingerprint:
        return None
    wing = f"private.{principal_id}.nutrition"
    return replace(parent_route, context=replace(context,
        authorization_subject=f"principal:{principal_id}", principal_id=principal_id,
        read_wings=(wing,), write_wing=wing, memory_scope="nutrition",
        grants=(MemoryGrant(id=context.source_ref, type="specialist_nutrition"),)))


def trusted_nutrition_child_route(parent, *, specialist, requested_scope):
    from gateway.memory_ingress import AuthenticatedMemoryIdentity, AuthenticatedMemoryInput
    from gateway.memory_routing import resolve_memory_provider_route, verified_memory_identity
    from gateway.pairing import PairingStore
    import yaml
    bound = getattr(parent, "_trusted_memory_input", None)
    writable = type(bound) is AuthenticatedMemoryInput
    if not writable:
        bound = getattr(parent, "_trusted_memory_identity", None)
    if specialist != "nutrition" or type(bound) not in (AuthenticatedMemoryInput, AuthenticatedMemoryIdentity):
        return None
    source = bound.source
    if requested_scope != f"hudeem-tripio-user-{source.user_id}":
        return None
    config = yaml.safe_load(Path(bound.source_config).read_text()) or {}
    pairing = PairingStore(hermes_home=Path(bound.source_config).parent)
    verified = verified_memory_identity(source, pairing)
    # The ordinary group's principal may be absent without individual DM
    # pairing. A fresh authenticated group source plus the exact registered
    # specialist-purpose mapping is its own narrow grant, not Personal access.
    route = resolve_memory_provider_route(source, config, request_authorized=True,
                                          principal_verified=verified)
    if route is None or route.context != getattr(parent, "_memory_access_context", None):
        return None
    child = resolve_nutrition_route(source, config, parent_route=route)
    if child is not None and not writable:
        child = replace(child, context=replace(child.context, write_wing=None))
    return child
