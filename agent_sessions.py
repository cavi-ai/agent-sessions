#!/usr/bin/env python3
"""List active Codex and Claude Code sessions, find sessions sharing a repository, and pass
messages between them."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_MINUTES = 30
TAIL_BYTES = 2_000_000
GIT_TIMEOUT = 5
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def data_dir():
    return Path(os.environ.get("AGENT_SESSIONS_DATA") or Path.home() / ".local/share/agent-sessions")


def resolved(path):
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(path)


def inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def tail_records(path, size=TAIL_BYTES):
    """JSON records from the last size bytes of a JSONL file; a cut first line is skipped."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            start = max(0, handle.tell() - size)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]
    records = []
    for line in lines:
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    return records


def unique_last(items, limit=20):
    seen, out = set(), []
    for item in reversed(items):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return list(reversed(out[:limit]))


def codex_db():
    numbered = []
    for path in codex_home().glob("state_*.sqlite"):
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if match:
            numbered.append((int(match.group(1)), path))
    return max(numbered)[1] if numbered else None


def codex_rollout(path):
    """Turn state and edited files from the end of a Codex rollout."""
    status, edited = "idle", []
    for record in tail_records(path):
        payload = record.get("payload") or {}
        kind = payload.get("type")
        if kind == "task_started":
            status = "running"
        elif kind in ("task_complete", "turn_aborted"):
            status = "idle"
        item = payload.get("item") or {}
        if item.get("type") == "FileChange":
            changes = item.get("changes") or {}
            edited.extend(changes if isinstance(changes, dict) else
                          [c.get("path") for c in changes if isinstance(c, dict) and c.get("path")])
    return status, unique_last(edited)


def codex_sessions(since, include_reviews=False, within=None):
    db = codex_db()
    if db is None:
        return []
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
        updated = ("coalesce(updated_at_ms, updated_at * 1000)" if "updated_at_ms" in columns
                   else "updated_at * 1000")
        title = " , ".join(f"nullif({c}, '')" for c in ("name", "title", "preview") if c in columns)
        nick = "agent_nickname" if "agent_nickname" in columns else "null"
        rows = connection.execute(
            f"select id, rollout_path, {updated}, cwd, coalesce({title}, ''), source, {nick} "
            f"from threads where archived = 0 and {updated} >= ? order by {updated} desc",
            (int(since * 1000),)).fetchall()
        connection.close()
    except sqlite3.Error as exc:
        print(f"agent-sessions: cannot read {db}: {exc}", file=sys.stderr)
        return []
    sessions = []
    for thread, rollout, updated_ms, cwd, title, source, nickname in rows:
        if within and not inside(resolved(cwd), within):
            continue
        parent = role = None
        try:
            origin = json.loads(source)
        except (TypeError, ValueError):
            origin = source
        if isinstance(origin, dict):
            sub = origin.get("subagent") or {}
            spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
            if isinstance(spawn, dict):
                parent, role = spawn.get("parent_thread_id"), "subagent"
            elif sub:
                role = sub.get("other", "review") if isinstance(sub, dict) else str(sub)
                if not include_reviews:
                    continue
        status, edited = codex_rollout(rollout)
        label = nickname or (title.splitlines() or [""])[0][:80]
        sessions.append({"host": "codex", "id": thread, "title": label, "cwd": cwd,
                         "updated": updated_ms / 1000, "status": status, "parent": parent,
                         "role": role, "edited": edited})
    return sessions


def claude_sessions(since, within=None):
    sessions = []
    for path in (claude_home() / "projects").glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < since:
            continue
        cwd = title = None
        edited = []
        for record in tail_records(path):
            if record.get("cwd"):
                cwd = record["cwd"]
            if record.get("type") == "last-prompt" and record.get("lastPrompt"):
                title = record["lastPrompt"]
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") in EDIT_TOOLS):
                    arguments = block.get("input") or {}
                    target = arguments.get("file_path") or arguments.get("notebook_path")
                    if target:
                        edited.append(target)
        if not cwd or (within and not inside(resolved(cwd), within)):
            continue
        sessions.append({"host": "claude", "id": path.stem,
                         "title": (title or "").strip().splitlines()[0][:80] if title else "",
                         "cwd": cwd, "updated": mtime, "status": "active", "parent": None,
                         "role": None, "edited": unique_last(edited)})
    return sessions


def git(directory, *args):
    return subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, check=False)


def git_state(cwd, cache=None):
    """Live repository facts for cwd: worktree root, shared git dir, branch, uncommitted files."""
    cache = {} if cache is None else cache
    key = resolved(cwd)
    if key in cache:
        return cache[key]
    state = {}
    if not Path(key).is_dir():
        state = {"missing": True}
    else:
        try:
            out = git(key, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir",
                      "--abbrev-ref", "HEAD")
            if out.returncode == 0:
                root, gitdir, common, branch = out.stdout.splitlines()[:4]
                gitdir, common = (resolved(Path(key) / p) for p in (gitdir, common))
                status = git(root, "status", "--porcelain")
                dirty = [line[3:].split(" -> ")[-1] for line in status.stdout.splitlines()
                         if len(line) > 3]
                state = {"root": resolved(root), "common": common, "branch": branch,
                         "linked": gitdir != common, "dirty": dirty}
        except (OSError, subprocess.SubprocessError, ValueError):
            state = {}
    cache[key] = state
    return state


def sessions(minutes=ACTIVE_MINUTES, include_reviews=False, within=None):
    """Active sessions, newest first; within limits them to working directories under a path."""
    since = time.time() - minutes * 60
    found = codex_sessions(since, include_reviews, within) + claude_sessions(since, within)
    cache = {}
    for session in found:
        session["git"] = git_state(session["cwd"], cache)
    return sorted(found, key=lambda s: -s["updated"])


def relative_files(session):
    """Uncommitted and recently edited files of a session, relative to its worktree."""
    git = session.get("git") or {}
    root = git.get("root")
    files = set(git.get("dirty", []))
    for path in session.get("edited", []):
        path = resolved(path)
        if root and inside(path, root):
            files.add(os.path.relpath(path, root))
    return files


def who(path, found):
    target = git_state(path)
    if not target.get("common"):
        raise SystemExit(f"agent-sessions: {path} is not inside a git repository")
    mine = set(target["dirty"])
    peers = []
    for session in found:
        git = session.get("git") or {}
        if git.get("common") != target["common"]:
            continue
        files = relative_files(session)
        peers.append({**session, "same_worktree": git.get("root") == target["root"],
                      "overlap": sorted(files & mine)})
    return {"root": target["root"], "branch": target["branch"], "linked": target["linked"],
            "dirty": len(mine), "sessions": peers}


def ago(epoch):
    minutes = int((time.time() - epoch) // 60)
    return f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"


def home(path):
    text = str(path)
    user = str(Path.home())
    return "~" + text[len(user):] if inside(text, user) else text


def describe(session):
    git = session.get("git") or {}
    if git.get("missing"):
        place = f"{home(session['cwd'])} (missing)"
    elif git.get("root"):
        kind = "worktree" if git["linked"] else "main checkout"
        place = f"{home(git['root'])} ({kind}, {git['branch']}, {len(git['dirty'])} uncommitted)"
    else:
        place = home(session["cwd"])
    parent = f", subagent of {session['parent'][:13]}" if session.get("parent") else ""
    mine = " (this session)" if session["id"] == os.environ.get("CLAUDE_CODE_SESSION_ID") else ""
    return (f"{session['host']} {session['id'][:13]}{mine} {session['status']} "
            f"{ago(session['updated'])} ago{parent}: {session['title'] or '(untitled)'} -- {place}")


# Message board: an append-only inbox per session id and per path, one delivery record per
# session. A path inbox reaches every session working under that path, including later ones.

def inbox_path(session_id):
    return data_dir() / "inbox" / f"{session_id}.jsonl"


def path_inbox(root):
    return data_dir() / "paths" / f"{hashlib.sha256(root.encode()).hexdigest()[:16]}.jsonl"


def state_path(session_id):
    return data_dir() / "state" / f"{session_id}.json"


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_state(session_id):
    try:
        return json.loads(state_path(session_id).read_text())
    except (OSError, ValueError):
        return {"delivered": [], "warned": []}


def save_state(session_id, state):
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    with os.fdopen(descriptor, "w") as handle:
        json.dump({key: value[-500:] for key, value in state.items()}, handle)
    os.replace(name, path)


def append(path, record):
    with locked(path), open(path, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def read_inbox(path):
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def send(to, text, found, sender=None, sender_name=None):
    """Leave text for one session id, or for every session working under a path.

    Returns the message and the active sessions it addresses now; a path message also waits
    for sessions that start under that path later."""
    message = {"id": uuid.uuid4().hex, "from": sender_name or sender or "unknown", "text": text,
               "from_id": sender, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    candidates = [s for s in found if s["id"] != sender]
    by_id = [s for s in candidates if s["id"] == to or (len(to) >= 6 and s["id"].startswith(to))]
    if len(by_id) > 1:
        raise SystemExit(f"agent-sessions: {to} matches {len(by_id)} sessions; give more of the id")
    if by_id:
        append(inbox_path(by_id[0]["id"]), {**message, "to": by_id[0]["id"]})
        return message, by_id
    if not Path(to).expanduser().is_dir():
        raise SystemExit(f"agent-sessions: no active session or directory matches {to}")
    root = resolved(to)
    append(path_inbox(root), {**message, "path": root})
    return message, [s for s in candidates if inside(resolved(s["cwd"]), root)]


def unread(session_id, cwd=None, mark=True):
    """Undelivered messages for a session: its own inbox, then inboxes of paths holding cwd."""
    messages = read_inbox(inbox_path(session_id))
    if cwd:
        here = resolved(cwd)
        for path in sorted((data_dir() / "paths").glob("*.jsonl")):
            messages += [m for m in read_inbox(path)
                         if inside(here, m.get("path", "")) and m.get("from_id") != session_id]
    if not messages:
        return []
    with locked(state_path(session_id)):
        state = load_state(session_id)
        seen = set(state["delivered"])
        fresh = [m for m in messages if m["id"] not in seen]
        if mark and fresh:
            state["delivered"] += [m["id"] for m in fresh]
            save_state(session_id, state)
    return fresh


def self_id(cwd, found):
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return os.environ["CLAUDE_CODE_SESSION_ID"]
    here = [s for s in found if resolved(s["cwd"]) == resolved(cwd)]
    if len(here) == 1:
        return here[0]["id"]
    raise SystemExit(f"agent-sessions: {len(here)} sessions work in {cwd}; pass --session")


def hook(payload, minutes=ACTIVE_MINUTES):
    """Context for a Claude Code or Codex hook: new messages, then sessions sharing this worktree."""
    session_id = payload.get("session_id") or payload.get("thread_id")
    if not session_id:
        return None
    event = payload.get("hook_event_name") or "UserPromptSubmit"
    cwd = payload.get("cwd") or os.getcwd()
    lines = [f"Message from {m['from']} at {m['at']} (another agent session): {m['text']}"
             for m in unread(session_id, cwd)]
    if event in ("SessionStart", "UserPromptSubmit"):
        here = git_state(cwd)
        if here.get("root"):
            with locked(state_path(session_id)):
                state = load_state(session_id)
                peers = [s for s in sessions(minutes, within=here["root"]) if s["id"] != session_id
                         and (s.get("git") or {}).get("root") == here["root"]
                         and s["id"] not in state["warned"]]
                if peers:
                    state["warned"] += [s["id"] for s in peers]
                    save_state(session_id, state)
            if peers:
                lines.append("Other agent sessions are working in this checkout:")
                lines += [f"- {describe(s)}" for s in peers]
                lines.append(f"Coordinate before editing: python3 {Path(__file__).resolve()} "
                             "send <id> '<message>'.")
    if not lines:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n".join(lines)}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-sessions", description=__doc__)
    parser.add_argument("--minutes", type=int, default=ACTIVE_MINUTES,
                        help="sessions updated within this many minutes count as active")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="active sessions with cwd, worktree, branch and status")
    listing.add_argument("--reviews", action="store_true", help="include Codex approval reviewers")
    sharing = sub.add_parser("who", help="active sessions in the same repository as PATH")
    sharing.add_argument("path", nargs="?", default=".")
    sending = sub.add_parser("send", help="leave a message for a session id or every session in PATH")
    sending.add_argument("to")
    sending.add_argument("text")
    sending.add_argument("--session", help="sender session id (default: this session)")
    reading = sub.add_parser("inbox", help="unread messages for this session")
    reading.add_argument("--session", help="session id (default: this session)")
    reading.add_argument("--keep", action="store_true", help="leave the messages unread")
    sub.add_parser("hook", help="read a Claude Code or Codex hook payload on stdin")
    args = parser.parse_args(argv)

    if args.command == "hook":
        try:
            raw = sys.stdin.read()
            result = hook(json.loads(raw) if raw.strip() else {}, args.minutes)
        except Exception as exc:  # noqa: BLE001 - a hook never blocks its session
            print(f"agent-sessions hook: {exc}", file=sys.stderr)
            return 0
        if result:
            print(json.dumps(result))
        return 0

    found = sessions(args.minutes, getattr(args, "reviews", False))
    if args.command == "list":
        result = found
        text = [describe(s) for s in found] or [f"No sessions active in the last {args.minutes} minutes."]
    elif args.command == "who":
        result = who(args.path, found)
        kind = "worktree" if result["linked"] else "main checkout"
        text = [f"{home(result['root'])} ({kind}, {result['branch']}, {result['dirty']} uncommitted)"]
        for peer in result["sessions"]:
            mark = "SAME WORKTREE " if peer["same_worktree"] else ""
            text.append(f"- {mark}{describe(peer)}")
            if peer["overlap"]:
                text.append(f"  also changing: {', '.join(peer['overlap'][:10])}")
        if not result["sessions"]:
            text.append("No other active session works in this repository.")
    elif args.command == "send":
        sender = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
        name = f"{f'session {sender[:8]}' if sender else 'an agent'} in {home(os.getcwd())}"
        message, targets = send(args.to, args.text, found, sender, name)
        result = {"message": message, "to": [t["id"] for t in targets]}
        waiting = (f" and any session that starts under {home(resolved(args.to))}"
                   if Path(args.to).expanduser().is_dir() else "")
        text = [f"Left message {message['id'][:8]} for {len(targets)} active session(s){waiting}:"]
        text += [f"- {describe(t)}" for t in targets]
    else:
        session_id = args.session or self_id(os.getcwd(), found)
        cwd = next((s["cwd"] for s in found if s["id"] == session_id), os.getcwd())
        result = unread(session_id, cwd, mark=not args.keep)
        text = [f"{m['at']} {m['from']}: {m['text']}" for m in result] or ["No unread messages."]
    print(json.dumps(result, indent=2) if args.json else "\n".join(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
