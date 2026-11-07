# Queued resource admission

Resource contention is control state. Use a consuming supervisor to request one
ticket, wait for admission, launch once and record its exit before releasing the
resource. Waiting does not deliver peer messages or start model turns.

The broker is host-local and uses the existing `AGENT_SESSIONS_DATA` directory.
Use the same production directory across projects and worktrees. Test fixtures
use separate directories. Each resource name has one exclusive holder and a FIFO
queue. There is no background service or additional MCP registration.

## Native supervisors

The supervisor calls `resource request`, `wait`, `start` and `finish` from one
resident owner process. A request key identifies one attempt; retrying that same
request is idempotent. A new explicit attempt needs a new key. The logical owner,
parent process birth identity, opaque lease and increasing generation bind the
handoff. A message claim does not allocate a resource.

Admission is separate from native execution authority, project locks and memory
supervision. Validate mutable inputs again after admission, immediately before
launching. The native harness must persist and sync its verified cleanup record
before `finish`; an evidence digest is a reference to that supervisor record,
not independent proof of native exit. Treat lease values as control capabilities
and keep them out of shared reports. A recovery process retains the receipt's
logical owner with `resource --session OWNER ...` and still needs the current
lease and generation.

Queued cancellation and expiry never start work. A dead prelaunch owner can be
removed. A launch-intent owner that disappears becomes `unresolved` and keeps the
resource. Heartbeat age or a chat message cannot release it. Reconcile through
the consuming supervisor's verified exit record; never replay uncertain work.

A resident native application holds its grant for its lifetime. Commands against
that exact owned application reuse its binding rather than entering a second
exclusive queue. Pre-existing applications remain with their operators; admission
must not adopt, close or preempt them.

## Inspect results

```sh
agent-sessions resource protocol
agent-sessions resource status TICKET
agent-sessions resource events --after 0
```

The event feed contains only the caller's terminal or unresolved outcomes, at
most 64 per page. Keep the returned cursor before reading another page. Event
identities are stable; repeated completion does not append another event.
Queue position, grant and start transitions remain bookkeeping. No event is
turned into a peer message or a new model turn by this command.

Use [messaging](messaging.md) for decisions, source-ownership handoffs and questions
that cannot be represented as job state. Resource requests, waiting notices,
release notices and acknowledgement chains belong in the broker path.

Logical owner IDs route control records within one OS user's store; they are not
an OS-user authentication boundary. Invoke release only from a trusted supervisor.
A resource completion records cleanup, not test, art or gameplay acceptance.
