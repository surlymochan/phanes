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
