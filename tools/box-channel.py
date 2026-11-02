#!/usr/bin/env python3
"""Box channel keeper: switch the box to the backup tunnel when the primary dies (Refs #1717).

The box reaches the control plane through one websocket tunnel; when that
tunnel service dies every box lane stops.  This tool keeps a second,
independent channel ready and moves the shim target to it when the primary
channel keeps failing:

  box-channel.py tick    --config ~/phanes-ctl/box-channel.json [--json]
  box-channel.py status  --config ~/phanes-ctl/box-channel.json

``tick`` probes both channels once, switches when the thresholds say so, writes
the shim target file and appends one brain log line per switch.  ``status``
prints the channel state the watchdog reads as JSON: the current channel, the
last success of each channel and the switch count.

The configuration file is JSON:

  {
    "primary": {"host": "127.0.0.1", "port": 8765},
    "backup":  {"host": "127.0.0.1", "port": 8766},
    "failure_threshold": 3,
    "recovery_threshold": 3,
    "state_file": "~/phanes-ctl/box-channel.json",
    "target_file": "~/phanes-ctl/box-channel.target",
    "brain_log": "/workspace/phanes/brain/log.md"
  }
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent / "openswarm" / "lib" / "transport" / "channel.py"

DEFAULT_CONFIG = os.environ.get("BOX_CHANNEL_CONFIG", "~/phanes-ctl/box-channel.json")


def load_module():
    """Load ``openswarm/lib/transport/channel.py`` by path (namespace package)."""
    spec = importlib.util.spec_from_file_location("openswarm_transport_channel", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


channel_module = load_module()


def expand(path):
    """Expand ``~`` and environment variables in a configured path."""
    return os.path.expanduser(os.path.expandvars(str(path)))


def read_config(path):
    """Read the channel configuration; a broken file is a hard error."""
    try:
        with open(expand(path), encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, ValueError) as error:
        raise ValueError("channel configuration unavailable: %s" % error)
    if not isinstance(config, dict):
        raise ValueError("channel configuration must be a JSON object")
    for name in ("primary", "backup"):
        if not isinstance(config.get(name), dict):
            raise ValueError("channel configuration needs a %s channel" % name)
    return config


def build(channel, config):
    """Build one tunnel channel from its configuration block."""
    name = config.get("name") or channel
    host = config.get("host")
    port = config.get("port")
    if not host or port is None:
        raise ValueError("channel %s needs a host and a port" % name)
    timeout = config.get("probe_timeout")
    return channel_module.TunnelChannel(name, host, port,
                                        timeout=timeout if timeout else channel_module.DEFAULT_PROBE_TIMEOUT)


def build_controller(config, brain_log=True):
    """Build the failover controller of a configuration."""
    primary = build("primary", config["primary"])
    backup = build("backup", config["backup"])
    logger = None
    log_path = config.get("brain_log")
    if brain_log and log_path:
        logger = channel_module.brain_logger(expand(log_path))
    return channel_module.ChannelFailover(
        primary, backup,
        failure_threshold=int(config.get("failure_threshold", channel_module.DEFAULT_FAILURE_THRESHOLD)),
        recovery_threshold=int(config.get("recovery_threshold", channel_module.DEFAULT_RECOVERY_THRESHOLD)),
        brain_log=logger)


def save_state(config, controller):
    """Persist the channel state and the shim target."""
    state_file = config.get("state_file")
    if not state_file:
        return None
    path = expand(state_file)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload = controller.state_dict()
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=1)
        stream.write("\n")
    os.replace(temporary, path)
    target_file = config.get("target_file")
    if target_file:
        channel_module.write_target(expand(target_file), payload)
    return path


def command_tick(args):
    """Probe the channels once, switch when needed, persist the new state."""
    config = read_config(args.config)
    controller = build_controller(config)
    state_file = config.get("state_file")
    if state_file:
        controller.restore(channel_module.load_state(expand(state_file)))
    event = controller.tick()
    path = save_state(config, controller)
    health = controller.health()
    if args.json:
        print(json.dumps({"health": health, "switch": event, "state_file": path},
                         ensure_ascii=False, indent=1))
    else:
        print("channel=%s target=%s switches=%d" % (health["current"], health["target"],
                                                    health["switch_count"]))
        if event:
            print("switch %s->%s at %s after %s" % (event["from"], event["to"],
                                                     event["at"], event["reason"]))
    return 0


def command_status(args):
    """Print the channel state the watchdog reads (always JSON)."""
    config = read_config(args.config)
    state_file = config.get("state_file")
    state = channel_module.load_state(expand(state_file)) if state_file else {}
    print(json.dumps(state, ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tick", "status"))
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="channel configuration file (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the channel state as JSON")
    args = parser.parse_args(argv)
    try:
        if args.command == "tick":
            return command_tick(args)
        return command_status(args)
    except (ValueError, OSError, channel_module.ChannelError) as error:
        parser.exit(1, "box-channel: %s\n" % error)


if __name__ == "__main__":
    raise SystemExit(main())
