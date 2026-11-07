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
