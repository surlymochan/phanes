### Added

- Pause and resume an agent's queue. A paused queue keeps and lists every message, and the current
  work continues. Delete all queued messages at once.
- Run a queued message now: the current work stops and the message starts next, also when the queue
  is paused. "Stop and send" in the composer does the same for a new message.
- Agents can steer or interrupt a busy teammate with `send_message`. Each use is logged, and
  interrupts have a cooldown and a limit so that agents cannot stop each other in a loop.

### Changed

- A message to a busy agent now steers the running turn by default, so the agent reads it at its
  next step. Agents and the app setting that you set to "queue" keep that setting.
