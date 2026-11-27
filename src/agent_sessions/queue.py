"""Local atomic message queue, receipts, claims, legacy migration, and session state."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths


def load_state(session_id):
    path = paths.state_path(session_id)
    try:
        state = json.loads(path.read_text())
        if (
            not isinstance(state, dict)
            or any(
                not isinstance(state.get(key, []), list)
                or any(not isinstance(item, str) for item in state.get(key, []))
                for key in ("delivered", "warned")
            )
            or not isinstance(state.get("started", ""), str)
            or not isinstance(state.get("host", ""), str)
            or not isinstance(state.get("cwd", ""), str)
        ):
            raise ValueError("expected warned and delivered lists and a started timestamp")
        state.setdefault("warned", [])
        return state
    except FileNotFoundError:
        return {"warned": []}
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read delivery state {path}: {exc}") from exc


def save_state(session_id, state):
    paths.write_json(paths.state_path(session_id), state)


def record_start(session_id, started=None):
    """Remember when a session first started, so only later invitations reach it."""
    with paths.locked(paths.state_path(session_id)):
        state = load_state(session_id)
        if not state.get("started"):
            state["started"] = started or datetime.now(timezone.utc).isoformat()
            save_state(session_id, state)


def record_session_context(session_id, host, cwd):
    """Remember a session's host and canonical cwd when native metadata omits workspace URIs."""
    if not host or not cwd:
        return
    canonical = paths.resolved(cwd)
    if not Path(canonical).is_absolute():
        return
    with paths.locked(paths.state_path(session_id)):
        state = load_state(session_id)
        if state.get("host") == host and state.get("cwd") == canonical:
            return
        state["host"] = host
        state["cwd"] = canonical
        save_state(session_id, state)


def recorded_session_cwd(session_id, expected_host):
    """Fallback cwd recorded by a profile binding or hook state for the expected host."""
    from . import profiles

    try:
        bound = profiles.binding(paths.data_dir(), session_id)
    except profiles.ProfileError:
        bound = None
    if (
        bound
        and bound.get("host") == expected_host
        and isinstance(bound.get("cwd"), str)
        and Path(bound["cwd"]).is_absolute()
    ):
        return bound["cwd"]
    try:
        state = load_state(session_id)
    except ValueError:
        return None
    cwd = state.get("cwd")
    if state.get("host") == expected_host and isinstance(cwd, str) and Path(cwd).is_absolute():
        return cwd
    return None


def publish(key, message, box="new"):
    """Write a message to a staging file, then rename it into its queue in one step."""
    target = paths.message_file(paths.private_dir(paths.queue_dir(key, box)), message["id"])
    descriptor, name = tempfile.mkstemp(
        dir=paths.private_dir(paths.data_dir() / "tmp"), prefix=".message-"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(message, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def valid_message(message):
    return (
        isinstance(message, dict)
        and all(isinstance(message.get(key), str) for key in ("id", "from", "at", "text"))
        and bool(paths.MESSAGE_ID.fullmatch(message["id"]))
        and (
            "path" not in message
            or (isinstance(message["path"], str) and Path(message["path"]).is_absolute())
        )
    )


def read_message(path):
    """A queued message, or None when it is gone or invalid."""
    try:
        message = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        message = None
    if valid_message(message) and message["id"] == path.stem:
        return message
    print(f"agent-sessions: skipping invalid message {path}", file=sys.stderr)
    return None


def listing(directory):
    try:
        return sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    except (FileNotFoundError, NotADirectoryError):
        return []


def read_inbox(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    messages = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not valid_message(message):
                raise ValueError("invalid message")
            messages.append(message)
        except ValueError:
            print(f"agent-sessions: skipping invalid message at {path}:{number}", file=sys.stderr)
    return messages


def send(to, text, found, sender=None, sender_name=None, pair_id=None):
    """Leave text for one session id, or for every session working under a path.

    Returns the message and the active sessions it addresses now; a path message also waits
    for sessions that start under that path later.
    """
    message = {
        "id": paths.new_id(),
        "from": sender_name or sender or "unknown",
        "text": text,
        "from_id": sender,
        "at": paths.now("seconds"),
    }
    if pair_id:
        message["pair_id"] = pair_id
    candidates = [s for s in found if s["id"] != sender]
    by_id = [s for s in candidates if s["id"] == to]
    if not by_id and len(to) >= 6:
        by_id = [s for s in candidates if s["id"].startswith(to)]
    if len(by_id) > 1:
        raise SystemExit(
            f"agent-sessions: {to} matches {len(by_id)} sessions; give more of the id"
        )
    if by_id:
        published = {**message, "to": by_id[0]["id"]}
        publish(paths.session_key(by_id[0]["id"]), published)
        return published, by_id
    if not Path(to).expanduser().is_dir():
        raise SystemExit(f"agent-sessions: no active session or directory matches {to}")
    root = paths.resolved(to)
    published = {**message, "path": root}
    publish(paths.path_key(root), published)
    return published, [s for s in candidates if paths.inside(paths.resolved(s["cwd"]), root)]


def invited(pair_id, session_id, host, started):
    """An invitation reaches its connected partner, or a session of the requested host that
    started after the pair was created.
    """
    from . import partners

    try:
        pair = partners.read_pair(pair_id)
        if host != pair["partner"]["host"]:
            return False
        if pair["status"] == "connected":
            return pair["partner"]["id"] == session_id
        return bool(started) and (
            datetime.fromisoformat(started) >= datetime.fromisoformat(pair["created_at"])
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"agent-sessions: skipping invitation with unreadable pair: {exc}", file=sys.stderr)
        return False


def receive(session_id, cwd=None, mark=True, host=None):
    """New messages for a session: its own queue, then queues of directories holding cwd.

    Receiving marks a message received; it stays in the queue until an agent claims it. Cost
    is one folder listing per ancestor of cwd plus the new messages, not the message history.
    """
    migrate_legacy()
    started = load_state(session_id).get("started")
    key = paths.session_key(session_id)
    messages = []
    for name in listing(paths.queue_dir(key)):
        message = read_message(paths.queue_dir(key) / name)
        if message is None:
            continue
        if mark:
            # A reply to this session's own message needs no claim: receiving it removes it.
            box = paths.data_dir() / "claimed" if message.get("re") else paths.queue_dir(key, "cur")
            try:
                os.rename(paths.queue_dir(key) / name, paths.private_dir(box) / name)
            except FileNotFoundError:
                continue
        messages.append(message)
    for root in paths.ancestors(paths.resolved(cwd)) if cwd else []:
        directory = paths.queue_dir(paths.path_key(root))
        for name in listing(directory):
            message = read_message(directory / name)
            if (
                message is None
                or not isinstance(message.get("path"), str)
                or paths.resolved(message["path"]) != root
                or message.get("from_id") == session_id
                or (
                    message.get("pair_id")
                    and not invited(message["pair_id"], session_id, host, started)
                )
            ):
                continue
            receipt = paths.receipt_dir(message["id"]) / session_id
            if receipt.exists():
                continue
            if mark:
                try:
                    os.close(
                        os.open(
                            paths.private_dir(receipt.parent) / session_id,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                    )
                except FileExistsError:
                    continue
            messages.append(message)
    return messages


def find_message(message_id, session_id, cwd):
    """The queued file of a message this session can see, or None."""
    key = paths.session_key(session_id)
    folders = [paths.queue_dir(key, "cur"), paths.queue_dir(key)]
    if cwd:
        folders += [paths.queue_dir(paths.path_key(root)) for root in paths.ancestors(paths.resolved(cwd))]
    return next(
        (path for path in (paths.message_file(folder, message_id) for folder in folders) if path.exists()),
        None,
    )


def take(path, message, session_id, text, sender_name=None):
    """Move one message to claimed/ and reply to its sender; None if another session won."""
    done = paths.message_file(paths.private_dir(paths.data_dir() / "claimed"), message["id"])
    try:
        os.rename(path, done)
    except FileNotFoundError:
        return None
    message = {**message, "claimed_by": session_id, "claimed_at": paths.now()}
    paths.write_json(done, message)
    shutil.rmtree(paths.receipt_dir(message["id"]), ignore_errors=True)
    reply = None
    sender = message.get("from_id")
    if (
        text
        and isinstance(sender, str)
        and sender != session_id
        and paths.SESSION_ID_RE.fullmatch(sender)
    ):
        reply = {
            "id": paths.new_id(),
            "from": sender_name or f"session {session_id[:8]}",
            "from_id": session_id,
            "to": sender,
            "re": message["id"],
            "text": text,
            "at": paths.now("seconds"),
        }
        publish(paths.session_key(sender), reply)
    return {"claimed": message, "reply": reply}


def claim(message_id, path, session_id, text="ready", sender_name=None):
    """Take a message this session received out of the queue and tell its sender."""
    done = paths.message_file(paths.data_dir() / "claimed", message_id)
    message = read_message(path) if path else None
    if message and message.get("pair_id"):
        raise ValueError(f"claim a partner invitation with: partner connect {message['pair_id']}")
    result = take(path, message, session_id, text, sender_name) if message else None
    if result:
        return result
    holder = read_message(done) if done.exists() else None
    if holder:
        raise ValueError(
            f"message {message_id} already claimed by {holder.get('claimed_by') or 'another session'}"
        )
    raise ValueError(f"no message {message_id} in this session's queues")


def status(message_id):
    """Whether a message is pending, received (and by whom) or claimed."""
    done = paths.message_file(paths.data_dir() / "claimed", message_id)
    if done.exists():
        message = read_message(done) or {}
        return {
            "id": message_id,
            "state": "claimed",
            "claimed_by": message.get("claimed_by"),
            "claimed_at": message.get("claimed_at"),
        }
    for path in sorted((paths.data_dir() / "queue").glob(f"*/*/{done.name}")):
        message = read_message(path)
        if message is None:
            continue
        receipts = paths.receipt_dir(message_id)
        received = (
            [message.get("to")]
            if path.parent.name == "cur"
            else sorted(os.listdir(receipts))
            if receipts.is_dir()
            else []
        )
        return {
            "id": message_id,
            "state": "received" if received else "pending",
            "received_by": received,
            "to": message.get("to") or message.get("path"),
        }
    raise ValueError(f"no message {message_id}")


def migrate_legacy():
    """Move messages from the earlier JSONL inboxes into the queue once, keeping receipts."""
    root = paths.data_dir()
    legacy = [root / "inbox", root / "paths"]
    if not any(folder.is_dir() for folder in legacy):
        return
    with paths.locked(root / "migration"):
        if not any(folder.is_dir() for folder in legacy):
            return
        delivered = {}
        for state in (root / "state").glob("*.json"):
            # Validate every receipt source before publishing or moving any legacy data.
            # Skipping damaged state would turn previously received messages into new work.
            ids = load_state(state.stem).get("delivered", [])
            for message_id in ids:
                delivered.setdefault(message_id, set()).add(state.stem)
        for path in sorted((root / "inbox").glob("*.jsonl")):
            if not paths.SESSION_ID_RE.fullmatch(path.stem):
                continue
            for message in read_inbox(path):
                seen = path.stem in delivered.get(message["id"], ())
                publish(paths.session_key(path.stem), message, "cur" if seen else "new")
        for path in sorted((root / "paths").glob("*.jsonl")):
            for message in read_inbox(path):
                if not message.get("path"):
                    continue
                publish(paths.path_key(message["path"]), message)
                for session in delivered.get(message["id"], ()):
                    if paths.SESSION_ID_RE.fullmatch(session):
                        receipt = paths.private_dir(paths.receipt_dir(message["id"])) / session
                        os.close(os.open(receipt, os.O_WRONLY | os.O_CREAT, 0o600))
        for folder in legacy:
            if folder.is_dir():
                folder.rename(folder.with_name(f"{folder.name}.migrated-{uuid.uuid4().hex[:8]}"))


def describe_message(message):
    origin = f"{message['from']} at {message['at']}"
    if message.get("re"):
        return (
            f"Reply {message['id']} to your message {message['re']} from {origin} "
            f"(session {message.get('from_id') or 'unknown'}): {message['text']}"
        )
    return (
        f"Message {message['id']} from {origin} "
        f"(reply session: {message.get('from_id') or 'unknown'}): {message['text']}"
    )
