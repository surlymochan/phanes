# Troubleshooting Volto

## Volto says agent CLI setup is required

Open Terminal and verify the CLI:

```bash
codex --version
```

If the command is missing, install Codex using the official installer:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Then run `codex login`, sign in with ChatGPT, fully quit Volto, and open it again.

You can use Claude instead. Verify and install Claude CLI:

```bash
claude --version
curl -fsSL https://claude.ai/install.sh | bash
claude auth login
```

If Codex is installed in a non-standard location, launch Volto with `VOLTO_CODEX_PATH` set to the
absolute Codex executable path.

For a non-standard Claude location, set `VOLTO_CLAUDE_PATH` to the absolute Claude executable path.

## The Linux AppImage exits immediately

On Ubuntu 23.10 or newer and on Debian 13, unprivileged user namespaces are restricted by AppArmor.
The Electron sandbox is built on one, so Volto exits during launch and writes a message about the
SUID sandbox or a user namespace to the terminal.

Install the AppArmor profile that ships with Volto, then reload AppArmor:

```bash
sudo install -m 0644 build/linux/openbot.apparmor /etc/apparmor.d/openbot
sudo systemctl reload apparmor
```

The same file is inside the AppImage at `resources/linux/openbot.apparmor`. The profile attaches to
the usual places to keep an AppImage; if yours is somewhere else, edit the path in the profile before
you install it.

Do not start Volto with `--no-sandbox`. It is not a supported workaround. The sandbox is the
boundary between a web renderer and the rest of your computer, and Volto gives its agents full
local access on the other side of it.

## The Linux AppImage says that no fusermount binary was found

The AppImage mounts itself with FUSE. It does not need libfuse2, but it needs the `fusermount3`
program. Install the `fuse3` package (`sudo pacman -S fuse3` on Arch Linux and Omarchy,
`sudo apt install fuse3` on Ubuntu and Debian), then start the AppImage again. With no FUSE, such
as in a container, start it with `--appimage-extract-and-run`.

## Remote desktop is missing on Linux

Remote desktop on Linux needs the x64 AppImage and an X11 session. Sunshine captures the X11 screen
and sends mouse and keyboard input through the XTest extension. Under Wayland, or with no `DISPLAY`,
remote desktop reports that it needs an X11 session. Log in with an Xorg session, or start Volto
under `xvfb-run` on a server. The arm64 AppImage does not include the runtime.

## Voice prompts fail on an older Linux

The voice runtime of the x64 AppImage needs glibc 2.32 or later, such as Ubuntu 22.04 or Debian 12.
On Ubuntu 20.04 or Debian 11, the dictation model downloads, but the runtime does not load, and
voice prompts show an error. Use a newer distribution. The arm64 AppImage needs glibc 2.17 or later.

## Volto is slow or stops because of the GPU on Linux

On Linux, Volto uses the GPU until the GPU fails on your computer. If the GPU process crashes, or a
start stops in its first 30 seconds, Volto writes `gpu-crash.json` or `gpu-start.json` in
`~/.config/OpenBot`. While one of these files is there, Volto renders on the CPU. Dialogs and
panels with a blurred background are then slower. An update with a new Chromium version tries the
GPU again. A GPU process restart after a lost GPU context, for example after suspend, does not
count. A server (`VOLTO_SERVER=1` or a hosted server) always renders on the CPU.

- To try the GPU again, for example after a driver update, remove the file. The log names it.
- To always use the GPU, start Volto with `VOLTO_DISABLE_GPU=0`.
- If Volto stops at each start, start it with `VOLTO_DISABLE_GPU=1` to always render on the CPU.

## Computer Use is unavailable

Computer Use needs the `cua-driver` binary. An installed release carries it, so the panel reporting
a missing driver means a development build, or a file that was removed. On macOS it also needs the
Screen Recording and Accessibility permissions; Windows and Linux ask for no permission, so there a
driver that answers is ready. Volto starts the driver itself; it does not bypass the macOS prompts.

If an installed release reports that the driver is missing, reinstall a complete Volto package,
then start Volto again. Keep your Volto data folder. A separate driver installation cannot
repair an installed release, because the application uses only its bundled driver.

In a development checkout, run `bun run prepare:cua-driver`, then press **Try again** in the panel.
This command writes the same pinned build the release ships.
`bun run cua-driver:doctor` reports which binary Volto would use, and `VOLTO_CUA_DRIVER_PATH`
selects a different one. The installed application ignores that variable: it runs only the driver it
was released with.

On Linux the driver reads the desktop through AT-SPI, so it needs the session bus of the desktop it
is to drive. A daemon started from a container, from `runuser`, or as root against another user's
session finds an empty accessibility tree, and `get_window_state` reports `degraded`. X11 is the
driver's fully supported Linux session. On Wayland Volto starts the driver with
`CUA_DRIVER_RS_ENABLE_WAYLAND=1`; set that variable yourself to `0` if your compositor works better
through XWayland. Window rectangles under GNOME also need the driver's own `winrects@cua` shell
extension, which its installer refreshes but does not enable for you.

If the panel reports that permissions are needed, open **System Settings → Privacy & Security** and
grant both **Screen & System Audio Recording** and **Accessibility**, then press **Check again**. A
development build asks for the grants as **Electron**, not as Volto, because the development binary
is the responsible process. For the same reason a development grant does not carry over to an
installed release, and each build must be granted once.

## Computer Use is slow

A Computer Use step has two parts: the driver does the action, and then the model reads the result
and chooses the next action. While the driver works, the activity line in the chat shows **Using an
app on this computer…**. While the model works, it shows **Deciding the next step in the app…**.
When the agent writes its own progress note, the line shows that note instead. When the line stays
the same for 5 seconds, the time since it changed shows next to the text.

A driver call usually takes a few seconds or less. To see the time of each call, start Volto from
a terminal. Each driver call that takes 5 seconds or more shows in its output as `Computer Use
driver answered`, with the tool name and the milliseconds. A call that gets no answer before the
agent closes the connection shows as `Computer Use driver did not answer`. Set
`VOLTO_LOG_LEVEL=debug` to show every call. The log does not contain what the agent typed.

Most of the time in a slow step is the model. Its time depends on the model and on the reasoning
effort in the agent settings: a high effort can use tens of seconds before each action. For
mechanical GUI tasks, select a lower effort or a faster model. Name the application and the action
in your request, for example "In Safari, open https://example.com", so the agent does not look for
the target first.

A step also gets slower as the conversation gets longer. The model reads the full conversation
before each action, and each window read stays in it. When the conversation fills the model's
context, the provider compacts it: it writes a summary, which can take minutes, and the chat
shows no action in that time. A new conversation for a new GUI task does not have this cost, and the
agent keeps its workspace. With OpenCode, Volto adds a JSON copy of each driver result
to the result text, because OpenCode shows the model only the text. The copy leaves out the element
list, because the tree in the text already names each element by its index.

## A chat is missing after an update

Do not follow the reset steps below. Your messages are stored in one SQLite file, nothing copies it
before an upgrade, and moving that folder puts the only copy out of reach.

Quit Volto and start it again first. On launch Volto gives a chat back to the agent it belongs to
when an agent and its chat lose track of each other, so a restart recovers most cases on its own.

If the chat is still missing, quit Volto and read the file directly. This reports chats that no
agent currently claims, and the number of messages waiting in each:

```sh
sqlite3 "$HOME/Library/Application Support/OpenBot/openbot.db" \
  "SELECT t.thread_id, t.agent_id, (SELECT count(*) FROM projection_thread_messages m
     WHERE m.thread_id = t.thread_id) AS messages
   FROM projection_threads t
   LEFT JOIN projection_agents a ON a.agent_id = t.agent_id
   WHERE a.agent_id IS NULL;"
```

Any row means the messages are still on disk and are recoverable. Report the output with the details
below, and keep the folder where it is until then.

## Volto will not start and names a stored agent profile

The message reads `Stored agent profile <id> has an unreadable "<field>" value`. Your data is intact:
Volto stops before it writes anything, which is what keeps the profile as it is.

Do not follow the reset steps below, and do not move the folder. Install the current version first:
Volto now repairs a stored profile field it cannot read and keeps the agent, its chat and its
workspace, while a release from before that repair refuses to start over the same profile.

If the current version still stops, quit Volto and read the profile it names. On macOS:

```sh
sqlite3 "$HOME/Library/Application Support/OpenBot/openbot.db" \
  "SELECT agent_json FROM projection_agents WHERE agent_id = '<id>';"
```

On Windows, the same file is at `%APPDATA%\OpenBot\openbot.db`.

Report the field the message names, together with the details below. The output holds your own file
paths, so review it before you publish it.

## Routines do not run while the computer sleeps

A routine runs only while the computer is awake and Volto is open.

- After a sleep, each overdue routine runs one time, when the network is back. Volto does not run
  each occurrence that it missed.
- After Volto was closed, the routine setting "If Volto was closed at the scheduled time"
  decides: skip the run, or run it one time when Volto opens.

On macOS and Windows, turn on Settings → General → **Keep this computer awake**. While the computer
is connected to power, it then does not sleep when idle. The display can still turn off. A closed
laptop lid, or a sleep that you select, still sleeps the computer, unless macOS uses an external
display (closed-display mode). On battery power, the computer sleeps as usual, unless you also turn
on **Also on battery**.

`pmset -g assertions` can show an Volto entry named `WebRTC has active PeerConnections`. It
exists only while another device is connected to this computer, so the computer can sleep after that
device disconnects. With the setting on, the entry is named `Electron`.

To see when a Mac slept, run `pmset -g log | grep -E " Sleep | Wake "`. `logs/main/main.log` (see
[Report a problem](#report-a-problem)) has a `Keeping this computer awake.` line when Volto
starts to keep it awake. For routines that run when no computer of yours is on, use a
[hosted server](hosted-servers.md) or a [self-hosted server](self-hosted-server.md).

## A company firewall or proxy blocks Volto

Sign-in shows "A firewall or proxy on this network blocked Volto from reaching `<host>`" when a
network filter, such as Fortinet, answers in place of the Volto service. This occurs when the
filter shows a block page, or when it inspects TLS with a root certificate that the computer does
not trust.

Volto trusts the root certificates in the operating system store, as a browser does. If the
company installs its inspection certificate on the computer, Volto accepts it. Agent CLIs that
Volto starts, such as Codex and Claude, check certificates by their own rules. Do not turn off
certificate checks to work around a block.

Ask the network administrator to allow these hosts over HTTPS (TCP 443) and WebSocket:

| Host | Use |
| --- | --- |
| `api.openbot.run` | Accounts, sign-in, teams, and hosted servers |
| `signal.openbot.run` | Connection setup for teams and remote servers |
| `openbot.run` | Public site and images in invitation emails |
| `*.openbot.run` | Hosted servers and sites, and `analytics.openbot.run` |
| `github.com`, `*.githubusercontent.com` | App updates |

Teams and remote servers also send WebRTC traffic to the other device or to the TURN relay that
Signal names, on the UDP and TCP ports that it names. A filter that blocks WebRTC makes these
connections slow or stops them. Conversations stay on the computer; the [privacy policy](../PRIVACY.md) lists what each
host receives.

If the filter puts `openbot.run` in a blocked category, the administrator can ask the vendor to
review it. For Fortinet, use the FortiGuard web filter lookup.

## Reset Volto

Quit Volto before moving data. To reset application state and the shared browser profile while
keeping agent workspaces, move this folder somewhere safe:

```text
~/Library/Application Support/OpenBot
```

To also reset agent workspaces, managed transfers, and downloads, move this folder as well:

```text
~/OpenBot
```

Volto creates fresh folders on the next launch. Review and back up their contents first. Do not
remove `~/.codex` or `~/.claude` unless you intentionally want to manage CLI login and history.

## Uninstall

Quit Volto. On macOS remove `OpenBot.app` from Applications; on Windows use the installer's
uninstaller; on Linux delete the AppImage, `~/.local/share/applications/openbot.desktop`,
`~/.local/share/icons/openbot.png`, and `/etc/apparmor.d/openbot` if you installed the profile. If
you also want to remove local Volto data, follow the reset steps above. Agent CLIs and their data
are independent and are not removed with Volto.

## Report a problem

Use the [bug report form](https://github.com/nightly-labs/openbot/issues/new?template=bug_report.yml)
for reproducible bugs. It asks for the platform, the Volto version (Settings → Updates on the
desktop, Settings → About on the phone), the operating system and hardware, the provider and its CLI
version, the steps, and the expected and actual results. When a provider row shows an error, select
**Copy diagnostics** in its menu and paste the result into the form. On the phone, Settings →
Support → **Save log file** makes a log that you can attach. Never publish tokens, `~/.codex`,
`~/.claude`, conversations, private files, Electron user data, or full unreviewed diagnostics.

When the desktop app closes by itself, look in the `logs` folder of its user data
(`%APPDATA%\OpenBot\logs` on Windows, `~/Library/Application Support/OpenBot/logs` on macOS,
`~/.config/OpenBot/logs` on Linux):

- `main/main.log` has a `Quitting: <reason>.` line when Volto quit by itself or was closed.
- `trace.ndjson` has an `exit` line with the same reason, and a `crash` line for each renderer or
  helper process that stopped.

A dump in the `Crashpad` folder next to `logs` means a process crashed. Tell us that a dump exists,
but do not attach it publicly: it can hold secrets. When neither file has a reason and there is no
dump, something outside Volto stopped it.
