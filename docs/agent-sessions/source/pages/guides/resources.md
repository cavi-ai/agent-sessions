# Queued resource admission

Resource contention is control state. Use a consuming supervisor to request one
ticket, wait for admission, launch once and record its exit before releasing the
resource. Waiting does not deliver peer messages or start model turns.

The broker is host-local and uses the existing `AGENT_SESSIONS_DATA` directory.
Use the same production directory across projects and worktrees. Test fixtures
use separate directories. Each resource name has one exclusive holder and a FIFO
queue. There is no background service or additional MCP registration.

## Native supervisors

The supervisor calls `resource request`, `wait`, `start` and `finish`. A request key identifies one attempt; retrying that same
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

Queued cancellation and expiry never start work. An ordinary dead prelaunch owner can be
removed. A resumable queued request retains its place until cancellation or expiry.
A launch-intent owner that disappears becomes `unresolved` and keeps the
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
agent-sessions resource events --lifecycle --after 0
```

The event feed contains only the caller's terminal or unresolved outcomes, at
most 64 per page. Keep the returned cursor before reading another page. Event
identities are stable; repeated completion does not append another event.
Queue position, grant and start transitions remain bookkeeping. No event is
turned into a peer message or a new model turn by this command.

`--lifecycle` also includes one timestamped `start` signal at committed launch
intent and an `end` or `attention` signal. They carry the same ticket and lease
generation. Retrying start or completion does not duplicate signals. Start does
not independently prove that an application was created; the supervisor owns
process evidence. Terminal-only events remain the default.
Only the first atomic start transition returns `launch_permitted: true`.
A replay acknowledges the same intent with `false`; consumers must refuse a
second process creation. This gate is keyed by the authoritative ticket in its
broker store, including when two checkpoint paths refer to the same attempt.

## Queue advice and compaction

Request with a stable logical owner and key, `--resumable`, a job-input
`--payload-sha256`, and optionally `--estimate-seconds`. Queue deadlines use
`--seconds` up to 86400; one blocking wait window is limited to 3600 seconds.
The response includes a UTC creation time, queue sequence, `jobs_ahead`, distinct
`sessions_ahead`, estimated start/wait, estimate basis and `retry_after_seconds`.
The broker sums earlier declared runtimes or the median of its last 20 verified
completed runtimes, subtracting elapsed time for the running holder. Missing
history, an overrun or unresolved cleanup gives an unknown ETA and a bounded
next-check hint. An estimate never releases a lock or grants admission.

Save the private ticket checkpoint, compact, then sleep for the broker's suggested
interval. Inspect the same ticket after waking. `wait --keep` can return a still
queued ticket without cancelling it. Resumable queued requests survive requester
exit; the supervising process binds its own PID/birth identity when it claims
the grant. Running work remains process-bound. A new attempt needs a new key;
changed payload, estimate or deadline cannot silently reuse the old key.

The packaged `resource_client.ResourceClient` supplies request, wait, start,
finish, scoped events and token-free checkpoint/resume helpers. From a source
checkout, try the fixed subprocess adapter:

```sh
python3 examples/shared_resource.py submit --handle /absolute/job.json --key render-attempt-1 --work-seconds 10
python3 examples/shared_resource.py status --handle /absolute/job.json
python3 examples/shared_resource.py run --handle /absolute/job.json
```

The example accepts no application or shell command. A project adapter replaces
its fixed workload and exit proof with the project's trusted native supervisor.
Keep the supervisor resident through admission and cleanup; a detached model
check must inspect advice rather than acquire an unused grant. Repeated completed
work uses its existing proof, while uncertain work refuses replay. Upgrade broker
implementations without leaving an old in-process wait loop active.

Use [messaging](messaging.md) for decisions, source-ownership handoffs and questions
that cannot be represented as job state. Resource requests, waiting notices,
release notices and acknowledgement chains belong in the broker path.

Logical owner IDs route control records within one OS user's store; they are not
an OS-user authentication boundary. Invoke release only from a trusted supervisor.
A resource completion records cleanup, not test, art or gameplay acceptance.
