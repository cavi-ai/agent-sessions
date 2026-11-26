"""Native session discovery for Codex, Claude Code, Antigravity (agy), and OpenCode."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import paths

AGY_EDIT_PATH_KEYS = (
    "TargetFile",
    "target_file",
    "AbsolutePath",
    "file_path",
    "Path",
    "path",
    "NotebookPath",
    "notebook_path",
)
AGY_STEP_PATH_RE = re.compile(
    r'"(?:' + "|".join(AGY_EDIT_PATH_KEYS) + r')"\s*:\s*"((?:\\.|[^"\\])+)"'
)


def tail_records(path, size=paths.TAIL_BYTES):
    """JSON dict records from the last size bytes of a JSONL file; a cut first line is skipped."""
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
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
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
    for path in paths.codex_home().glob("state_*.sqlite"):
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if match:
            numbered.append((int(match.group(1)), path))
    return max(numbered)[1] if numbered else None


def codex_rollout(path):
    """Turn state and edited files from the end of a Codex rollout."""
    status, edited = "idle", []
    if not path:
        return status, edited
    for record in tail_records(path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "task_started":
            status = "running"
        elif kind in ("task_complete", "turn_aborted"):
            status = "idle"
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "FileChange":
            changes = item.get("changes") or {}
            if isinstance(changes, dict):
                edited.extend(changes)
            elif isinstance(changes, list):
                edited.extend(
                    c.get("path") for c in changes if isinstance(c, dict) and c.get("path")
                )
    return status, unique_last(edited)


def codex_sessions(since, include_reviews=False, within=None):
    db = codex_db()
    if db is None:
        return []
    try:
        with closing(
            sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as connection:
            columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
            if not {"id", "rollout_path", "cwd", "source", "archived"} <= columns or not (
                {"updated_at_ms", "updated_at"} & columns
            ):
                return []
            updated = (
                "coalesce(updated_at_ms, updated_at * 1000)"
                if "updated_at_ms" in columns and "updated_at" in columns
                else "updated_at_ms"
                if "updated_at_ms" in columns
                else "updated_at * 1000"
            )
            title_parts = " , ".join(
                f"nullif({c}, '')" for c in ("name", "title", "preview") if c in columns
            )
            title_expr = f"coalesce({title_parts}, '')" if title_parts else "''"
            nick = "agent_nickname" if "agent_nickname" in columns else "null"
            rows = connection.execute(
                f"select id, rollout_path, {updated}, cwd, {title_expr}, source, {nick} "
                f"from threads where archived = 0 and {updated} >= ? order by {updated} desc",
                (int(since * 1000),),
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"agent-sessions: cannot read {db}: {exc}", file=sys.stderr)
        return []
    found = []
    for thread, rollout, updated_ms, cwd, title, source, nickname in rows:
        if not isinstance(thread, str) or not paths.SESSION_ID_RE.fullmatch(thread):
            continue
        if not isinstance(cwd, str) or not cwd:
            continue
        if within and not paths.inside(paths.resolved(cwd), within):
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
        raw_title = title if isinstance(title, str) else ""
        label = nickname or (raw_title.splitlines() or [""])[0][:80]
        found.append(
            {
                "host": "codex",
                "id": thread,
                "title": label,
                "cwd": cwd,
                "updated": updated_ms / 1000,
                "status": status,
                "parent": parent,
                "role": role,
                "edited": edited,
            }
        )
    return found


def claude_sessions(since, within=None):
    found = []
    for path in (paths.claude_home() / "projects").glob("*/*.jsonl"):
        if not paths.SESSION_ID_RE.fullmatch(path.stem):
            continue
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
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") in paths.EDIT_TOOLS
                ):
                    arguments = block.get("input")
                    if not isinstance(arguments, dict):
                        continue
                    target = arguments.get("file_path") or arguments.get("notebook_path")
                    if target:
                        edited.append(target)
        if not cwd or (within and not paths.inside(paths.resolved(cwd), within)):
            continue
        raw_title = title.strip() if isinstance(title, str) else ""
        found.append(
            {
                "host": "claude",
                "id": path.stem,
                "title": (raw_title.splitlines() or [""])[0][:80],
                "cwd": cwd,
                "updated": mtime,
                "status": "active",
                "parent": None,
                "role": None,
                "edited": unique_last(edited),
            }
        )
    return found


def _uri_to_path(uri):
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("file://"):
        return urllib.parse.unquote(urllib.parse.urlparse(uri).path)
    return uri


def _parse_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            num = float(text)
            return num / 1000.0 if num > 1e12 else num
        except ValueError:
            pass
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        match = re.match(
            r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\.(\d+)([+-]\d{2}:\d{2})?$",
            text,
        )
        if match:
            base, frac, tz = match.groups()
            text = f"{base}.{(frac + '000000')[:6]}{tz or ''}"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def agy_edited(conversation_id, home_dir=None):
    if not isinstance(conversation_id, str) or not paths.SESSION_ID_RE.fullmatch(conversation_id):
        return []
    edited = []
    home_dir = paths.agy_home() if home_dir is None else Path(home_dir)
    transcript = home_dir / "brain" / conversation_id / ".system_generated/logs/transcript.jsonl"
    if transcript.is_file():
        for record in tail_records(transcript):
            calls = record.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or call.get("name") not in paths.AGY_EDIT_TOOLS:
                    continue
                args = call.get("args") or call.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if not isinstance(args, dict):
                    continue
                for key in AGY_EDIT_PATH_KEYS:
                    val = args.get(key)
                    if isinstance(val, str) and val:
                        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                            try:
                                val = json.loads(val)
                            except ValueError:
                                pass
                        if isinstance(val, str) and val:
                            edited.append(val)
                            break
    if not edited:
        conv_db = home_dir / "conversations" / f"{conversation_id}.db"
        if conv_db.is_file():
            try:
                with closing(
                    sqlite3.connect(conv_db.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
                ) as conn:
                    cols = {row[1] for row in conn.execute("pragma table_info(steps)")}
                    col = (
                        "step_payload"
                        if "step_payload" in cols
                        else ("payload" if "payload" in cols else None)
                    )
                    if col:
                        rows = conn.execute(
                            f"select {col} from steps where step_type = 132"
                        ).fetchall()
                        for (payload,) in rows:
                            if not payload:
                                continue
                            text = (
                                payload.decode("utf-8", errors="ignore")
                                if isinstance(payload, (bytes, bytearray))
                                else str(payload)
                            )
                            if not any(tool in text for tool in paths.AGY_EDIT_TOOLS):
                                continue
                            for match in AGY_STEP_PATH_RE.finditer(text):
                                try:
                                    path_val = json.loads(f'"{match.group(1)}"')
                                except ValueError:
                                    path_val = match.group(1)
                                if path_val:
                                    edited.append(path_val)
            except sqlite3.Error:
                pass
    return unique_last(edited)


def _agy_sessions_in(home_dir, since, within=None):
    db = Path(home_dir) / "conversation_summaries.db"
    if not db.is_file():
        return []
    try:
        with closing(
            sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            cols = {row[1] for row in conn.execute("pragma table_info(conversation_summaries)")}
            if not {"conversation_id", "last_modified_time", "workspace_uris"} <= cols:
                return []
            sel = [
                "conversation_id",
                "last_modified_time",
                "workspace_uris",
                "title" if "title" in cols else "''",
                "preview" if "preview" in cols else "''",
                "status" if "status" in cols else "''",
                "parent_conversation_id" if "parent_conversation_id" in cols else "''",
                "agent_name" if "agent_name" in cols else "''",
                "not_fully_idle" if "not_fully_idle" in cols else "0",
            ]
            where = "where coalesce(killed, 0) = 0 " if "killed" in cols else ""
            rows = conn.execute(
                f"select {', '.join(sel)} from conversation_summaries "
                f"{where}order by last_modified_time desc"
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"agent-sessions: cannot read {db}: {exc}", file=sys.stderr)
        return []
    found = []
    for (
        cid,
        modified,
        uris_raw,
        title,
        preview,
        status_raw,
        parent_id,
        agent_name,
        not_idle,
    ) in rows:
        if not isinstance(cid, str) or not paths.SESSION_ID_RE.fullmatch(cid):
            continue
        updated = _parse_timestamp(modified)
        if updated is None or updated < since:
            continue
        cwd = None
        try:
            uris = json.loads(uris_raw) if isinstance(uris_raw, str) else uris_raw
        except ValueError:
            uris = []
        if isinstance(uris, list):
            for uri in uris:
                candidate = _uri_to_path(uri)
                if candidate:
                    cwd = candidate
                    break
        if not cwd or (within and not paths.inside(paths.resolved(cwd), within)):
            continue
        status_val = (
            "running"
            if ("RUNNING" in str(status_raw or "").upper() or bool(not_idle))
            else "idle"
        )
        parent = parent_id or None
        raw_label = (title or preview or "").strip()
        label = (raw_label.splitlines() or [""])[0][:80] if raw_label else (agent_name or "")
        role = "subagent" if parent else (agent_name or None)
        found.append(
            {
                "host": "agy",
                "id": cid,
                "title": label,
                "cwd": cwd,
                "updated": updated,
                "status": status_val,
                "parent": parent,
                "role": role,
                "edited": agy_edited(cid, home_dir=home_dir),
            }
        )
    return found


def agy_sessions(since, within=None):
    by_id = {}
    for home_dir in paths.agy_homes():
        for session in _agy_sessions_in(home_dir, since, within):
            prev = by_id.get(session["id"])
            if prev is None or session["updated"] > prev["updated"]:
                by_id[session["id"]] = session
    return list(by_id.values())


def opencode_edited(conn, session_id, cwd, tables=None):
    """Files named by the latest OpenCode edit, write and patch tool calls."""
    edited = []
    try:
        if tables is None:
            tables = {
                row[0]
                for row in conn.execute("select name from sqlite_master where type = 'table'")
            }
        if "session_message" not in tables:
            return edited
        rows = conn.execute(
            "select id, data from session_message where session_id = ? and type = "
            "'assistant' order by seq desc limit 200",
            (session_id,),
        ).fetchall()
        parts_by_message = {}
        if rows and "session_part" in tables:
            ids = [message_id for message_id, _ in rows]
            placeholders = ", ".join("?" for _ in ids)
            for message_id, raw in conn.execute(
                "select message_id, data from session_part where type = 'tool' "
                f"and message_id in ({placeholders}) order by message_id, ordinal",
                ids,
            ):
                try:
                    part = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                parts_by_message.setdefault(message_id, []).append(part)
    except sqlite3.Error:
        return unique_last(edited)
    for message_id, raw in reversed(rows):
        # OpenCode reads stored parts before any inline content that remains.
        parts = parts_by_message.get(message_id, [])
        try:
            content = json.loads(raw).get("content")
        except (TypeError, ValueError, AttributeError):
            content = None
        if isinstance(content, list):
            parts.extend(content)
        for part in parts:
            if (
                not isinstance(part, dict)
                or part.get("type") != "tool"
                or part.get("name") not in paths.OPENCODE_EDIT_TOOLS
            ):
                continue
            state = part.get("state")
            args = state.get("input") if isinstance(state, dict) else None
            if not isinstance(args, dict):
                continue
            targets = [args.get(key) for key in ("path", "filePath", "file_path")]
            if isinstance(args.get("patchText"), str):
                targets += paths.OPENCODE_PATCH_FILE_RE.findall(args["patchText"])
            for target in targets:
                if isinstance(target, str) and target.strip():
                    target = target.strip()
                    edited.append(target if os.path.isabs(target) else str(Path(cwd) / target))
    return unique_last(edited)


def opencode_sessions(since, within=None):
    db = paths.opencode_db_path()
    if db is None or not db.is_file():
        return []
    try:
        with closing(
            sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            tables = {
                row[0]
                for row in conn.execute("select name from sqlite_master where type = 'table'")
            }
            table = "session_v2" if "session_v2" in tables else "session"
            if table not in tables:
                return []
            cols = {row[1] for row in conn.execute(f"pragma table_info({table})")}
            if not {"id", "directory", "time_updated"} <= cols:
                return []
            sel = [
                "id",
                "directory",
                "time_updated",
                "coalesce(title, '')" if "title" in cols else "''",
                "parent_id" if "parent_id" in cols else "null",
                "time_busy" if "time_busy" in cols else "null",
                "time_idle" if "time_idle" in cols else "null",
            ]
            where = "and time_archived is null " if "time_archived" in cols else ""
            rows = conn.execute(
                f"select {', '.join(sel)} from {table} where time_updated >= ? {where}"
                "order by time_updated desc",
                (int(since * 1000),),
            ).fetchall()
            found = []
            for sid, cwd, updated_ms, title, parent, busy, idle in rows:
                if not isinstance(sid, str) or not paths.SESSION_ID_RE.fullmatch(sid) or not cwd:
                    continue
                if within and not paths.inside(paths.resolved(cwd), within):
                    continue
                running = busy is not None and (idle is None or busy > idle)
                found.append(
                    {
                        "host": "opencode",
                        "id": sid,
                        "title": (title.splitlines() or [""])[0][:80] if title else "",
                        "cwd": cwd,
                        "updated": updated_ms / 1000,
                        "status": "running" if running else "idle",
                        "parent": parent,
                        "role": "subagent" if parent else None,
                        "edited": opencode_edited(conn, sid, cwd, tables),
                    }
                )
    except sqlite3.Error as exc:
        print(f"agent-sessions: cannot read {db}: {exc}", file=sys.stderr)
        return []
    return found


def opencode_session(session_id):
    """Directory, parent and creation time OpenCode recorded for one session."""
    db = paths.opencode_db_path()
    if db is None or not db.is_file():
        return None
    try:
        with closing(
            sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            tables = {
                row[0]
                for row in conn.execute("select name from sqlite_master where type = 'table'")
            }
            for table in ("session_v2", "session"):
                if table in tables:
                    cols = {row[1] for row in conn.execute(f"pragma table_info({table})")}
                    if not {"id", "directory"} <= cols:
                        continue
                    parent_col = "parent_id" if "parent_id" in cols else "null"
                    created_col = "time_created" if "time_created" in cols else "null"
                    row = conn.execute(
                        f"select directory, {parent_col}, {created_col} from {table} "
                        "where id = ?",
                        (session_id,),
                    ).fetchone()
                    if row:
                        return {"cwd": row[0], "parent": row[1], "created": row[2]}
    except sqlite3.Error:
        return None
    return None


def sessions(minutes=paths.ACTIVE_MINUTES, include_reviews=False, within=None, host=None):
    """Active sessions, newest first; within limits them to working directories under a path."""
    from . import partners

    since = time.time() - minutes * 60
    found = []
    if host in (None, "codex"):
        found.extend(codex_sessions(since, include_reviews, within))
    if host in (None, "claude"):
        found.extend(claude_sessions(since, within))
    if host in (None, "agy"):
        found.extend(agy_sessions(since, within))
    if host in (None, "opencode"):
        found.extend(opencode_sessions(since, within))
    cache = {}
    for session in found:
        session["git"] = paths.git_state(session["cwd"], cache)
    for pair in partners.all_pairs():
        for session in found:
            for mine, peer in (
                (pair["caller"], pair["partner"]),
                (pair["partner"], pair["caller"]),
            ):
                if (mine["host"], mine["id"]) == (session["host"], session["id"]):
                    session.setdefault("partners", []).append(
                        {**peer, "pair_id": pair["id"], "status": pair["status"]}
                    )
    return sorted(found, key=lambda s: -s["updated"])


def matches_session(session, query):
    """Match a literal case-insensitive substring against session and checkout details."""
    if query is None:
        return True
    git_info = session.get("git") or {}
    fields = (
        session.get("id"),
        session.get("title"),
        session.get("cwd"),
        git_info.get("root"),
        git_info.get("common"),
        git_info.get("branch"),
    )
    needle = query.casefold()
    return any(isinstance(value, str) and needle in value.casefold() for value in fields)


def relative_files(session):
    """Uncommitted and recently edited files of a session, relative to its worktree."""
    git_info = session.get("git") or {}
    root = git_info.get("root")
    files = set(git_info.get("dirty", []))
    for path in session.get("edited", []):
        path = paths.resolved(path)
        if root and paths.inside(path, root):
            files.add(os.path.relpath(path, root))
    return files


def who(path, found):
    target = paths.git_state(path)
    if not target.get("common"):
        raise SystemExit(f"agent-sessions: {path} is not inside a git repository")
    mine = set(target["dirty"])
    peers = []
    for session in found:
        git_info = session.get("git") or {}
        if git_info.get("common") != target["common"]:
            continue
        files = relative_files(session)
        peers.append(
            {
                **session,
                "same_worktree": git_info.get("root") == target["root"],
                "overlap": sorted(files & mine),
            }
        )
    return {
        "root": target["root"],
        "branch": target["branch"],
        "linked": target["linked"],
        "dirty": len(mine),
        "sessions": peers,
    }


def ago(epoch):
    minutes = int((time.time() - epoch) // 60)
    return f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"


def describe(session, full_id=False, current_id=None):
    git_info = session.get("git") or {}
    if git_info.get("missing"):
        place = f"{paths.home(session['cwd'])} (missing)"
    elif git_info.get("root"):
        kind = "worktree" if git_info["linked"] else "main checkout"
        place = (
            f"{paths.home(git_info['root'])} "
            f"({kind}, {git_info['branch']}, {len(git_info['dirty'])} uncommitted)"
        )
    else:
        place = paths.home(session["cwd"])
    parent = f", subagent of {session['parent'][:13]}" if session.get("parent") else ""
    active_id = paths.environment_session_id() if current_id is None else current_id
    mine = " (this session)" if session["id"] == active_id else ""
    partner_notes = "".join(
        f"; partner {p['host']} {p['id'] or p['status']} in {paths.home(p['cwd'])}"
        for p in session.get("partners", [])
    )
    display_id = session["id"] if full_id else session["id"][:13]
    return (
        f"{session['host']} {display_id}{mine} {session['status']} "
        f"{ago(session['updated'])} ago{parent}: {session['title'] or '(untitled)'} -- "
        f"{place}{partner_notes}"
    )


def self_id(cwd, found):
    env_id = paths.environment_session_id()
    if env_id:
        return paths.validate_session_id(env_id)
    return session_in_directory(cwd, found)["id"]


def session_in_directory(cwd, found):
    here = [s for s in found if paths.resolved(s["cwd"]) == paths.resolved(cwd)]
    if len(here) == 1:
        return here[0]
    raise SystemExit(f"agent-sessions: {len(here)} sessions work in {cwd}; pass --session")
