"""Scoped, source-backed Telegram feedback intake for GitHub issues.

Only durable messages from the authenticated current topic can become quotes
or attachments. Credentials stay behind fixed-repository server operations;
the model cannot select a credential, path, HTTP endpoint or remote URL.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from gateway.session_context import get_session_env
from hermes_constants import get_default_hermes_root
from tools.registry import registry

REPOS = {"lessonpilot": "Mayk05s/LessonPilot", "boxmap": "Mayk05s/BoxMap"}
MEDIA_BRANCH = "hermes-issue-media"
MAX_MEDIA_BYTES = 90 * 1024 * 1024
_REVIEWS = {}
_REVIEW_LOCK = threading.Lock()


def _body_hash(body):
    return hashlib.sha256((body or "").encode()).hexdigest()


def _open_issues(api):
    result = []
    for page in range(1, 11):
        rows = api.call("GET", f"issues?state=open&per_page=100&page={page}&sort=updated&direction=desc")
        result.extend(row for row in rows if "pull_request" not in row)
        if len(rows) < 100:
            return result
    raise ValueError("Open issue scan exceeded 1000 entries; narrow the repository backlog before creation")


def _snapshot(rows):
    return _body_hash(json.dumps(sorted([(r["number"], r["title"], r.get("body") or "", r.get("updated_at")) for r in rows]), ensure_ascii=False))


def _review_scope(scope, project):
    return (*scope, project, _body_hash(str(get_session_env("HERMES_SESSION_USER_REQUEST", "") or "")))


def _check_review(args, scope, api):
    with _REVIEW_LOCK:
        record = _REVIEWS.get(str(args.get("review_token") or ""))
    if not record or record["expires"] < time.monotonic() or record["scope"] != _review_scope(scope, args["project"]):
        raise ValueError("Before creating an issue, call find and review suitable existing issues in this request")
    reason = args.get("new_issue_reason")
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
        raise ValueError("Explain briefly why none of the reviewed issues fits; otherwise append to the suitable issue")
    if _snapshot(_open_issues(api)) != record["snapshot"]:
        raise ValueError("Open issues changed since review; call find again before creating")


def _update_issue(api, args):
    number = args.get("issue_number")
    body = args.get("body")
    title = args.get("title")
    state = args.get("state")
    reason = args.get("state_reason")
    if type(number) is not int or number < 1:
        raise ValueError("Provide a positive issue_number")
    if body is not None and (not isinstance(body, str) or len(body) > 65000):
        raise ValueError("Provide the complete updated body, up to 65000 characters")
    if title is not None and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 160 or "\n" in title):
        raise ValueError("Invalid updated title")
    if state is not None and state not in {"open", "closed"}:
        raise ValueError("For update choose state open or closed; all is only a list filter")
    if reason is not None and reason not in {"completed", "not_planned", "reopened"}:
        raise ValueError("Invalid state_reason")
    if reason is not None and ((reason == "reopened" and state != "open") or (reason != "reopened" and state != "closed")):
        raise ValueError("state_reason requires a matching explicit state")
    payload = {}
    if body is not None:
        payload["body"] = body
    if title is not None:
        payload["title"] = title.strip()
    if state is not None:
        payload["state"] = state
    if reason is not None:
        payload["state_reason"] = reason
    if not payload:
        raise ValueError("Provide at least one change: body, title or state")
    before = api.call("GET", f"issues/{number}")
    if "pull_request" in before:
        raise ValueError("Cannot edit a pull request through issue intake")
    if not args.get("expected_body_sha256") or args["expected_body_sha256"] != _body_hash(before.get("body")):
        raise ValueError("Issue changed or read evidence is missing; read it again before updating")
    if args.get("expected_updated_at") != before.get("updated_at"):
        raise ValueError("Issue changed since reading; read it again before updating")
    if all((before.get(key) or "" if key == "body" else before.get(key)) == value for key, value in payload.items()):
        return {"success": True, "updated": False, "already_current": True, "readback_verified": True,
                "state": before["state"], "url": before["html_url"], "number": number}
    api.call("PATCH", f"issues/{number}", payload)
    verified = api.call("GET", f"issues/{number}")
    ok = all(verified.get(key) == value for key, value in payload.items())
    return {"success": ok, "updated": True, "readback_verified": ok,
            "state": verified["state"], "url": verified["html_url"], "number": number,
            **({} if ok else {"error": "Updated but read-back differs; inspect existing issue instead of recreating"})}


def _append_body(before, draft, media):
    """Preserve the existing issue and append source-backed points without resets."""
    body = before or ""
    marker = f'<!-- hermes-issue-intake:{draft["key"]} -->'
    if marker in body:
        return body
    refs = _references(draft, body)
    matches = _task_matches(body)
    for match in reversed(matches):
        original = re.sub(r" \(сообщени[ея].*\)$", "", match[2]).strip().casefold()
        for index, item in enumerate(draft["items"]):
            if original == item.strip().casefold():
                merged = _merge_item_refs(match[2], _item_refs(draft, index, refs))
                body = body[:match.start(2)]+merged+body[match.end(2):]
    matches = _task_matches(body)
    existing = {re.sub(r" \(сообщени[ея].*\)$", "", m[2]).strip().casefold() for m in matches}
    items = [(index, item) for index, item in enumerate(draft["items"]) if item.strip().casefold() not in existing]
    start_number = max([int(m[1]) for m in matches], default=0)
    points = "\n".join(f"- [ ] {start_number+i}. {item}{_item_refs(draft, index, refs)}" for i, (index, item) in enumerate(items, 1))
    if points:
        at = matches[-1].end() if matches else len(body.rstrip())
        body = body[:at] + "\n" + points + body[at:]
    new_messages = [message for message in draft["messages"] if message["url"] not in body]
    quote_lines = _quote_lines(new_messages, refs)
    spoiler = "\n".join(["<details>", "<summary>Исходные сообщения целиком</summary>", "", *quote_lines, "</details>"])
    if new_messages:
        current = re.search(r"<details>\s*<summary>Исходные сообщения целиком</summary>.*?</details>", body, re.S)
        if current:
            inside = spoiler[spoiler.index("</summary>")+len("</summary>"):spoiler.rindex("</details>")]
            at = current.end()-len("</details>")
            body = body[:at]+inside+body[at:]
        else:
            body = body.rstrip()+"\n\n"+spoiler
    if media:
        body = body.rstrip()+"\n\n"+"\n".join(_media_lines(draft, media, refs))
    for entry in refs["media"].values():
        if not entry.get("tasks"):
            continue  # Preserve legacy captions whose task was never recorded.
        names = "(?:Скрин|Screen)" if entry["kind"] == "Скрин" else "Файл"
        # Refresh caption text only; keep original asset URLs and media numbers.
        body = re.sub(rf'{names} {entry["number"]} — [^\]\n*]+(?=\*\*|\]\()', lambda _: _media_caption(entry), body)
    body = re.sub(r"\n?<!-- hermes-issue-refs:[A-Za-z0-9+/=]+ -->", "", body)
    body = body.rstrip()+"\n\n"+marker+"\n"+_refs_marker(refs)
    if len(body) > 65000:
        raise ValueError("Combined issue exceeds size limit; do not truncate existing text or quotes")
    return body


def _scope(project):
    if project not in REPOS:
        raise ValueError("Choose project lessonpilot or boxmap")
    get = lambda name: str(get_session_env(name, "") or "").strip()
    profile, chat, thread = (get("HERMES_SESSION_PROFILE_NAME"),
                             get("HERMES_SESSION_CHAT_ID"),
                             get("HERMES_SESSION_THREAD_ID"))
    if get("HERMES_SESSION_PLATFORM") != "telegram":
        raise ValueError("Issue intake requires a trusted Telegram source")
    family = profile == "family-chat" and chat == "-1003966683704"
    boxmap = (profile in {"personal", "boxmap"} and
              chat == "-1003735932411" and thread == "6827")
    if not ((project == "lessonpilot" and family) or
            (project == "boxmap" and boxmap)):
        raise ValueError("Project is not enabled in this chat/profile/topic")
    return profile, chat, thread


def _root():
    return get_default_hermes_root()


def _db_path():
    return _root() / "gateway_jobs.sqlite3"


def _messages(scope, ids=None, limit=60):
    profile, chat, thread = scope
    if ids is not None:
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError("Select 1 to 100 source message IDs")
        ids = list(dict.fromkeys(str(x) for x in ids))
        if any(not re.fullmatch(r"\d+", x) for x in ids):
            raise ValueError("Invalid Telegram message ID")
    with sqlite3.connect(_db_path().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        query = """SELECT request_message_id, request_text, media_json, message_type,
                          source_json, created_at
                     FROM gateway_inbox_events
                    WHERE platform='telegram' AND json_valid(source_json)
                      AND json_extract(source_json, '$.profile_name')=?
                      AND json_extract(source_json, '$.chat_id')=?
                      AND COALESCE(json_extract(source_json, '$.thread_id'),'')=?
                      AND created_at<=?"""
        params = [profile, chat, thread, time.time()]
        if ids is not None:
            query += " AND request_message_id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(300 if ids is not None else min(max(int(limit), 1), 100))
        rows = db.execute(query, params).fetchall()
    result, seen = [], set()
    for row in rows:
        mid = str(row["request_message_id"] or "")
        if not re.fullmatch(r"\d+", mid) or mid in seen:
            continue
        seen.add(mid)
        source = json.loads(row["source_json"])
        media = json.loads(row["media_json"] or "[]")
        if not isinstance(media, list):
            raise ValueError("Invalid saved attachments; cannot publish incomplete feedback")
        result.append({"message_id": mid, "text": row["request_text"],
                       "author": str(source.get("user_name") or source.get("user_id") or ""),
                       "media": media, "created_at": row["created_at"],
                       "missing_media": not media and str(row["message_type"]) in {"photo", "image", "video", "audio", "voice", "document", "animation"},
                       "url": f"https://t.me/c/{chat.removeprefix('-100')}/{mid}"})
    if ids is not None and set(ids) != seen:
        raise ValueError("Source messages unavailable in this exact chat/topic/profile")
    return sorted(result, key=lambda x: (x["created_at"], int(x["message_id"])))


def _token_path(project):
    folder = _root() / "git-access" / REPOS[project].replace("/", "__")
    path = folder / "issues_token"
    # The already configured BoxMap credential stays in its existing scope.
    if project == "boxmap" and not path.exists():
        return folder / "pr_token"
    return path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, project):
        path = _token_path(project)
        self.repo = REPOS[project]
        self.cli = None
        if not path.is_file():
            selector = path.parent / "auth.json"
            if selector.is_file() and json.loads(selector.read_text()).get("method") == "gh-cli":
                self.cli = shutil.which("gh")
                if not self.cli or not (_root().parent / ".config/gh/hosts.yml").is_file():
                    raise ValueError("Configured server GitHub CLI authorization is unavailable")
                return
            raise ValueError(f"Missing repository credential: {path}; needs Issues write and Contents write for media")
        if path.stat().st_mode & 0o077:
            raise ValueError("Repository credential must have mode 600")
        self.token = path.read_text().strip()
        if not self.token:
            raise ValueError("Repository credential is empty")

    def _cli_call(self, method, endpoint, payload=None, missing_ok=False):
        """Use existing server OAuth without reading/exporting its credential.

        Only an operator-owned per-repository selector enables this transport.
        The model never gets gh, terminal, arbitrary API paths or the token.
        """
        accept = "application/vnd.github.object+json" if method == "GET" and endpoint.startswith("contents/") else "application/vnd.github+json"
        command = [self.cli, "api", "--hostname", "github.com", "--include", "--method", method,
                   "-H", "Accept: " + accept, "-H", "X-GitHub-Api-Version: 2022-11-28",
                   ("repos/" + self.repo + "/" + endpoint).rstrip("/")]
        if payload is not None:
            command.extend(["--input", "-"])
        env = {k: v for k, v in os.environ.items() if k not in {
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
            "GH_HOST", "GH_DEBUG", "GH_FORCE_TTY"}}
        env["GH_CONFIG_DIR"] = str(_root().parent / ".config/gh")
        env["GH_PROMPT_DISABLED"] = "1"
        completed = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                                   text=True, capture_output=True, timeout=60, env=env, check=False)
        match = re.match(r"HTTP/\S+\s+(\d{3})", completed.stdout)
        status = int(match.group(1)) if match else 0
        if status == 404 and missing_ok:
            return None
        if completed.returncode or not 200 <= status < 300:
            raise ValueError(f"Server GitHub CLI returned HTTP {status or 'unknown'} for this repository operation")
        _, separator, body = completed.stdout.partition("\n\n")
        if not separator:
            raise ValueError("Server GitHub CLI returned an invalid response")
        return json.loads(body)

    def call(self, method, endpoint, payload=None, missing_ok=False):
        if self.cli:
            return self._cli_call(method, endpoint, payload, missing_ok)
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{endpoint}",
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": ("application/vnd.github.object+json" if method == "GET" and endpoint.startswith("contents/") else "application/vnd.github+json"),
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json", "User-Agent": "Hermes-Issue-Intake"})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise ValueError(f"GitHub returned HTTP {exc.code} for this repository operation") from None


def _draft(args, scope):
    title, summary, items = args.get("title", ""), args.get("summary", ""), args.get("items")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160 or "\n" in title:
        raise ValueError("Provide a short issue title (up to 160 characters)")
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 600:
        raise ValueError("Provide a short plain-language description (up to 600 characters)")
    if (not isinstance(items, list) or not 1 <= len(items) <= 30 or
            any(not isinstance(x, str) or not x.strip() or len(x) > 300 or "\n" in x for x in items)):
        raise ValueError("Provide 1 to 30 short single-line action items")
    if not args.get("message_ids"):
        raise ValueError("Explicit source message IDs are required for preview/publish")
    problem = str(args.get("problem") or "").strip().lower()
    if not problem or len(problem) > 80:
        raise ValueError("Provide a stable problem group, e.g. materials or lesson-board")
    messages = _messages(scope, args["message_ids"])
    sources = args.get("item_sources")
    selected = {message["message_id"] for message in messages}
    if sources is None and len(messages) == 1:
        sources = [[messages[0]["message_id"]] for _ in items]
    if (not isinstance(sources, list) or len(sources) != len(items) or
            any(not isinstance(group, list) or not group or any(str(mid) not in selected for mid in group) for group in sources)):
        raise ValueError("Provide item_sources: one nonempty array of selected message IDs for each task item")
    identity = json.dumps([REPOS[args["project"]], scope, problem,
                           [x["message_id"] for x in messages]], ensure_ascii=False)
    return {"title": title.strip(), "summary": summary.strip(), "items": [x.strip() for x in items],
            "messages": messages, "item_sources": [[str(mid) for mid in group] for group in sources],
            "key": hashlib.sha256(identity.encode()).hexdigest()}


def _references(draft, before=""):
    markers = re.findall(r"<!-- hermes-issue-refs:([A-Za-z0-9+/=]+) -->", before)
    refs = json.loads(base64.b64decode(markers[-1])) if markers else {"messages": {}, "media": {}}
    for message in draft["messages"]:
        url = message["url"]
        if url not in refs["messages"]:
            refs["messages"][url] = max(refs["messages"].values(), default=0)+1
        for index, attachment in enumerate(message.get("media", []), 1):
            key = f"{url}|{index}"
            mime = str(attachment.get("type") or mimetypes.guess_type(str(attachment.get("path", "")))[0] or "")
            kind = "Скрин" if mime.startswith("image/") and not str(attachment.get("path", "")).lower().endswith(".svg") else "Файл"
            if key not in refs["media"]:
                number = max((m["number"] for m in refs["media"].values() if m["kind"] == kind), default=0)+1
                refs["media"][key] = {"kind": kind, "number": number, "message": refs["messages"][url]}
    refs.setdefault("tasks", {})
    for match in _task_matches(before):
        text = re.sub(r" \(сообщени[ея].*\)$", "", match[2]).strip().casefold()
        refs["tasks"][text] = int(match[1])
        for kind, pattern in [("Скрин", "скрины?"), ("Файл", "файлы?")]:
            numbers = {int(number) for group in re.findall(pattern+r" ([\d, ]+)", match[2]) for number in re.findall(r"\d+", group)}
            for entry in refs["media"].values():
                if entry["kind"] == kind and entry["number"] in numbers:
                    entry["tasks"] = sorted(set(entry.get("tasks", [])) | {int(match[1])})
    for index, item in enumerate(draft["items"]):
        key = item.strip().casefold()
        if key not in refs["tasks"]:
            refs["tasks"][key] = max(refs["tasks"].values(), default=0)+1
        ids = draft.get("item_sources", [[message["message_id"] for message in draft["messages"]] for _ in draft["items"]])[index]
        for message in draft["messages"]:
            if message["message_id"] in ids:
                for media_key, entry in refs["media"].items():
                    if media_key.rsplit("|", 1)[0] == message["url"]:
                        entry["tasks"] = sorted(set(entry.get("tasks", [])) | {refs["tasks"][key]})
    return refs


def _refs_marker(refs):
    encoded = base64.b64encode(json.dumps(refs, ensure_ascii=False).encode()).decode()
    return f"<!-- hermes-issue-refs:{encoded} -->"


def _reference_text(messages, refs):
    urls = [message["url"] for message in messages]
    numbers = sorted({refs["messages"][url] for url in urls})
    parts = [("сообщение " if len(numbers) == 1 else "сообщения ")+", ".join(map(str, numbers))]
    for kind, singular, plural in [("Скрин", "скрин", "скрины"), ("Файл", "файл", "файлы")]:
        media = sorted({value["number"] for key, value in refs["media"].items() if key.rsplit("|", 1)[0] in urls and value["kind"] == kind})
        if media:
            parts.append((singular if len(media) == 1 else plural)+" "+", ".join(map(str, media)))
    return "; ".join(parts)


def _item_refs(draft, index, refs):
    ids = draft.get("item_sources", [[message["message_id"] for message in draft["messages"]] for _ in draft["items"]])[index]
    messages = [message for message in draft["messages"] if message["message_id"] in ids]
    return " ("+_reference_text(messages, refs)+")"


def _task_matches(body):
    # Quoted checkboxes must never affect task numbering or task edits.
    ranges = [(m.start(), m.end()) for m in re.finditer(r"<details\b.*?</details>", body, re.S | re.I)]
    return [m for m in re.finditer(r"(?m)^- \[[ xX]\] (\d+)\. (.+)$", body)
            if not any(start <= m.start() < end for start, end in ranges)]


def _merge_item_refs(text, suffix):
    old = re.search(r" \((сообщени[ея] \d[^()]*)\)$", text)
    combined = (old[1] if old else "")+suffix
    parts = []
    for pattern, singular, plural in [("сообщени[ея]", "сообщение", "сообщения"), ("скрины?", "скрин", "скрины"), ("файлы?", "файл", "файлы")]:
        numbers = sorted({int(number) for group in re.findall(pattern+r" ([\d, ]+)", combined) for number in re.findall(r"\d+", group)})
        if numbers:
            parts.append((singular if len(numbers) == 1 else plural)+" "+", ".join(map(str, numbers)))
    return (text[:old.start()] if old else text)+" ("+"; ".join(parts)+")"


def _quote_lines(messages, refs):
    lines = []
    for message in messages:
        number = refs["messages"][message["url"]]
        media = _reference_text([message], refs).split("; ", 1)
        suffix = " ("+media[1]+")" if len(media) > 1 else ""
        lines.extend([f'[Сообщение {number}]({message["url"]}){suffix}', "",
                      "<blockquote><pre>"+html.escape(message["text"] or "[Сообщение без текста]")+"</pre></blockquote>", ""])
    return lines


def _media_caption(entry):
    tasks = entry.get("tasks", [])
    if not tasks:
        raise ValueError("Every attachment must be linked to at least one task through item_sources")
    kind = "Screen" if entry["kind"] == "Скрин" else entry["kind"]
    task_label = "Задача" if len(tasks) == 1 else "Задачи"
    return f'{kind} {entry["number"]} — {task_label} {", ".join(map(str, tasks))} (Сообщение {entry["message"]})'


def _media_lines(draft, media, refs):
    lines = ["Медиа:", ""]
    counts = {}
    for item in media:
        mid = item["message_id"]
        counts[mid] = counts.get(mid, 0)+1
        message = next(message for message in draft["messages"] if message["message_id"] == mid)
        entry = refs["media"][f'{message["url"]}|{item.get("attachment_index", counts[mid])}']
        label = _media_caption(entry)
        lines.extend([f'**{label}** · [Исходное сообщение]({message["url"]})', ""])
        if item.get("url"):
            lines.append(f'{"!" if item["image"] else ""}[{label}]({item["url"]})')
            lines.append(f'[Открыть оригинал]({item["url"].removesuffix("?raw=true")})')
        else:
            lines.append("Оригинал будет приложен при публикации.")
        lines.append("")
    return lines


def _render(draft, media):
    refs = _references(draft)
    lines = [draft["summary"], "", "<details>", "<summary>Исходные сообщения целиком</summary>", ""]
    lines.extend(_quote_lines(draft["messages"], refs))
    lines.extend(["</details>", ""])
    lines.extend(f"- [ ] {index}. {item}{_item_refs(draft, index-1, refs)}" for index, item in enumerate(draft["items"], 1))
    if media:
        lines.extend(["", *_media_lines(draft, media, refs)])
    lines.extend(["", f'<!-- hermes-issue-intake:{draft["key"]} -->', _refs_marker(refs)])
    body = "\n".join(lines)
    if len(body) > 65000:
        raise ValueError("Quotes exceed issue size; split the group without truncating quotes")
    return body


def _attachments(draft):
    files = []
    for message in draft["messages"]:
        if message.get("missing_media"):
            raise ValueError("An original media message has no saved attachment; recover it before publishing")
        for attachment_index, attachment in enumerate(message["media"], 1):
            if not isinstance(attachment, dict) or not attachment.get("path"):
                raise ValueError("An original attachment has no saved file; recover it before publishing")
            path = Path(attachment["path"]).resolve()
            roots = [_root() / name for name in (
                "image_cache", "audio_cache", "video_cache", "document_cache",
                "cache/images", "cache/audio", "cache/videos", "cache/documents")]
            if not any(path.is_relative_to(root.resolve()) for root in roots):
                raise ValueError("Attachment is outside the Telegram media cache")
            if not path.is_file() or path.stat().st_size > MAX_MEDIA_BYTES:
                raise ValueError("An attachment is missing or exceeds 90 MiB; do not silently omit it")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            mime = str(attachment.get("type") or mimetypes.guess_type(path.name)[0] or "")
            suffix = path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".bin"
            files.append({"message_id": message["message_id"], "attachment_index": attachment_index, "content": content,
                          "path": f"telegram/{draft['key']}/{digest}{suffix}",
                          "image": mime.startswith("image/") and suffix != ".svg"})
    return files


def _upload(api, files):
    if not files:
        return []
    branch = api.call("GET", f"git/ref/heads/{MEDIA_BRANCH}", missing_ok=True)
    if branch is None:
        repo = api.call("GET", "")
        base = urllib.parse.quote(repo["default_branch"], safe="")
        ref = api.call("GET", f"git/ref/heads/{base}")
        api.call("POST", "git/refs", {"ref": f"refs/heads/{MEDIA_BRANCH}", "sha": ref["object"]["sha"]})
    uploaded = []
    for item in files:
        path = urllib.parse.quote(item["path"], safe="/")
        existing = api.call("GET", f"contents/{path}?ref={MEDIA_BRANCH}", missing_ok=True)
        if existing is None:
            api.call("PUT", f"contents/{path}", {"branch": MEDIA_BRANCH,
                     "message": "Attach Telegram feedback media",
                     "content": base64.b64encode(item["content"]).decode()})
        verified = api.call("GET", f"contents/{path}?ref={MEDIA_BRANCH}")
        expected = hashlib.sha1(b"blob " + str(len(item["content"])).encode() + b"\0" + item["content"]).hexdigest()
        if verified.get("sha") != expected:
            raise ValueError("Uploaded attachment read-back failed")
        uploaded.append({"message_id": item["message_id"], "attachment_index": item.get("attachment_index", 1), "image": item["image"],
                         "url": f"https://github.com/{api.repo}/blob/{MEDIA_BRANCH}/{path}?raw=true"})
    return uploaded


def _find_duplicate(api, key):
    marker = f"<!-- hermes-issue-intake:{key} -->"
    for page in range(1, 11):
        issues = api.call("GET", f"issues?state=all&per_page=100&page={page}&sort=created&direction=desc")
        for issue in issues:
            if "pull_request" not in issue and marker in (issue.get("body") or ""):
                return issue
        if len(issues) < 100:
            return None
    raise ValueError("Duplicate search exceeded 1000 issues; reconcile before publishing")


def github_issue_intake(args):
    try:
        project = args.get("project")
        scope = _scope(project)
        action = args.get("action", "sources")
        if action == "sources":
            return json.dumps({"success": True, "repository": REPOS[project],
                               "messages": _messages(scope, args.get("message_ids"), args.get("limit", 60))}, ensure_ascii=False)
        if action == "status":
            api = GitHub(project)
            metadata = api.call("GET", "")
            api.call("GET", "issues?per_page=1")
            return json.dumps({"success": True, "repository": api.repo,
                               "has_issues": metadata.get("has_issues"),
                               "read_verified": True, "write_verified": False})
        if action == "find":
            api = GitHub(project)
            rows = _open_issues(api)
            if len(rows) > 100:
                raise ValueError("More than 100 open issues: read/reconcile the backlog before automatic creation")
            token = secrets.token_urlsafe(24)
            with _REVIEW_LOCK:
                now = time.monotonic()
                for expired in [key for key, value in _REVIEWS.items() if value["expires"] < now]:
                    del _REVIEWS[expired]
                _REVIEWS[token] = {"scope": _review_scope(scope, project), "snapshot": _snapshot(rows), "expires": now+600}
            return json.dumps({"success": True, "repository": api.repo, "review_token": token,
                               "instruction": "Compare existing issues by meaning. Read suitable candidates fully. If one fits, append to it; create only when none fits, using this token and a short reason.",
                               "issues": [{"number": row["number"], "title": row["title"],
                                           "body": row.get("body") or "", "url": row["html_url"]} for row in rows]}, ensure_ascii=False)
        if action == "list":
            state = args.get("state", "open")
            page = args.get("page", 1)
            limit = args.get("limit", 30)
            if state not in {"open", "closed", "all"}:
                raise ValueError("Choose issue state open, closed or all")
            if type(page) is not int or not 1 <= page <= 1000 or type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("Invalid issue page or limit")
            api = GitHub(project)
            rows = api.call("GET", f"issues?state={state}&per_page={limit}&page={page}&sort=updated&direction=desc")
            issues = [{"number": row["number"], "title": row["title"], "state": row["state"],
                       "url": row["html_url"], "comments": row.get("comments", 0),
                       "labels": [label["name"] for label in row.get("labels", [])],
                       "updated_at": row.get("updated_at")}
                      for row in rows if "pull_request" not in row]
            return json.dumps({"success": True, "repository": api.repo, "issues": issues,
                               "page": page, "has_more": len(rows) == limit}, ensure_ascii=False)
        if action == "read":
            number = args.get("issue_number")
            page = args.get("page", 1)
            if type(number) is not int or number < 1 or type(page) is not int or not 1 <= page <= 1000:
                raise ValueError("Provide a positive issue_number and valid comments page")
            api = GitHub(project)
            issue = api.call("GET", f"issues/{number}")
            if "pull_request" in issue:
                raise ValueError("This number belongs to a pull request, not an issue")
            comments = api.call("GET", f"issues/{number}/comments?per_page=100&page={page}")
            return json.dumps({"success": True, "repository": api.repo,
                               "issue": {"number": issue["number"], "title": issue["title"],
                                         "state": issue["state"], "state_reason": issue.get("state_reason"), "body": issue.get("body") or "",
                                         "body_sha256": _body_hash(issue.get("body")), "updated_at": issue.get("updated_at"),
                                         "url": issue["html_url"],
                                         "labels": [label["name"] for label in issue.get("labels", [])]},
                               "comments": [{"body": comment.get("body") or "", "url": comment["html_url"],
                                             "author": (comment.get("user") or {}).get("login")}
                                            for comment in comments],
                               "comments_page": page, "has_more_comments": len(comments) == 100}, ensure_ascii=False)
        if action == "update":
            api = GitHub(project)
            with (_token_path(project).parent / "issue-intake.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return json.dumps(_update_issue(api, args), ensure_ascii=False)
        if action == "append":
            number = args.get("issue_number")
            if type(number) is not int or number < 1:
                raise ValueError("Provide issue_number to append feedback")
            draft = _draft(args, scope)
            files = _attachments(draft)
            api = GitHub(project)
            with (_token_path(project).parent / "issue-intake.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                before = api.call("GET", f"issues/{number}")
                if "pull_request" in before or before.get("state") != "open":
                    raise ValueError("Feedback may be appended only to an open issue, not a pull request")
                marker = f'<!-- hermes-issue-intake:{draft["key"]} -->'
                if marker in (before.get("body") or ""):
                    return json.dumps({"success": True, "updated": False, "already_exists": True,
                                       "number": number, "url": before["html_url"]})
                if args.get("expected_body_sha256") != _body_hash(before.get("body")) or args.get("expected_updated_at") != before.get("updated_at"):
                    raise ValueError("Read the target issue again before appending; it changed or read evidence is missing")
                _append_body(before.get("body"), draft, [{"message_id": f["message_id"], "attachment_index": f["attachment_index"], "image": f["image"],
                            "url": f"https://github.com/{api.repo}/blob/{MEDIA_BRANCH}/{f['path']}?raw=true"} for f in files])
                body = _append_body(before.get("body"), draft, _upload(api, files))
                result = _update_issue(api, {**args, "body": body, "title": None, "state": None, "state_reason": None})
                result["media_count"] = len(files)
                return json.dumps(result, ensure_ascii=False)
        if action not in {"preview", "publish"}:
            raise ValueError("Unknown intake action")
        draft = _draft(args, scope)
        files = _attachments(draft)
        if action == "preview":
            return json.dumps({"success": True, "title": draft["title"],
                               "body": _render(draft, [{"message_id": f["message_id"], "attachment_index": f["attachment_index"], "image": f["image"], "url": ""} for f in files]), "media_count": len(files),
                               "published": False}, ensure_ascii=False)
        api = GitHub(project)
        with (_token_path(project).parent / "issue-intake.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            duplicate = _find_duplicate(api, draft["key"])
            if duplicate:
                return json.dumps({"success": True, "created": False, "already_exists": True,
                                   "url": duplicate["html_url"], "number": duplicate["number"]})
            _check_review(args, scope, api)
            _render(draft, [{"message_id": f["message_id"], "attachment_index": f["attachment_index"], "image": f["image"],
                            "url": f"https://github.com/{api.repo}/blob/{MEDIA_BRANCH}/{f['path']}?raw=true"} for f in files])
            body = _render(draft, _upload(api, files))
            issue = api.call("POST", "issues", {"title": draft["title"], "body": body})
            number = issue["number"]
            verified = api.call("GET", f"issues/{number}")
            if verified.get("body") != body or verified.get("title") != draft["title"]:
                return json.dumps({"success": False, "created": True, "readback_verified": False,
                                   "url": issue.get("html_url"), "number": number,
                                   "error": "Created but contents failed read-back; do not recreate"})
            return json.dumps({"success": True, "created": True, "url": verified["html_url"],
                               "number": number, "media_count": len(files), "readback_verified": True}, ensure_ascii=False)
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, urllib.error.URLError, subprocess.SubprocessError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)


SCHEMA = {
    "name": "github_issue_intake",
    "description": "GitHub issues: list/read full issues and comments; find suitable open issues BEFORE creating. Prefer append to a suitable issue (preserves old points, quotes, media and checkmarks). update edits body/title or closes/reopens via state=closed/open using read evidence; body may be omitted. publish requires review_token from find and new_issue_reason. sources/preview preserve Telegram quotes/media. Mutations read back; list/read/find/status/preview do not publish. LessonPilot family chat only; BoxMap existing topic only.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {
                       "project": {"type": "string", "enum": ["lessonpilot", "boxmap"]},
                       "action": {"type": "string", "enum": ["sources", "status", "list", "read", "find", "update", "append", "preview", "publish"]},
                       "body": {"type": "string", "description": "Complete updated issue body for update; preserve all unrelated content"},
                       "expected_body_sha256": {"type": "string", "description": "body_sha256 returned by read for update/append"},
                       "expected_updated_at": {"type": "string", "description": "updated_at returned by read"},
                       "review_token": {"type": "string", "description": "Fresh token from find, required to create a new issue"},
                       "new_issue_reason": {"type": "string", "description": "Brief reason why no reviewed issue fits this problem"},
                       "issue_number": {"type": "integer", "minimum": 1},
                       "state": {"type": "string", "enum": ["open", "closed", "all"]},
                       "state_reason": {"type": "string", "enum": ["completed", "not_planned", "reopened"], "description": "Optional reason paired with explicit update state: completed/not_planned for closed, reopened for open"},
                       "page": {"type": "integer", "minimum": 1, "maximum": 1000},
                       "message_ids": {"type": "array", "items": {"type": "string"}},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                       "problem": {"type": "string", "description": "Stable problem group, e.g. materials"},
                       "title": {"type": "string"}, "summary": {"type": "string"},
                       "items": {"type": "array", "items": {"type": "string"}},
                       "item_sources": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "For every task item, the source Telegram message IDs supporting it, in the same order as items. Required with multiple messages. The tool assigns stable message/screenshot numbers and adds matching references to quotes, tasks and media captions; do not type those suffixes into items yourself."}},
                   "required": ["project", "action"]}}

registry.register(name="github_issue_intake", toolset="github-issues", schema=SCHEMA,
                  handler=lambda args, **_: github_issue_intake(args),
                  check_fn=lambda: True, emoji="📋")


def specialist_task(args):
    """Build a fixed specialist invocation; never pass through arbitrary tools/goals."""
    project = args.get("project")
    scope = _scope(project)
    ids = args.get("message_ids")
    if ids is not None:
        _messages(scope, ids)
    request = str(get_session_env("HERMES_SESSION_USER_REQUEST", "") or "").strip()
    if not request:
        raise ValueError("A gateway-attributed user request is required")
    return {
        "specialist": "github_issues", "role": "leaf",
        "toolsets": ["github-issues", "vision"],
        "goal": "Handle the current explicit Telegram request for GitHub issues. Read full issues and comments. Edit, close or reopen existing issues with update when requested (state=closed/open, body optional); use expected_body_sha256 and expected_updated_at from read. For new feedback first find suitable existing issues by meaning and append new points/quotes/media there. Create with publish only when none fits; requires find review_token and new_issue_reason. Preserve old content and checkbox states. Mark acceptance checkboxes only after user confirmation or explicit permission for that specific task. Reading alone does not authorize execution. Start the user-facing response with 📋 *Issues*. Report verified URLs or the precise blocker.",
        "context": json.dumps({"project": project, "message_ids": ids,
                               "current_user_request": request}, ensure_ascii=False),
    }


SPECIALIST_SCHEMA = {
    "name": "github_issue_specialist",
    "description": "📋 Issues specialist: read/list, edit, close/reopen existing issues, add feedback points and media. Before creating a new issue, find and compare suitable existing ones; prefer adding to the matching issue. Use for LessonPilot family chat or BoxMap topic. Preserve full quotes, media and checkbox states. No expanded technical plans. Reading does not authorize execution. Does not grant terminal/delegation access.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {
                       "project": {"type": "string", "enum": ["lessonpilot", "boxmap"]},
                       "message_ids": {"type": "array", "items": {"type": "string"}}},
                   "required": ["project"]}}

registry.register(name="github_issue_specialist", toolset="github-issue-specialist",
                  schema=SPECIALIST_SCHEMA,
                  handler=lambda args, **_: json.dumps({"success": False, "error": "Requires agent dispatch"}),
                  check_fn=lambda: True, emoji="📋")
