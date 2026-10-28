Let a phone continue a paused agent and read the context usage

Two additive, capability-gated Team API routes: agent-pause-continue-v1 (continue an agent that an agent-pause marker holds) and context-usage-v1 (read the context usage and compaction state). Both have frozen wire codecs, a chat authorization level, strict client decoding, and E2E for allowed, refused, hidden, unknown, capability-absent and malformed callers.
