"""Paths, host/environment identity, git state, validation, and atomic storage primitives."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.4.1"
ACTIVE_MINUTES = 30
TAIL_BYTES = 2_000_000
GIT_TIMEOUT = 5
HOSTS = ("codex", "claude", "agy", "opencode")
HOST_CHOICES = ("codex", "claude", "agy", "antigravity", "opencode")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
AGY_EDIT_TOOLS = (
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "notebook_edit",
    "edit_notebook",
    "sed_file",
)
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreInvocation", "PreToolUse", "PostToolUse")
NATIVE_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")
FOREIGN_SESSION_ENVS = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_AGENT",
    "OPENCODE_SESSION_ID",
)
OPENCODE_EDIT_TOOLS = ("edit", "write", "patch")
OPENCODE_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE
)
OPENCODE_PLUGIN = "agent-sessions.ts"

SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}")
MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _nearer_host(present):
    target = os.environ.get("CLAUDE_PID", "")
    claude_pid = int(target) if target.isdigit() else None
    pid = os.getppid()
    for _ in range(64):
        if claude_pid is not None and pid == claude_pid and "claude" in present:
            return "claude"
        if pid <= 1:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        parent, _, command = out.partition(" ")
        base = os.path.basename(command.strip()).lower()
        if "codex" in base and "codex" in present:
            return "codex"
        if "claude" in base and "claude" in present:
            return "claude"
        if (base == "agy" or "antigravity" in base) and "agy" in present:
            return "agy"
        if base == "opencode" and "opencode" in present:
            return "opencode"
        if not parent.isdigit():
            return None
        pid = int(parent)
    return None


def environment_session():
    """Return the current (host, ID), leaving the host unset for unresolved ID collisions."""
    codex = os.environ.get("CODEX_THREAD_ID")
    claude = os.environ.get("CLAUDE_CODE_SESSION_ID")
    agy = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
    opencode = os.environ.get("OPENCODE_SESSION_ID")
    present = {
        k: v
        for k, v in (
            ("codex", codex),
            ("claude", claude),
            ("agy", agy),
            ("opencode", opencode),
        )
        if v
    }
    if len(present) <= 1:
        return next(iter(present.items()), (None, None))
    nearer = _nearer_host(present)
    if nearer:
        return nearer, present[nearer]
    if claude and codex and claude_is_nearer():
        return "claude", claude
    if agy and os.environ.get("ANTIGRAVITY_AGENT") == "1":
        return "agy", agy
    host, session_id = next(iter(present.items()))
    if list(present.values()).count(session_id) > 1:
        return None, session_id
    return host, session_id


def environment_session_id():
    """This process's session; when one host runs inside another, the nearer host wins."""
    return environment_session()[1]


def claude_is_nearer():
    """True when CLAUDE_PID is an ancestor with no Codex process between it and this one."""
    target = os.environ.get("CLAUDE_PID", "")
    if not target.isdigit():
        return False
    pid = os.getppid()
    for _ in range(64):
        if pid == int(target):
            return True
        if pid <= 1:
            return False
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
        parent, _, command = out.partition(" ")
        if not parent.isdigit() or "codex" in os.path.basename(command.strip()).lower():
            return False
        pid = int(parent)
    return False


def validate_session_id(session_id):
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid session id; use letters, numbers, underscores and hyphens")
    return session_id


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def agy_home():
    return Path(
        os.environ.get("AGY_HOME")
        or os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
        or Path.home() / ".gemini/antigravity-cli"
    )


def agy_homes():
    """Antigravity app-data homes to scan (explicit env override, or CLI, app, and IDE defaults)."""
    explicit = os.environ.get("AGY_HOME") or os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if explicit:
        return [Path(explicit)]
    candidates = [
        Path.home() / ".gemini/antigravity-cli",
        Path.home() / ".gemini/antigravity",
        Path.home() / ".gemini/antigravity-ide",
    ]
    seen, out = set(), []
    for candidate in candidates:
        key = resolved(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def agy_config_dir():
    return Path(os.environ.get("AGY_CONFIG_DIR") or Path.home() / ".gemini/config")


def opencode_data_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode"


def opencode_db_path():
    name = os.environ.get("OPENCODE_DB", "opencode.db")
    return None if name == ":memory:" else opencode_data_dir() / name


def opencode_config_dir():
    return Path(
        os.environ.get("OPENCODE_CONFIG_DIR")
        or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    )


def data_dir():
    return Path(
        os.environ.get("AGENT_SESSIONS_DATA") or Path.home() / ".local/share/agent-sessions"
    )


def _canonicalize_case(path: Path) -> str:
    """Return the filesystem's canonical casing for path on macOS (F_GETPATH) when available."""
    if not hasattr(fcntl, "F_GETPATH"):
        return str(path)
    current = path
    tail = []
    while not current.exists() and current != current.parent:
        tail.append(current.name)
        current = current.parent
    flags = getattr(os, "O_EVTONLY", os.O_RDONLY | os.O_NONBLOCK)
    try:
        fd = os.open(current, flags)
    except OSError:
        return str(path)
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
    except (OSError, ValueError):
        return str(path)
    finally:
        os.close(fd)
    canonical = os.fsdecode(raw.split(b"\0", 1)[0])
    if not canonical:
        return str(path)
    return os.path.join(canonical, *reversed(tail)) if tail else canonical


def resolved(path):
    try:
        return _canonicalize_case(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(path)


def resolved_strict(path):
    return _canonicalize_case(Path(path).expanduser().resolve(strict=True))


def inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def home(path):
    text = str(path)
    user = str(Path.home())
    if inside(text, user):
        return "~" + text[len(user) :]
    if Path(text).is_absolute():
        canonical_text = resolved(text)
        canonical_user = resolved(user)
        if inside(canonical_text, canonical_user):
            return "~" + canonical_text[len(canonical_user) :]
    return text


def ancestors(path):
    return [path, *(str(parent) for parent in Path(path).parents)]


def new_id():
    """A 32-character id that sorts by creation time."""
    return f"{time.time_ns():016x}{uuid.uuid4().hex[:16]}"


def now(timespec="auto"):
    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def queue_dir(key, box="new"):
    return data_dir() / "queue" / key / box


def session_key(session_id):
    return f"s-{validate_session_id(session_id)}"


def path_key(root):
    return f"p-{hashlib.sha256(root.encode()).hexdigest()[:16]}"


def message_file(directory, message_id):
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        raise ValueError("invalid message id")
    return directory / f"{message_id}.json"


def receipt_dir(message_id):
    return data_dir() / "receipts" / message_id


def state_path(session_id):
    return data_dir() / "state" / f"{validate_session_id(session_id)}.json"


def pair_path(pair_id):
    if not re.fullmatch(r"[a-f0-9]{32}", pair_id):
        raise ValueError("invalid pair id; use the full id returned by partner")
    return data_dir() / "pairs" / f"{pair_id}.json"


def private_parent(path):
    """Create private storage without changing permissions on unrelated parent folders."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = data_dir()
    if root.exists() and inside(str(path.resolve()), str(root.resolve())):
        path.parent.chmod(0o700)
        root.chmod(0o700)


def private_dir(path):
    """Create path and its missing parents under the data directory as owner-only folders."""
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
    return path


@contextmanager
def locked(path, blocking=True):
    private_parent(path)
    descriptor = os.open(
        path.with_suffix(path.suffix + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600
    )
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def write_json(path, value):
    private_parent(path)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".agent-sessions-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def git(directory, *args):
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", directory, *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )


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
            out = git(
                key,
                "rev-parse",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
                "--abbrev-ref",
                "HEAD",
            )
            if out.returncode == 0:
                root, gitdir, common, branch = out.stdout.splitlines()[:4]
                gitdir, common = (resolved(Path(key) / p) for p in (gitdir, common))
                status = git(root, "-c", "core.quotePath=false", "status", "--porcelain")
                dirty = []
                for line in status.stdout.splitlines():
                    if len(line) > 3:
                        item = line[3:].split(" -> ")[-1]
                        if len(item) >= 2 and item[0] == '"' and item[-1] == '"':
                            try:
                                item = json.loads(item)
                            except ValueError:
                                pass
                        dirty.append(item)
                state = {
                    "root": resolved(root),
                    "common": common,
                    "branch": branch,
                    "linked": gitdir != common,
                    "dirty": dirty,
                }
        except (OSError, subprocess.SubprocessError, ValueError):
            state = {}
    cache[key] = state
    return state
