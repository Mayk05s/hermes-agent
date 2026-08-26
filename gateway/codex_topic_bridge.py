"""Direct Telegram-topic bridge to the local Codex app-server.

This module deliberately has no dependency on the Hermes agent runtime.  The
Telegram transport can hand an owner message to Codex before the normal Hermes
gateway callback is involved, while the resulting Codex thread is persisted in
the same ``CODEX_HOME`` used by the desktop app.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from utils import atomic_json_write, is_truthy_value


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Optional[Awaitable[None]]]
ThreadCreatedCallback = Callable[[str, str], Optional[Awaitable[None]]]


class CodexTopicBridgeError(RuntimeError):
    """A safe, user-displayable Codex bridge failure."""


@dataclass(frozen=True)
class DirectCodexTopicRoute:
    """Validated operator configuration for one Telegram forum topic."""

    chat_id: str
    thread_id: str
    owner_user_ids: frozenset[str]
    cwd: str
    title_prefix: str = "Tripio / system"
    model: Optional[str] = None
    effort: Optional[str] = None
    approval_policy: str = "never"
    sandbox: str = "dangerFullAccess"
    turn_timeout_seconds: float = 3600.0

    @property
    def key(self) -> str:
        return f"telegram:{self.chat_id}:{self.thread_id}"

    def authorizes(self, user_id: Any) -> bool:
        candidate = str(user_id or "").strip()
        return bool(candidate and candidate in self.owner_user_ids)


@dataclass(frozen=True)
class CodexTopicRunResult:
    thread_id: str
    text: str
    created: bool
    title: str


@dataclass
class _ActiveTurn:
    process: asyncio.subprocess.Process
    thread_id: str
    turn_id: str
    write_lock: asyncio.Lock


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = ()
    return [str(item).strip() for item in values if str(item or "").strip()]


def direct_codex_route_for_source(
    group_topics: Any,
    source: Any,
) -> Optional[DirectCodexTopicRoute]:
    """Resolve a fail-closed direct Codex route from Telegram topic config."""

    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if not chat_id or not thread_id or not isinstance(group_topics, list):
        return None

    for chat_entry in group_topics:
        if not isinstance(chat_entry, dict):
            continue
        if str(chat_entry.get("chat_id", "")).strip() != chat_id:
            continue
        topics = chat_entry.get("topics", [])
        if not isinstance(topics, list):
            return None
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            if str(topic.get("thread_id", "")).strip() != thread_id:
                continue
            raw = topic.get("direct_codex")
            if raw is True:
                raw = {"enabled": True}
            if not isinstance(raw, dict) or not is_truthy_value(raw.get("enabled"), False):
                return None

            owner_ids = frozenset(_string_list(raw.get("owner_user_ids")))
            cwd = str(raw.get("cwd") or "").strip()
            # A direct coding route is privileged.  Missing owners or an
            # implicit cwd must never degrade into broad access.
            if not owner_ids or not cwd or not os.path.isabs(cwd):
                logger.error(
                    "Direct Codex route %s:%s is claimed but locked: explicit "
                    "owner_user_ids and an absolute cwd are required",
                    chat_id,
                    thread_id,
                )
                # Return a claimed route with no authorized users instead of
                # ``None``.  ``None`` means ordinary Hermes routing; using it
                # for malformed privileged config would silently punch through
                # the intended isolation boundary.
                owner_ids = frozenset()

            try:
                timeout = float(raw.get("turn_timeout_seconds", 3600.0))
            except (TypeError, ValueError):
                timeout = 3600.0
            timeout = min(max(timeout, 30.0), 21600.0)

            model = str(raw.get("model") or "").strip() or None
            effort = str(raw.get("effort") or "").strip() or None
            return DirectCodexTopicRoute(
                chat_id=chat_id,
                thread_id=thread_id,
                owner_user_ids=owner_ids,
                cwd=cwd,
                title_prefix=str(raw.get("title_prefix") or topic.get("name") or "Tripio / system").strip(),
                model=model,
                effort=effort,
                approval_policy=str(raw.get("approval_policy") or "never").strip(),
                sandbox=str(raw.get("sandbox") or "dangerFullAccess").strip(),
                turn_timeout_seconds=timeout,
            )
        return None
    return None


def _default_state_path() -> Path:
    hermes_home = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    return hermes_home / "codex_topic_bridge.json"


def _safe_title(prefix: str, prompt: str) -> str:
    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    first_line = re.sub(r"\s+", " ", first_line)
    if len(first_line) > 72:
        first_line = f"{first_line[:69].rstrip()}..."
    return f"{prefix} - {first_line}" if first_line else prefix


def _thread_sandbox_mode(value: str) -> str:
    normalized = str(value or "").strip()
    return {
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }.get(normalized, normalized)


def _turn_sandbox_policy_type(value: str) -> str:
    normalized = str(value or "").strip()
    return {
        "read-only": "readOnly",
        "workspace-write": "workspaceWrite",
        "danger-full-access": "dangerFullAccess",
    }.get(normalized, normalized)


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


class CodexTopicBridge:
    """Small JSONL client for the locally managed Codex app-server."""

    def __init__(
        self,
        *,
        state_path: Optional[Path] = None,
        codex_binary: Optional[str] = None,
    ) -> None:
        self.state_path = Path(state_path) if state_path else _default_state_path()
        self.codex_binary = codex_binary or shutil.which("codex") or "codex"
        self._state_lock = asyncio.Lock()
        self._route_locks: dict[str, asyncio.Lock] = {}
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._state = self._load_state()
        self._request_id = 1000

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "routes": {}}
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring unreadable Codex topic bridge state", exc_info=True)
            return {"version": 1, "routes": {}}
        if not isinstance(data, dict) or not isinstance(data.get("routes"), dict):
            return {"version": 1, "routes": {}}
        return data

    def _save_state(self) -> None:
        atomic_json_write(
            self.state_path,
            self._state,
            mode=0o600,
        )

    def _route_record(self, route: DirectCodexTopicRoute) -> dict[str, Any]:
        record = self._state.setdefault("routes", {}).get(route.key, {})
        return record if isinstance(record, dict) else {}

    async def status(self, route: DirectCodexTopicRoute) -> dict[str, Any]:
        async with self._state_lock:
            record = dict(self._route_record(route))
        record["active"] = route.key in self._active_turns
        return record

    async def reset(self, route: DirectCodexTopicRoute) -> Optional[str]:
        if route.key in self._active_turns:
            raise CodexTopicBridgeError("Сначала остановите текущий ход командой /stop.")
        async with self._state_lock:
            record = self._state.setdefault("routes", {}).pop(route.key, None)
            self._save_state()
        if isinstance(record, dict):
            return str(record.get("thread_id") or "") or None
        return None

    async def interrupt(self, route: DirectCodexTopicRoute) -> bool:
        active = self._active_turns.get(route.key)
        if active is None:
            return False
        self._request_id += 1
        await self._send_json(
            active.process,
            {
                "method": "turn/interrupt",
                "id": self._request_id,
                "params": {"threadId": active.thread_id, "turnId": active.turn_id},
            },
            active.write_lock,
        )
        return True

    async def close(self) -> None:
        active_turns = list(self._active_turns.values())
        for active in active_turns:
            try:
                active.process.terminate()
            except ProcessLookupError:
                pass
        if active_turns:
            await asyncio.gather(
                *(active.process.wait() for active in active_turns),
                return_exceptions=True,
            )
        self._active_turns.clear()

    async def run_turn(
        self,
        route: DirectCodexTopicRoute,
        prompt: str,
        *,
        image_paths: Iterable[str] = (),
        attachment_paths: Iterable[str] = (),
        on_progress: Optional[ProgressCallback] = None,
        on_thread_created: Optional[ThreadCreatedCallback] = None,
    ) -> CodexTopicRunResult:
        lock = self._route_locks.setdefault(route.key, asyncio.Lock())
        async with lock:
            return await self._run_turn_locked(
                route,
                prompt,
                image_paths=image_paths,
                attachment_paths=attachment_paths,
                on_progress=on_progress,
                on_thread_created=on_thread_created,
            )

    async def _run_turn_locked(
        self,
        route: DirectCodexTopicRoute,
        prompt: str,
        *,
        image_paths: Iterable[str],
        attachment_paths: Iterable[str],
        on_progress: Optional[ProgressCallback],
        on_thread_created: Optional[ThreadCreatedCallback],
    ) -> CodexTopicRunResult:
        if not Path(route.cwd).is_dir():
            raise CodexTopicBridgeError(f"Рабочая папка Codex не найдена: {route.cwd}")

        async with self._state_lock:
            record = dict(self._route_record(route))
        previous_thread_id = str(record.get("thread_id") or "").strip()

        try:
            return await self._run_protocol(
                route,
                prompt,
                previous_thread_id=previous_thread_id,
                image_paths=image_paths,
                attachment_paths=attachment_paths,
                on_progress=on_progress,
                on_thread_created=on_thread_created,
            )
        except FileNotFoundError as exc:
            raise CodexTopicBridgeError(
                f"Codex CLI не найден: {self.codex_binary}"
            ) from exc

    async def _spawn_proxy(self) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            self.codex_binary,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise CodexTopicBridgeError("Не удалось открыть канал к Codex app-server.")
        return process

    async def _spawn_ready_proxy(self) -> asyncio.subprocess.Process:
        # A private stdio app-server process is independent from both Hermes
        # and the desktop app's own daemon, while sharing the same CODEX_HOME
        # thread store and login.  This avoids coupling the reserve route to
        # either long-running service.
        return await self._spawn_proxy()

    @staticmethod
    async def _send_json(
        process: asyncio.subprocess.Process,
        payload: dict[str, Any],
        write_lock: asyncio.Lock,
    ) -> None:
        if process.stdin is None:
            raise CodexTopicBridgeError("Канал записи Codex закрыт.")
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    @staticmethod
    async def _stderr_text(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        try:
            raw = await asyncio.wait_for(process.stderr.read(), timeout=1.0)
        except asyncio.TimeoutError:
            return ""
        return raw.decode("utf-8", errors="replace").strip()[-1000:]

    async def _read_json(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout: float,
        write_lock: asyncio.Lock,
    ) -> dict[str, Any]:
        if process.stdout is None:
            raise CodexTopicBridgeError("Канал чтения Codex закрыт.")
        while True:
            try:
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise CodexTopicBridgeError("Codex не ответил вовремя.") from exc
            if not raw:
                detail = await self._stderr_text(process)
                raise CodexTopicBridgeError(
                    f"Соединение с Codex app-server закрыто{': ' + detail if detail else '.'}"
                )
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("Ignoring non-JSON Codex app-server output: %r", raw[:300])
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                await self._answer_server_request(process, message, write_lock)
                continue
            return message

    async def _answer_server_request(
        self,
        process: asyncio.subprocess.Process,
        request: dict[str, Any],
        write_lock: asyncio.Lock,
    ) -> None:
        method = str(request.get("method") or "")
        request_id = request.get("id")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result: dict[str, Any] = {"decision": "acceptForSession"}
        elif method == "item/permissions/requestApproval":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            requested = params.get("permissions") if isinstance(params, dict) else None
            result = {"permissions": requested if isinstance(requested, dict) else {}, "scope": "session"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            await self._send_json(
                process,
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unsupported client request: {method}"},
                },
                write_lock,
            )
            return
        await self._send_json(process, {"id": request_id, "result": result}, write_lock)

    async def _request(
        self,
        process: asyncio.subprocess.Process,
        payload: dict[str, Any],
        *,
        timeout: float,
        write_lock: asyncio.Lock,
        on_notification: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> dict[str, Any]:
        request_id = payload.get("id")
        await self._send_json(process, payload, write_lock)
        while True:
            message = await self._read_json(process, timeout=timeout, write_lock=write_lock)
            if message.get("id") == request_id:
                return message
            if on_notification is not None and "method" in message:
                await on_notification(message)

    async def _run_protocol(
        self,
        route: DirectCodexTopicRoute,
        prompt: str,
        *,
        previous_thread_id: str,
        image_paths: Iterable[str],
        attachment_paths: Iterable[str],
        on_progress: Optional[ProgressCallback],
        on_thread_created: Optional[ThreadCreatedCallback],
    ) -> CodexTopicRunResult:
        process = await self._spawn_ready_proxy()
        write_lock = asyncio.Lock()
        created = False
        thread_id = previous_thread_id
        title = ""
        final_messages: list[str] = []
        last_commentary = ""
        turn_id = ""

        async def handle_notification(message: dict[str, Any]) -> None:
            nonlocal last_commentary
            if message.get("method") != "item/completed":
                return
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            item = params.get("item") if isinstance(params, dict) else None
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                return
            text = str(item.get("text") or "").strip()
            if not text:
                return
            phase = str(item.get("phase") or "")
            if phase == "commentary":
                if on_progress is not None and text != last_commentary:
                    last_commentary = text
                    await _maybe_await(on_progress(text))
                return
            final_messages.append(text)

        try:
            init = await self._request(
                process,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "tripio_system_bridge",
                            "title": "Tripio System Bridge",
                            "version": "1.0.0",
                        }
                    },
                },
                timeout=30.0,
                write_lock=write_lock,
                on_notification=handle_notification,
            )
            if init.get("error"):
                raise CodexTopicBridgeError(str(init["error"].get("message") or "Codex initialization failed"))
            await self._send_json(process, {"method": "initialized", "params": {}}, write_lock)

            if thread_id:
                resume = await self._request(
                    process,
                    {
                        "method": "thread/resume",
                        "id": 2,
                        "params": {
                            "threadId": thread_id,
                            "cwd": route.cwd,
                            "approvalPolicy": route.approval_policy,
                            "sandbox": _thread_sandbox_mode(route.sandbox),
                        },
                    },
                    timeout=30.0,
                    write_lock=write_lock,
                    on_notification=handle_notification,
                )
                if resume.get("error"):
                    logger.warning(
                        "Codex thread %s could not be resumed; creating a replacement: %s",
                        thread_id,
                        resume["error"],
                    )
                    thread_id = ""

            if not thread_id:
                start_params: dict[str, Any] = {
                    "cwd": route.cwd,
                    "approvalPolicy": route.approval_policy,
                    "sandbox": _thread_sandbox_mode(route.sandbox),
                    "serviceName": "tripio_system_bridge",
                }
                if route.model:
                    start_params["model"] = route.model
                start = await self._request(
                    process,
                    {"method": "thread/start", "id": 3, "params": start_params},
                    timeout=30.0,
                    write_lock=write_lock,
                    on_notification=handle_notification,
                )
                if start.get("error"):
                    raise CodexTopicBridgeError(str(start["error"].get("message") or "Codex thread creation failed"))
                thread = start.get("result", {}).get("thread", {})
                thread_id = str(thread.get("id") or "").strip()
                if not thread_id:
                    raise CodexTopicBridgeError("Codex не вернул ID новой задачи.")
                created = True
                title = _safe_title(route.title_prefix, prompt)
                named = await self._request(
                    process,
                    {
                        "method": "thread/name/set",
                        "id": 4,
                        "params": {"threadId": thread_id, "name": title},
                    },
                    timeout=30.0,
                    write_lock=write_lock,
                    on_notification=handle_notification,
                )
                if named.get("error"):
                    logger.warning("Could not name direct Codex thread %s: %s", thread_id, named["error"])
                async with self._state_lock:
                    self._state.setdefault("routes", {})[route.key] = {
                        "thread_id": thread_id,
                        "title": title,
                        "cwd": route.cwd,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self._save_state()
                if on_thread_created is not None:
                    await _maybe_await(on_thread_created(thread_id, title))
            else:
                title = str(record_title or "") if (record_title := self._route_record(route).get("title")) else route.title_prefix

            inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for path in image_paths:
                candidate = str(path or "").strip()
                if candidate:
                    inputs.append({"type": "localImage", "path": candidate})
            attachments = [str(path).strip() for path in attachment_paths if str(path or "").strip()]
            if attachments:
                inputs.append({
                    "type": "text",
                    "text": "Локальные файлы, приложенные пользователем:\n" + "\n".join(f"- {path}" for path in attachments),
                })

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": inputs,
                "cwd": route.cwd,
                "approvalPolicy": route.approval_policy,
                "sandboxPolicy": {"type": _turn_sandbox_policy_type(route.sandbox)},
            }
            if route.model:
                turn_params["model"] = route.model
            if route.effort:
                turn_params["effort"] = route.effort

            turn = await self._request(
                process,
                {"method": "turn/start", "id": 5, "params": turn_params},
                timeout=30.0,
                write_lock=write_lock,
                on_notification=handle_notification,
            )
            if turn.get("error"):
                raise CodexTopicBridgeError(str(turn["error"].get("message") or "Codex turn failed to start"))
            turn_id = str(turn.get("result", {}).get("turn", {}).get("id") or "").strip()
            if not turn_id:
                raise CodexTopicBridgeError("Codex не вернул ID хода.")
            self._active_turns[route.key] = _ActiveTurn(process, thread_id, turn_id, write_lock)

            while True:
                message = await self._read_json(
                    process,
                    timeout=route.turn_timeout_seconds,
                    write_lock=write_lock,
                )
                await handle_notification(message)
                if message.get("method") != "turn/completed":
                    continue
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                completed_turn = params.get("turn") if isinstance(params, dict) else None
                if not isinstance(completed_turn, dict) or str(completed_turn.get("id") or "") != turn_id:
                    continue
                status = str(completed_turn.get("status") or "")
                if status == "failed":
                    error = completed_turn.get("error") if isinstance(completed_turn.get("error"), dict) else {}
                    raise CodexTopicBridgeError(str(error.get("message") or "Ход Codex завершился с ошибкой."))
                if status == "interrupted":
                    raise CodexTopicBridgeError("Ход Codex остановлен.")
                break

            text = final_messages[-1].strip() if final_messages else "Готово. Подробности сохранены в задаче Codex."
            async with self._state_lock:
                current = self._state.setdefault("routes", {}).setdefault(route.key, {})
                current.update({
                    "thread_id": thread_id,
                    "title": title,
                    "cwd": route.cwd,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                self._save_state()
            return CodexTopicRunResult(thread_id=thread_id, text=text, created=created, title=title)
        finally:
            active = self._active_turns.get(route.key)
            if active is not None and active.process is process:
                self._active_turns.pop(route.key, None)
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                await process.wait()
