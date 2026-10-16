"""Immutable original-source handoff for opt-in external memory providers."""
from dataclasses import dataclass, replace
import json
import logging
from pathlib import Path
import time


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthenticatedMemoryIdentity:
    source_json: str
    source_config: str

    @property
    def source(self):
        from gateway.session import SessionSource
        return SessionSource.from_dict(json.loads(self.source_json))


@dataclass(frozen=True)
class AuthenticatedMemoryInput:
    source_json: str
    text: str
    received_at: float
    source_config: str

    @property
    def source(self):
        from gateway.session import SessionSource
        return SessionSource.from_dict(json.loads(self.source_json))


def capture_authenticated_memory(runner, event, source):
    """Called after gateway admission, before model/reply/history augmentation.

    Raw Telegram authorship is mandatory for initial capture. Restart work uses
    the already-sealed durable outbox, never a reconstructed attribution label.
    """
    existing = getattr(event, "authenticated_memory_input", None)
    if type(existing) is AuthenticatedMemoryInput:
        return existing
    constituents = getattr(event, "authenticated_memory_constituents", None)
    if isinstance(constituents, tuple) and constituents:
        bound_inputs, admissions = [], []
        for constituent in constituents:
            try:
                bound = capture_authenticated_memory(runner, constituent, source)
                if str(constituent.message_id) == str(event.message_id):
                    event.authenticated_memory_identity = getattr(constituent, "authenticated_memory_identity", None)
                    event.authenticated_memory_identity_receipt = getattr(constituent, "authenticated_memory_identity_receipt", None)
                if bound is not None:
                    bound_inputs.append(bound)
                    admissions.extend(getattr(constituent, "authenticated_memory_admissions", []))
            except Exception:
                continue
        event.authenticated_memory_inputs = tuple(bound_inputs)
        event.authenticated_memory_admissions = admissions
        # The merged event retains the first message's routing/reply anchor.
        first = next((v for v in bound_inputs if str(v.source.message_id) == str(event.message_id)), None)
        event.authenticated_memory_input = first
        return first
    if getattr(event, "internal", False) or getattr(event, "durable_recovery", False):
        return None
    raw = getattr(event, "raw_message", None)
    author = getattr(raw, "from_user", None)
    text = getattr(event, "authenticated_user_request_text", None)
    if not author or getattr(author, "is_bot", False):
        return None
    raw_chat = getattr(raw, "chat", None)
    if str(getattr(raw_chat, "id", "")) != str(source.chat_id) or str(getattr(raw, "message_id", "")) != str(event.message_id):
        return None
    if getattr(source.platform, "value", source.platform) != "telegram":
        return None
    # Match the trusted Telegram adapter's canonical destination, not a raw
    # reply UI anchor. Forum General omits its raw thread but routes as "1";
    # ordinary group/DM replies can carry a non-topic anchor which is dropped.
    raw_thread = getattr(raw, "message_thread_id", None)
    raw_kind = str(getattr(raw_chat, "type", "")).split(".")[-1].lower()
    raw_topic = bool(getattr(raw, "is_topic_message", False))
    forum = getattr(raw_chat, "is_forum", False) is True
    if forum and raw_kind not in {"group", "supergroup"}:
        return None
    if raw_kind in {"group", "supergroup"}:
        if source.chat_type != "group":
            return None
        if raw_thread is None or not (raw_topic or forum):
            raw_thread = "1" if forum else None
    elif raw_kind == "private":
        if source.chat_type != "dm":
            return None
        if not raw_topic:
            raw_thread = None
    if str(raw_thread or "") != str(source.thread_id or ""):
        return None
    if source.user_id and str(source.user_id) != str(author.id):
        return None
    source = replace(source, user_id=str(author.id), user_name=None, message_id=str(event.message_id))
    if not runner._is_user_authorized(source):
        return None
    profile = str(source.profile_name or "default")
    config = runner._load_profile_config_for_name(profile)
    route = runner._memory_provider_route_for_source(source, config, event.message_id)
    if route is None or route.context.actor_ref == "unknown":
        return None
    config_path = str(runner._profile_home_for_name(profile) / "config.yaml")
    from plugins.memory import load_memory_provider
    provider = load_memory_provider(route.provider).for_access_context(route.context, options=route.provider_options)
    # Authenticated identity is independent of eligible fact text. Media can
    # read the verified participant's memory but cannot create text evidence.
    identity = AuthenticatedMemoryIdentity(json.dumps(source.to_dict(), sort_keys=True), config_path)
    event.authenticated_memory_identity = identity
    seal_identity = getattr(provider, "seal_authenticated_identity", None)
    event.authenticated_memory_identity_receipt = seal_identity(identity) if callable(seal_identity) else None
    # Identity also authorizes profile/chat-scoped tools in new topics. It
    # does not grant fact ingestion or widen the topic's memory permissions.
    if not route.context.write_wing:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if any(getattr(raw, field, None) for field in ("forward_origin", "forward_date", "forward_from", "external_reply")):
        return None
    # Automatically generated transcripts/OCR and quoted material are not
    # authenticated typed assertions, even when identity itself is verified.
    raw_text = getattr(raw, "text", None) or getattr(raw, "caption", None)
    if not isinstance(raw_text, str) or text != raw_text:
        return None
    bound = AuthenticatedMemoryInput(json.dumps(source.to_dict(), sort_keys=True), text, time.time(), config_path)
    try:
        admission = provider.on_authenticated_event(bound)
    except Exception as exc:
        # A fact filter (notably credential_source_rejected) cannot revoke
        # the separately verified author. Keep the identity receipt available
        # to batch handoff and durable recovery, without admitting this text.
        # Exception messages may contain secrets; log only the exception type.
        logger.warning("Authenticated memory fact admission unavailable (%s)", type(exc).__name__)
        event.authenticated_memory_input = None
        return None
    event.authenticated_memory_input = bound
    event.authenticated_memory_admissions = [admission] if admission else []
    event.authenticated_memory_inputs = (bound,)
    return bound


def matching_input(value, source, event_message_id=None):
    if type(value) not in (AuthenticatedMemoryInput, AuthenticatedMemoryIdentity):
        return None
    actual = value.source
    for field in ("platform", "chat_id", "thread_id", "profile_name"):
        if (getattr(actual, field, None) or "") != (getattr(source, field, None) or ""):
            raise ValueError("Authenticated memory source mismatch")
    for field in ("user_id", "user_id_alt"):
        if getattr(source, field, None) and getattr(source, field) != getattr(actual, field, None):
            raise ValueError("Authenticated memory actor mismatch")
    expected_message = source.message_id or event_message_id
    if expected_message is not None and str(actual.message_id) != str(expected_message):
        raise ValueError("Authenticated memory event mismatch")
    return actual


def restore_authenticated_memory_identity(runner, event, source, metadata=None):
    """Recover only sealed identity for read access, never inferred fact text.

    Raw-less events and persisted sender labels are not proof. A valid receipt
    must match the exact event and its CURRENT gateway authorization and route.
    """
    if metadata is None:
        store = runner._durable_store()
        if store is None:
            return None
        inbox_id = getattr(event, "durable_inbox_id", None)
        inbox = store.get_inbox_event(inbox_id) if inbox_id else None
        if inbox is None and getattr(event, "durable_job_id", None):
            for item in store.inputs_for_job(event.durable_job_id):
                candidate = store.get_inbox_event(item["event_id"])
                if candidate is None:
                    continue
                expected = source.message_id or event.message_id
                if expected is not None and str(candidate.get("request_message_id")) != str(expected):
                    continue
                inbox = candidate
                break
        if inbox is None:
            return None
        metadata = json.loads(inbox.get("event_json") or "{}")
    receipts = [metadata.get("authenticated_memory_identity_receipt")]
    receipts.extend(metadata.get("authenticated_memory_admissions", []))
    for receipt in receipts:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("evidence"), dict):
            continue
        packet = receipt["evidence"]
        try:
            identity = AuthenticatedMemoryIdentity(json.dumps(packet["source"], sort_keys=True), packet["config_path"])
            actual = matching_input(identity, source, event.message_id)
            profile = str(actual.profile_name or "default")
            if identity.source_config != str(runner._profile_home_for_name(profile) / "config.yaml"):
                continue
            if not runner._is_user_authorized(actual):
                continue
            config = runner._load_profile_config_for_name(profile)
            route = runner._memory_provider_route_for_source(actual, config, actual.message_id)
            if route is None or route.context.grants:
                continue
            from plugins.memory import load_memory_provider
            provider = load_memory_provider(route.provider).for_access_context(route.context, options=route.provider_options)
            if provider.verify_authenticated_identity(receipt) is None:
                continue
            event.authenticated_memory_identity = identity
            event.authenticated_memory_identity_receipt = receipt
            return identity
        except (KeyError, ValueError, TypeError, OSError):
            continue
    return None
