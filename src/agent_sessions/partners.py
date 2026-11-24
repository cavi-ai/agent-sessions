"""Partner pair lifecycle, guarded profile launching, and completion records."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths, profiles, queue


def read_pair(pair_id):
    pair = json.loads(paths.pair_path(pair_id).read_text(encoding="utf-8"))
    if not isinstance(pair, dict) or pair.get("id") != pair_id or pair.get("status") not in (
        "needs_creation",
        "needs_delivery",
        "waiting",
        "connected",
        "needs_profile_launch",
        "profile_launch_pending",
        "completed",
    ):
        raise ValueError(f"invalid pair record: {pair_id}")
    for key in ("caller", "partner"):
        end = pair.get(key)
        if (
            not isinstance(end, dict)
            or "id" not in end
            or end.get("host") not in paths.HOSTS
            or not isinstance(end.get("cwd"), str)
            or not Path(end["cwd"]).is_absolute()
        ):
            raise ValueError(f"invalid pair endpoint: {pair_id}")
        if (
            key == "caller"
            or pair["status"] in ("connected", "completed")
            or end.get("id") is not None
        ):
            paths.validate_session_id(end.get("id"))
    if pair.get("profile") is not None:
        profiles.identifier(pair["profile"])
    if pair["status"] == "completed":
        completion = pair.get("completion")
        if (
            not isinstance(completion, dict)
            or set(completion) != {"at", "by", "native_state"}
            or not isinstance(completion["at"], str)
            or completion["by"] not in (None, pair["caller"]["id"], pair["partner"]["id"])
            or completion["native_state"]
            not in ("closed", "archived", "archive_failed", "retained", "stopped")
        ):
            raise ValueError("invalid pair completion record")
        try:
            stamp = datetime.fromisoformat(completion["at"])
            if stamp.utcoffset() is None:
                raise ValueError("missing timezone")
        except ValueError as error:
            raise ValueError("invalid pair completion time") from error
    return pair


def all_pairs():
    pairs = []
    for path in sorted((paths.data_dir() / "pairs").glob("*.json")):
        try:
            pairs.append(read_pair(path.stem))
        except (OSError, ValueError, TypeError) as exc:
            print(f"agent-sessions: skipping unreadable pair {path.stem}: {exc}", file=sys.stderr)
    return pairs


def completed_workers(pairs):
    """Worker identities whose completed pairs have no remaining open pair."""
    closed = {
        (p["partner"]["host"], p["partner"]["id"]) for p in pairs if p["status"] == "completed"
    }
    opened = {
        (endpoint["host"], endpoint["id"])
        for p in pairs
        if p["status"] != "completed"
        for endpoint in (p["caller"], p["partner"])
    }
    return closed - opened


def record_pair_completion(pair, completed_by, native_state):
    """Record a terminal pair state while the caller holds its lock."""
    if pair.get("status") != "connected":
        raise ValueError("completion requires a connected pair")
    if completed_by not in (None, pair["caller"]["id"], pair["partner"]["id"]):
        raise ValueError("only a pair endpoint can complete the partner")
    if native_state not in ("closed", "archived", "archive_failed", "retained", "stopped"):
        raise ValueError("invalid native completion state")
    pair["completion"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "by": completed_by,
        "native_state": native_state,
    }
    pair["status"] = "completed"


def worker_has_open_references(pair):
    identity = (pair["partner"]["host"], pair["partner"]["id"])
    return any(
        other["id"] != pair["id"]
        and other["status"] != "completed"
        and any((end["host"], end["id"]) == identity for end in (other["caller"], other["partner"]))
        for other in all_pairs()
    )


def _stop_claude_background(pair):
    partner = pair["partner"]
    if partner["host"] != "claude":
        raise ValueError("--stop requires a Claude background partner")
    try:
        session_id = str(uuid.UUID(partner["id"]))
    except (ValueError, AttributeError):
        raise ValueError("Claude background partner requires a full session UUID") from None
    if session_id != partner["id"]:
        raise ValueError("Claude background partner requires a canonical session UUID")
    environment = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "CLAUDE_CONFIG_DIR")
        if key in os.environ
    }
    try:
        result = subprocess.run(
            ["claude", "agents", "--json", "--all"],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1_000_000:
            raise ValueError("Claude background inventory is unavailable")
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise TypeError("Claude background inventory is invalid")
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("kind") == "background"
            and row.get("sessionId") == session_id
            and isinstance(row.get("cwd"), str)
            and paths.resolved(row["cwd"]) == paths.resolved(partner["cwd"])
        ]
        if len(matches) != 1 or matches[0].get("state") != "done":
            raise ValueError("Claude background session is not confirmed done")
        job_id = matches[0].get("id")
        if (
            not isinstance(job_id, str)
            or not re.fullmatch(r"[A-Za-z0-9]{8}", job_id)
            or sum(isinstance(row, dict) and row.get("id") == job_id for row in rows) != 1
        ):
            raise ValueError("Claude background job identity is ambiguous")
        stopped = subprocess.run(
            ["claude", "stop", job_id],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if stopped.returncode != 0:
            raise ValueError("Claude background stop failed")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ValueError("Claude background status or stop is unavailable") from error


def complete_partner(pair_id, completed_by=None, stop=False):
    if completed_by is not None:
        paths.validate_session_id(completed_by)
    with paths.locked(paths.pair_path(pair_id), blocking=False):
        pair = read_pair(pair_id)
        if completed_by not in (None, pair["caller"]["id"], pair["partner"]["id"]):
            raise ValueError("only a pair endpoint can complete the partner")
        if stop and pair["partner"]["host"] != "claude":
            raise ValueError("--stop requires a Claude background partner")
        if pair["status"] == "completed":
            if stop and pair["completion"]["native_state"] != "stopped":
                raise ValueError("pair is already completed without a confirmed stop")
            return pair
        if pair["status"] != "connected":
            raise ValueError("completion requires a connected pair")
        last_run = pair.get("last_run")
        if last_run is not None and (
            not isinstance(last_run, dict) or last_run.get("status") != "completed"
        ):
            raise ValueError("managed run must complete before the pair can be completed")
        if stop:
            if worker_has_open_references(pair):
                raise ValueError("Claude worker is still used by another open pair")
            _stop_claude_background(pair)
        record_pair_completion(pair, completed_by, "stopped" if stop else "retained")
        paths.write_json(paths.pair_path(pair_id), pair)
        return pair


def create_partner(host, directory, brief, caller_id, found, project_id=None, profile_id=None):
    """Persist a native Codex creation request or a Claude/Antigravity/OpenCode path invitation."""
    host = profiles.normalize_host(host)
    if host not in paths.HOSTS:
        raise ValueError(f"unsupported partner host: {host}")
    caller = next((s for s in found if s["id"] == caller_id), None)
    if caller is None:
        raise ValueError("caller is not a discovered session; check --session and --minutes")
    root = paths.resolved(directory)
    if not Path(root).is_dir():
        raise ValueError(f"partner directory does not exist: {root}")
    if not brief.strip():
        raise ValueError("partner brief must not be empty")
    if profile_id:
        profile = profiles.load(paths.data_dir(), profile_id)
        if profile["host"] != host or profile["cwd"] != root:
            raise profiles.ProfileError("partner host/directory does not match the profile")
    if host == "codex" and not project_id and not profile_id:
        raise ValueError("Codex partners require --project-id from codex_app list_projects")
    pair = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "needs_creation" if host == "codex" else "needs_delivery",
        "caller": {
            "host": caller["host"],
            "id": caller["id"],
            "cwd": paths.resolved(caller["cwd"]),
        },
        "partner": {"host": host, "id": None, "cwd": root},
    }
    command = shlex.join([sys.executable, "-m", "agent_sessions", "partner", "connect", pair["id"]])
    prompt = (
        f"{brief}\n\nPartner pair: {pair['id']}. Caller: {caller['host']} {caller_id}. "
        f"Work in {root}. Record this session as the partner with:\n{command}\n"
        "This connection records routing metadata; it does not authorize additional work."
    )
    path = paths.pair_path(pair["id"])
    if profile_id:
        bootstrap = (
            f"Register this partner session only. Pair: {pair['id']}. "
            f"Caller: {caller['host']} {caller_id}. Work in {root}.\n"
            f"Connect with:\n{command}\n"
            "Report the connection result, then wait for the caller to send work. "
            "Do not start other work if connection fails."
        )
        pair.update(
            profile=profile_id,
            status="needs_profile_launch",
            invitation=bootstrap,
            brief=brief,
            launch={
                "argv": [
                    sys.executable,
                    "-m",
                    "agent_sessions",
                    "profile",
                    "launch",
                    profile_id,
                    "--pair",
                    pair["id"],
                ],
                "cwd": root,
            },
        )
        with paths.locked(path):
            paths.write_json(path, pair)
    elif host == "codex":
        pair["request"] = {
            "tool": "mcp__codex_app__create_thread",
            "arguments": {
                "target": {
                    "type": "project",
                    "projectId": project_id,
                    "environment": {"type": "local"},
                },
                "prompt": prompt,
            },
        }
        with paths.locked(path):
            paths.write_json(path, pair)
    else:
        # The lock keeps an immediate recipient from connecting before the record exists.
        # Preserve pending state on failure: an append may have succeeded before an I/O error.
        pair["invitation"] = prompt
        with paths.locked(path):
            paths.write_json(path, pair)
            queue.send(root, prompt, found, sender=caller_id, pair_id=pair["id"])
            pair["status"] = "waiting"
            paths.write_json(path, pair)
    return pair


def check_partner_profile(pair, session_id):
    profile = profiles.load(paths.data_dir(), pair["profile"])
    receipt = profiles.binding(paths.data_dir(), session_id)
    if (
        not receipt
        or receipt["profile"] != profile["id"]
        or receipt["state"] != "configured"
        or receipt["host"] != profile["host"]
        or receipt["cwd"] != profile["cwd"]
    ):
        raise profiles.ProfileError(
            "partner has no matching profile launch receipt; use profile launch"
        )
    return profile


def attach_partner(session_id, caller_id, found, profile_id=None):
    paths.validate_session_id(session_id)
    caller = next((s for s in found if s["id"] == caller_id), None)
    peer = next((s for s in found if s["id"] == session_id), None)
    if not caller or not peer or session_id == caller_id:
        raise ValueError("attach requires two different discovered sessions")
    peer_cwd = paths.resolved(peer["cwd"])
    pair = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "connected",
        "caller": {
            "host": caller["host"],
            "id": caller["id"],
            "cwd": paths.resolved(caller["cwd"]),
        },
        "partner": {"host": peer["host"], "id": peer["id"], "cwd": peer_cwd},
    }
    if peer["host"] == "codex":
        pair["partner"]["host_id"] = "local"
    if profile_id:
        pair["profile"] = profile_id
        profile = check_partner_profile(pair, session_id)
        if profile["host"] != peer["host"] or profile["cwd"] != peer_cwd:
            raise profiles.ProfileError("existing worker host/directory does not match the profile")
    for existing in all_pairs():
        if (
            existing["caller"]["id"] == caller_id
            and existing["partner"]["id"] == session_id
            and existing.get("profile") == profile_id
            and existing["status"] == "connected"
        ):
            if paths.resolved(existing["partner"]["cwd"]) != peer_cwd:
                raise ValueError("existing partner directory changed; inspect before reconnecting")
            return existing
    with paths.locked(paths.pair_path(pair["id"])):
        paths.write_json(paths.pair_path(pair["id"]), pair)
    return pair


def launch_profile(
    profile_id,
    session_id=None,
    pair_id=None,
    dry_run=False,
    prompt=None,
    headless=False,
    output_format=None,
):
    profile = profiles.load(paths.data_dir(), profile_id)
    if session_id:
        paths.validate_session_id(session_id)
        if (profile["host"], session_id) in completed_workers(all_pairs()):
            raise profiles.ProfileError("completed worker requires a new active pair before resume")
        receipt = profiles.binding(paths.data_dir(), session_id)
        if not receipt or receipt["profile"] != profile_id:
            raise profiles.ProfileError("session is not bound to this profile")
    if pair_id:
        pair = read_pair(pair_id)
        if pair.get("profile") != profile_id:
            raise profiles.ProfileError("pair uses a different profile")
        if pair["status"] == "completed":
            raise profiles.ProfileError("completed pair cannot be resumed")
        if not session_id:
            if headless or prompt is not None or output_format is not None:
                raise profiles.ProfileError(
                    "cannot combine --print/--headless with initial --pair bootstrap"
                )
            if pair["status"] != "needs_profile_launch":
                raise profiles.ProfileError(
                    "partner launch outcome may exist; inspect and resume, never replay"
                )
            prompt = pair["invitation"]
    command = profiles.argv(
        profile, session_id, prompt, headless=headless, output_format=output_format
    )
    marker = {"AGENT_SESSIONS_PROFILE_ID": profile_id}
    if dry_run:
        return {
            "profile": profile_id,
            "host": profile["host"],
            "cwd": profile["cwd"],
            "argv": command,
            "environment": marker,
        }
    env = {**os.environ, **marker}
    for var in paths.FOREIGN_SESSION_ENVS:
        env.pop(var, None)
    if profile["host"] == "opencode":
        # Keep native project configuration enabled and ignore inherited overrides.
        for var in (
            "OPENCODE_CONFIG_PROJECT_DISABLE",
            "OPENCODE_DISABLE_PROJECT_CONFIG",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_CONTENT",
        ):
            env.pop(var, None)

    def attempt(original, pair=None):
        try:
            os.chdir(profile["cwd"])
            os.execvpe(command[0], command, env)
        except OSError:
            try:
                os.fchdir(original)
            finally:
                if pair is not None:
                    pair["status"] = "needs_profile_launch"
                    paths.write_json(paths.pair_path(pair_id), pair)
            raise

    # Python creates non-inheritable descriptors, so successful exec releases the pair lock.
    original = os.open(".", os.O_RDONLY)
    try:
        if pair_id and not session_id:
            with paths.locked(paths.pair_path(pair_id)):
                pair = read_pair(pair_id)
                if pair["status"] != "needs_profile_launch":
                    raise profiles.ProfileError(
                        "partner launch already attempted; inspect before retrying"
                    )
                pair["status"] = "profile_launch_pending"
                paths.write_json(paths.pair_path(pair_id), pair)
                attempt(original, pair)
        else:
            attempt(original)
    finally:
        os.close(original)


def claim_invitation(pair):
    """Remove a connected pair's invitation from its directory queue and tell the caller."""
    directory = paths.queue_dir(paths.path_key(pair["partner"]["cwd"]))
    for name in queue.listing(directory):
        message = queue.read_message(directory / name)
        if message and message.get("pair_id") == pair["id"]:
            queue.take(
                directory / name,
                message,
                pair["partner"]["id"],
                f"ready: {pair['partner']['id']} connected to pair {pair['id']}",
            )


def connect_partner(pair_id, session_id, caller_id, found, host_id=None):
    """Connect a pair to a discovered session, validating host and working directory."""
    paths.validate_session_id(session_id)
    with paths.locked(paths.pair_path(pair_id)):
        pair = read_pair(pair_id)
        if caller_id not in (pair["caller"]["id"], session_id):
            raise ValueError("only a pair endpoint can record the connection")
        if session_id == pair["caller"]["id"]:
            raise ValueError("a session cannot be its own partner")
        if host_id and (pair["partner"]["host"] != "codex" or host_id != "local"):
            raise ValueError("only local Codex partners support --host-id local")
        if pair["status"] == "completed":
            raise ValueError("completed pair cannot be reconnected")
        if pair.get("profile"):
            check_partner_profile(pair, session_id)
        if pair["status"] == "connected":
            if pair["partner"]["id"] != session_id:
                raise ValueError("pair already connected to another session")
            if host_id:
                pair["partner"]["host_id"] = host_id
                paths.write_json(paths.pair_path(pair_id), pair)
            claim_invitation(pair)
            return pair
        peer = next((s for s in found if s["id"] == session_id), None)
        expected = pair["partner"]
        if peer is None or peer["host"] != expected["host"]:
            raise ValueError("partner must be a discovered session of the requested host")
        matches = (
            paths.resolved(peer["cwd"]) == expected["cwd"]
            if peer["host"] == "codex"
            else paths.inside(paths.resolved(peer["cwd"]), expected["cwd"])
        )
        if not matches:
            raise ValueError("partner session is working in a different directory")
        pair["partner"]["id"] = session_id
        if host_id:
            pair["partner"]["host_id"] = host_id
        pair["status"] = "connected"
        paths.write_json(paths.pair_path(pair_id), pair)
        claim_invitation(pair)
    return pair
