# Hub and execution integrations

This directory exposes the Swrm executables used by the Phanes Hub integration:

- `hub-fleet-gateway`: dispatch and retrieve results through a configured fleet.
- `fleet-result-bundle`: package a completed remote run for local review.

The Hub integration is implemented in `volto/src/backend/`; Swrm routing and
execution live in `swrm/`. Use `swrm` for new CLI invocations. The legacy
`swrm` executable remains available as a compatibility alias.

Configure fleet hosts and local paths in the consuming deployment. Keep host
names, account names, credentials, and machine-specific paths out of this public
repository.

## Fleet deployment configuration

The controlled fleet host set, the per-host lane limits and the backend
assignment have a single source of truth:
[`docs/fleet-topology-expected.md`](../docs/fleet-topology-expected.md). The
same values are shown in the deployment example
[`docs/examples/hub-bridge.env.example`](../docs/examples/hub-bridge.env.example)
and in the SSH runbook [`docs/dual-mode-ssh.md`](../docs/dual-mode-ssh.md); the
three must stay in sync, so the topology document is not duplicated here.

The control plane runs on `box`. `box` is not a controlled fleet host: set
`OPENSWARM_AUTO_LOCAL_HOST=box` and give it an empty backend allowlist
(`"box":[]`) in `OPENSWARM_AUTO_HOST_BACKENDS`, so the local lane runtime is
neither probed nor used as a dispatch target.
