# Executor operation

The default models are selected for the accounts checked during this recovery. Model
catalogues and billing can change. Codex probes the signed-in account's model list;
Copilot `auto` reports the selected model in one-shot attempt metadata. Cursor's
one-shot default requests Composer 2.5 standard speed. Cursor ACP currently exposes
Composer 2.5 fast as its default, so its cost can differ from one-shot runs.

Pi uses a Linux bubblewrap launcher and a single-model MiniMax broker. The broker runs
on the host that already has the Pi credential; the worker never receives that
credential. The worker needs bubblewrap, socat, and a Pi 0.73-compatible installation.
The launcher disables extension, skill and context discovery, starts with an empty
environment, and lets the sandbox reach only the broker socket. The broker accepts
only the configured model and the MiniMax messages endpoint.
Each broker process accepts at most four concurrent inference requests and refuses a
fifth with HTTP 503 until one of them finishes. The broker drops a request body that
stalls for 20 seconds (`OPENSWARM_BROKER_UPLOAD_TIMEOUT_MS` overrides that bound);
the limit covers the upload only, so a slow inference may take longer.

From a machine with the existing SSH identities, start the bounded bridge:

```sh
bin/pi-fleet-bridge --broker-host gocc --worker gores1 --minutes 120
```

The command prints the worker socket path. On the worker, set
`OPENSWARM_PI_BROKER_SOCKET` to that path and `OPENSWARM_PI_RUNTIME` to the isolated
Pi installation prefix, then run the candidate's `bin/openswarm-fleet`. This uses a
separate OpenSwarm state directory, preserving the existing fleet configuration. Keep the bridge
alive for the run. Interrupting it or reaching its time limit closes its SSH sessions
and removes its private sockets. It is a transport process, not an executor.

Pi read-only review and consult run only through the isolated launcher; without it
they are rejected. One-shot isolated Pi cannot resume a rate-limited session;
resident RPC Pi keeps its session inside the sandbox. A missing bridge, unsupported
host, unapproved model or unisolated binary fails closed. Do not turn off
`pi.require_isolation` on a host containing credentials or other valuable data.

## Lane restart recovery

A `lane serve` restart (a stalled restart, a host blip, a broker disconnect) used to void the run
that was in flight: the burned tokens and the half-finished change were gone and the card had to
be dispatched again from zero. The lane supervisor now persists a resume record per in-flight run
(`<run dir>/resume.json`: branch, pushed commit, task file, workspace path, session identity) and,
when it starts again, tries to resume the run it had in flight.

The recovery is graded and conservative, and it never guesses:

* a run whose branch carries a pushed commit resumes from the branch tip;
* a run with only uncommitted workspace changes resumes from that state (the workspace is not
  reset or cleaned) and is marked as recovered from the uncommitted state;
* a run whose state does not line up with the dispatcher (the issue is finished, the card was
  re-dispatched, the lane got another run) is abandoned with a written reason.

The authoritative dispatcher state is read from `OPENSWARM_AD_STATE`, or from the phanes
autodispatch state and the dispatch engine state when the host has them. It decides whether the card
is still in progress on this lane, whether the issue is already finished and whether it was
re-dispatched to another lane in the meantime. When no dispatcher state is readable, the state the
lane left behind names the run the lane was executing: that run may be resumed, and the brain log
says that the dispatcher state was not checked. A run that neither the dispatcher state nor the lane
state claims is left alone instead of being resumed or failed on a guess. Every recovery attempt
writes one line to the brain log (run id, grade, recovery point). A failed recovery never keeps the
lane from taking new work.

The session of the interrupted run is restored as well: Pi switches back into the recorded session
file, Codex resumes the recorded thread, and a headless executor continues its conversation. A
backend that cannot restore its session starts a new one and says so in the event log.
`lane.resume_grace_minutes` (default 10) is the extra time a recovered run may use after its
original budget ran out.

Copilot on macOS uses the normal GitHub device login. If the system keychain is
unavailable, its CLI may store the credential in plaintext under `COPILOT_HOME`;
restrict the directory and files to the owning account and treat that location as a
credential store. The Copilot sandbox restricts shell commands; built-in file edits
use the CLI's best-effort policy rather than an OS sandbox.
