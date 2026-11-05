# Partner sessions

A partner record connects a caller and another native session. Records preserve
each endpoint's host, session ID and directory. `list` and `who` show the relationship
from both ends. One caller can have several partners across both hosts.

## Choose a route

| You want to… | Use |
| --- | --- |
| Invite a new Claude session under a directory | `partner claude` |
| Create a Codex chat through the desktop app | `partner codex --project-id` |
| Register a worker that already exists | `partner attach` |
| Launch or resume with pinned configuration | [Worker profiles](profiles.md) |

## Invite a Claude partner

```sh
agent-sessions partner claude /path/to/project 'Review the parser'
```

This saves a pair in `waiting` state and queues an invitation. It does not launch
Claude. Start a new Claude session under that directory with the hooks installed.
The invitation supplies this connection command:

```sh
agent-sessions partner connect PAIR_ID
```

Only sessions that start after the invitation was created receive it. Resuming or
compacting an already running session does not make it eligible. The first valid
connection claims the invitation and replies `ready` to the caller; later sessions
no longer receive it. Use `partner connect`, not plain `claim`, for invitations.

## Create a Codex partner

This route requires an agent in the Codex desktop app with its native chat tools.
The shell CLI prepares a request; the agent executes it through the host.

1. Call `mcp__codex_app__list_projects` with `{}`. Select a saved local project
   whose path equals the requested directory.
2. Use that returned project ID:

   ```sh
   agent-sessions partner codex /path/to/project 'Review the parser' --project-id PROJECT_ID
   ```

3. The result is `needs_creation` with `request.tool` and `request.arguments`.
   Invoke the actual `mcp__codex_app__create_thread` tool with those arguments:

   ```json
   {
     "target": {
       "type": "project",
       "projectId": "PROJECT_ID",
       "environment": {"type": "local"}
     },
     "prompt": "THE_RETURNED_PROMPT"
   }
   ```

4. After native creation returns a real `threadId` and `hostId`, connect it:

   ```sh
   agent-sessions partner connect PAIR_ID THREAD_ID --host-id local
   ```

The new partner can also connect itself using `partner connect PAIR_ID`.
Connection validates its discovered host and directory. A pending `clientThreadId`
is not a usable `threadId`.

Creation uses the existing checkout and configured model. If creation fails or its
outcome is uncertain, inspect the app before retrying; another creation call can
create a second chat.

## Attach or add more partners

Register an existing session without creating one:

```sh
agent-sessions partner attach SESSION_ID
```

Attachment validates its discovered identity and directory. Repeating the same
attachment reuses its pair. To add more partners, repeat creation or attachment
for each worker. Each relationship gets its own pair ID; it does not replace the
caller's other partners.

Inspect any saved pair:

```sh
agent-sessions partner show PAIR_ID
```

## Follow-up work

`send` queues a message without starting a turn:

```sh
agent-sessions send SESSION_ID 'Review the next parser change'
```

For a human-authorized native Codex follow-up, call
`mcp__codex_app__send_message_to_thread` with these argument names:

```json
{"threadId": "THREAD_ID", "hostId": "local", "prompt": "FOLLOW_UP_TEXT"}
```

Native creation and follow-ups appear as visible chats or prompts. Peer invitations
do not supply the human authorization those operations require.

## Pair states

| State | Meaning | Next action |
| --- | --- | --- |
| `needs_creation` | A Codex request is saved | Execute the native request, then connect its result |
| `needs_delivery` | Queueing a Claude invitation did not complete | Inspect the saved invitation and storage error |
| `waiting` | A Claude invitation is queued | Start an eligible Claude session and connect it |
| `needs_profile_launch` | A profiled partner is prepared | Run its guarded launch command once |
| `profile_launch_pending` | A guarded launch was attempted | Inspect the native session before retrying |
| `connected` | Both native endpoints are recorded | Send work or resume a bound profile |

For guarded launches and resume, continue with [Worker profiles](profiles.md).
