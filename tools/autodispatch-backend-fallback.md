# Autodispatch backend starvation fallback

Canonical runtime: `~/phanes-ctl/bin/autodispatch.py` on the box.
Repo mirror: [`tools/lane-autodispatch/autodispatch.py`](lane-autodispatch/autodispatch.py) (keep in sync when rules change).
Unit proof: [`tools/lane-autodispatch/test_can_take.py`](lane-autodispatch/test_can_take.py).

## Rules (Refs #232)

When picking the next `lane-ready` issue for a lane whose executor family is `backend` (`pi` | `agy` | `codex`):

1. **Prefer exact pin.** Issues labelled `backend:<same>` rank first.
2. **Then unpinned.** Issues with **no** `backend:*` label are always eligible (starvation fallback).
3. **Cross-pin (optional).** Issues pinned to another backend are eligible **unless** they carry `strict-backend`. This keeps agy/codex lanes busy when the queue is pi-heavy (and vice versa).
4. **Codex socket guard.** Even when cross-pin is allowed, a `codex` lane never takes work whose title/body matches Unix-socket / `AF_UNIX` / `socketpair` / `needs-socket` / `套接字` (codex sandbox blocks `AF_UNIX` bind).
5. **Priority within a rank.** `p0` < `p1` < `p2` < unlabelled, then lower issue number.

Ranking key (lower is better): `(0=own pin, 1=unpinned, 2=cross-pin, prio, number)`.

## Opt-out

Add label `strict-backend` on an issue to forbid cross-pin. Exact pin and unpinned still apply.

## Policy (Refs #1361)

The openswarm policy can narrow the cross-pin rule: `dispatch.fallback` maps a pinned backend to
the backends its cards may fall back to (in the order they are tried) and `dispatch.fallback_wait_s`
is how long the pinned backend gets an idle lane first (`dispatch.fallback_strict` switches the
fallback off). A paused backend (`PAUSE-BACKEND-<backend>`) is skipped.

Sources, the same ones the openswarm CLI uses (`load_policy` in `openswarm/bin/openswarm`):

1. the base policy of the install of the openswarm CLI (`<install>/policy.json`) — the install the
   CLI derives from its own path (`SKILL_DIR`, the parent of its `bin` directory). The dispatcher
   finds the same install: `$OPENSWARM_SKILL_DIR` names it explicitly, otherwise the `openswarm`
   CLI on the `PATH` is used, otherwise the install of the box (`~/.claude/skills/openswarm`);
2. the local override of the data directory (`$OPENSWARM_HOME/policy.local.json`), merged on top
   of the base policy.

Only **one** base policy is read: the CLI knows the policy of its own install only, so a second
install (the openswarm tree of this repository, for instance) must not be merged in — the CLI and
the dispatcher would dispatch differently.

A `policy.json` in the data directory is **not** a policy source: a stale copy there must not
drop the published fallback rules. `AD_FALLBACK_POLICY` overrides everything with a complete
policy file.

## Sync steps (box ↔ repo)

```bash
# after editing box runtime:
cp ~/phanes-ctl/bin/autodispatch.py /workspace/phanes-repo/tools/lane-autodispatch/autodispatch.py
# or after merging a PR that changes the mirror:
cp /workspace/phanes-repo/tools/lane-autodispatch/autodispatch.py ~/phanes-ctl/bin/autodispatch.py
# restart is via the existing supervisor loop (lane-autodispatch); no systemd unit on the box.
```

## Proof

```bash
python3 tools/lane-autodispatch/test_can_take.py
```

## Box boot

See [`docs/ctl-boot.md`](../docs/ctl-boot.md) — `start-control-plane` after reboot; no systemd.
