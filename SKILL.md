---
name: agent-sessions
description: Use before changing a repository that another Claude or Codex session may be working in, before moving or deleting shared code, or when asked to message, find or coordinate with another agent session. Lists active sessions with their worktree, branch and uncommitted files, and leaves messages for them.
---

# agent-sessions

`agent_sessions.py` sits next to this file.

- Before editing a repository, run `python3 <this folder>/agent_sessions.py who <repo path>`. If another session is marked `SAME WORKTREE`, or is changing the files you are about to change, message it and work in your own worktree.
- To list everything active: `agent_sessions.py list`.
- To message a session: `agent_sessions.py send <session id or path> '<text>'`. The message reaches that session the next time its hook runs or its agent runs `inbox`.
- To read messages for this session: `agent_sessions.py inbox`. In Codex, pass `--session <thread id>` when several threads share the working directory.
