# Antigravity macOS runtime

`bin/agy-via-local-proxy` launches the existing Antigravity CLI with the configured local proxy. Its OAuth credential is stored as base64-encoded JSON in the login Keychain, under account `openswarm` and service `openbot-agy-nick-oauth`. The wrapper restores it to a private home on a RAM disk and recreates that home after a restart. Configure the Keychain item through an authenticated GUI session; never print its contents.

The wrapper leaves the existing user's Antigravity profile untouched. It saves the credential back only when the refresh token changes. An unavailable or locked login Keychain, or a failed RAM disk mount, stops the launch rather than placing credentials on disk. A full machine restart still requires a logged-in user session and an unlocked login Keychain. The wrapper must run as the same logged-in user, with `OPENSWARM_HOME` set by the executor service.

## Keyring mode and the "keychain cannot be found" dialog

The RAM-disk home has no `~/Library/Keychains`, so macOS cannot find a default keychain there. When agy uses its OS keyring (service `antigravity`) from a GUI (Aqua) session, Security shows a "keychain cannot be found" dialog for every token save, and agy waits 5 seconds before it falls back to its token file. A lane that runs as a launchd agent therefore leaves a new dialog on screen for each run.

`OPENSWARM_AGY_KEYRING` controls this:

- `file` (default): the wrapper marks the child session as headless (`SSH_CONNECTION` and `SSH_CLIENT`, only when they are unset). agy then logs `Using file-based token storage because SSH session detected` and never calls `/usr/bin/security` with the isolated home. The wrapper still syncs a rotated token back to its own Keychain item with the real home.
- `os`: keep agy's default keyring behaviour. Use this only when the child home can reach a login keychain.

A full RAM disk also makes agy fall back to the keyring, because its token file write fails. When free space drops below `OPENSWARM_AGY_MIN_FREE_MB` (default 24), the wrapper moves conversations older than `OPENSWARM_AGY_ARCHIVE_AFTER_H` hours (default 6) to `$OPENSWARM_HOME/agy-conversation-archive`. It moves them and never deletes them.

The wrapper never resets, creates, or reorders keychains, and it never changes the default keychain.

