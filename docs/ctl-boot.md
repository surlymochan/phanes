# Box control-plane boot (no systemd)

This box has no systemd user units and no cron for phanes ctl. After reboot, start the loops with:

```bash
~/phanes-ctl/bin/start-control-plane
```

That script starts (if not already running):

| Loop | Script | Role |
|------|--------|------|
| ib-refresh | `ctl-loop` → `ib-refresh` | renew instabox SSH certs (~10 min) |
| lane-autodispatch | `lane-autodispatch` | SSH fleet dispatch + backlog refill |
| lane-merge | `lane-merge` → `merge-loop.sh` | merge Linux-green `fleet/*` PRs |
| bus-worker | `bus-worker-loop` | GitHub issue bus mode 2 |

Manual one-shots (same as comments in each script):

```bash
setsid nohup ~/phanes-ctl/bin/lane-autodispatch >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/bus-worker-loop >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 &
setsid nohup ~/phanes-ctl/bin/ctl-loop >/dev/null 2>&1 &
```

Status: `~/phanes-ctl/bin/lane-autodispatch-status`

Do not invent systemd units here; a documented start script is enough.
