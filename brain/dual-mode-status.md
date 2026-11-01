# Dual-mode status (2026-11-01 11:43 +08 Asia/Singapore)

Project: https://github.com/users/surlymochan/projects/6  
Epic: https://github.com/surlymochan/phanes/issues/223

## Mode A — Hub/SSH (Mac-independent from box)
- Status: **OPERATIONAL** (11 lanes configured; health-loop guards hosts)
- SSH: `id_ed25519_phanes_ctl` + ControlMaster; hosts ib1–5 / gocc / gorelay / govpn / govpn2
- Dispatch: `~/phanes-ctl/bin/lane-autodispatch` reading `lane-ready` issues
- Boot: `~/phanes-ctl/bin/start-control-plane` (see [`docs/ctl-boot.md`](../docs/ctl-boot.md))
- Status paths: `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` (see `tools/check-lane-status-paths.sh`)
- Proof (earlier): run `20261101-031136-7a2856` → gorelay/agy

## Mode B — GitHub issue bus
- Status: **RESIDENT, zero-manual PASS**
- Bus: https://github.com/surlymochan/phanes-bus
- Proof: bus issue #9 → claim `surlymochan-phanes-box` → PR https://github.com/surlymochan/phanes/pull/354
- Earlier: issue #7 → PR #297 (merged)
- Resident: `~/phanes-ctl/bin/bus-worker-loop` (included in `start-control-plane`)
- Credential: box `gh` only; no token copied to fleet hosts

## Stability guards (2026-11-01)
- `health-loop` probes disk/mem/ssh jitter on box+ib*+govpn*+gocc+gorelay every ~60s
- Hard degrade → `PAUSE-HOST-*` / `PAUSE-<LANE>` / `PAUSE-BACKEND-*` (auto: bodies); autodispatch skips them; other backends keep running
- Quota/429 on harvest also sets `PAUSE-BACKEND-*`; health-loop clears after hold if no fresh signal
- 37-minute tick silence: structural fix (wall-kill process trees, parallel probes, early heartbeat) — soak in progress; see `brain/lessons-37min-silence.md` if present
- ib-refresh: reuse cert when TTL ≥25m (no force renew every tick); wall cap 420s

## Remaining gaps (Epic #223)
1. Box reboot still needs manual `start-control-plane` (documented; no systemd on box).
2. Hub App UI bridge may still default to Mac paths for interactive Hub — fleet SSH path from box is fine (#224 docs Done).
3. gocc Data volume often ≥96% used — health soft-alerts; free space recovered by clearing retired codex/copilot caches (~4GiB on 2026-11-01). Keep watching.
4. GH token rotation (user action; not automated).
5. Soak: confirm ≥30–60 min without autodispatch heartbeat gaps after stallfix.
6. #226 lane status path check script now in repo; mark Done when merged.

## Milestone checklist
- [x] M1 SSH path from box (smoke + resident dispatch)
- [x] M2 Issue bus e2e + resident worker
- [x] M3 Fleet 10+ lanes (11 configured; CX5 up)
- [x] M4 RSI routing record-only gate (#228)
- [x] M5 org-health demo contract tests (#231) — ongoing hardening via high-bar queue
- [ ] Stability soak + reboot story closed enough to mark Epic Done
