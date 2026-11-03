#!/usr/bin/env python3
"""The idle lane serve soak of #1817: the zombie children of the supervisor stay 0.

The script starts the real ``lane serve`` supervisor with an executor that polls git in
the lane worktree over and over.  Every poll leaves the orphan of #1817 behind (a
detached background ``git`` that nobody waits for), so the supervisor adopts it and has
to reap it.  The supervisor is watched through ``ps`` while it runs: the zombie children
of the supervisor must stay 0, and the supervisor must reap every orphan of every poll
round.  Without the reaping of the supervisor the zombies pile up one per round, which
is what filled the process table of the execution host (Refs #1817).

The evidence of the acceptance is the log this script writes:

    python3 openswarm/tests/lane_zombie_soak.py --minutes 30 --out soak.txt

Every sample is taken right after the supervisor reaped an orphan (the primary sample)
and every five seconds (the background snapshot), so the log shows both the number the
acceptance asks for and the worst case between two polls.
"""
import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

# The fake executor of the regression test: the same code path the soak runs.
import test_lane_zombies as fixture  # noqa: E402

CLI = ROOT / "bin" / "openswarm"


def zombie_children(pid):
    """The zombie children of one process, counted out of ``ps``."""
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,stat="], capture_output=True, text=True)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith("Z") and parts[1] == str(pid):
            found.append(int(parts[0]))
    return found


def host_zombie_count():
    """The number of zombie (Z) processes on the whole host."""
    result = subprocess.run(["ps", "-eo", "stat="], capture_output=True, text=True)
    return sum(1 for line in result.stdout.splitlines() if line.strip().startswith("Z"))


class Soak:
    """One idle lane serve, watched while it runs."""

    def __init__(self, workdir, interval):
        self.workdir = pathlib.Path(workdir)
        self.interval = interval
        self.home = self.workdir / "home"
        self.marker = self.workdir / "marker"
        self.repo = self.workdir / "repo"
        self.serve = None
        self.max_serve_zombies = 0
        self.max_background_zombies = 0
        self.max_host_zombies = 0
        self.samples = 0

    # ---------------------------------------------------------------- fixtures

    def git(self, *args):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True)

    def prepare(self):
        """Build the lane repo, the fake executor and the openswarm home."""
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.home.mkdir(parents=True)
        self.repo.mkdir(parents=True)
        self.git("init", "-q", str(self.repo))
        self.git("-C", str(self.repo), "config", "user.email", "lane@example.com")
        self.git("-C", str(self.repo), "config", "user.name", "lane")
        (self.repo / "README").write_text("lane\n", encoding="utf-8")
        self.git("-C", str(self.repo), "add", "-A")
        self.git("-C", str(self.repo), "commit", "-q", "-m", "first")
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.write_text(fixture.POST_COMMIT_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        fake_pi = self.workdir / "fake-pi"
        fake_pi.write_text(fixture.FAKE_PI, encoding="utf-8")
        fake_pi.chmod(0o755)
        (self.home / "policy.local.json").write_text(
            json.dumps({"pi": {"require_isolation": False}}), encoding="utf-8")
        lanes = {"lanes": [{"name": "ZT", "backend": "pi", "model": "MiniMax-M3.1-Flash",
                            "provider": "minimax-cn", "repo": str(self.repo),
                            "binary": str(fake_pi)}]}
        (self.home / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")

    def start(self):
        """Start the real lane serve supervisor and wait until it is idle."""
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.home)
        env["OPENSWARM_LANES_FILE"] = str(self.home / "lanes.json")
        env["OPENSWARM_ZOMBIE_MARKER"] = str(self.marker)
        env["OPENSWARM_ZOMBIE_POLL_INTERVAL"] = str(self.interval)
        # The supervisor log goes to a file: a pipe nobody reads would block the serve.
        self.log = open(self.workdir / "serve.log", "w", encoding="utf-8")
        self.serve = subprocess.Popen([sys.executable, str(CLI), "lane", "serve", "ZT"],
                                      stdout=self.log, stderr=subprocess.STDOUT,
                                      text=True, env=env, start_new_session=True)
        end = time.time() + 60
        while time.time() < end:
            if self.serve.poll() is not None:
                raise RuntimeError("the lane serve exited: " + self.serve_log())
            if self.state().get("state") == "idle" and self.state().get("child_pid"):
                return
            time.sleep(0.1)
        raise RuntimeError("the lane serve did not become idle")

    def serve_log(self):
        """The supervisor log of the soak."""
        try:
            return (self.workdir / "serve.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def stop(self):
        if self.serve is None or self.serve.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.serve.pid), signal.SIGTERM)
        except OSError:
            self.serve.terminate()
        try:
            self.serve.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.serve.kill()
        if getattr(self, "log", None) is not None:
            self.log.close()
        # The executor runs in its own session, so the supervisor's group does not hold it.
        child = self.state().get("child_pid")
        if child:
            try:
                os.kill(int(child), signal.SIGTERM)
            except (OSError, ValueError):
                pass

    # ---------------------------------------------------------------- readings

    def state(self):
        """The lane state the supervisor writes."""
        try:
            recorded = json.loads((self.home / "lanes" / "ZT" / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return recorded if isinstance(recorded, dict) else {}

    def rounds(self):
        """The number of poll rounds the fake executor has run."""
        try:
            return len(self.marker.read_text(encoding="utf-8").split())
        except OSError:
            return 0

    def reaped(self):
        """The number of dead children the supervisor has reaped."""
        return int(self.state().get("zombies_reaped") or 0)

    def sample(self, kind, elapsed):
        """One reading of the process table of the supervisor."""
        found = zombie_children(self.serve.pid)
        host = host_zombie_count()
        self.samples += 1
        if kind == "primary":
            self.max_serve_zombies = max(self.max_serve_zombies, len(found))
        else:
            self.max_background_zombies = max(self.max_background_zombies, len(found))
        self.max_host_zombies = max(self.max_host_zombies, host)
        return {"kind": kind, "elapsed": round(elapsed, 1), "rounds": self.rounds(),
                "reaped": self.reaped(), "serve_zombies": len(found), "host_zombies": host}


def run(args, out):
    soak = Soak(args.workdir, args.interval)
    soak.prepare()
    soak.start()
    out.write(f"# lane serve zombie soak of #1817 — started {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    out.write(f"# host={os.uname().nodename} kernel={os.uname().release} "
              f"cpus={os.cpu_count()} python={sys.version.split()[0]}\n")
    out.write(f"# serve pid={soak.serve.pid} lane=ZT poll_interval={args.interval}s "
              f"duration={args.minutes}min\n")
    out.flush()
    started = time.time()
    deadline = started + args.minutes * 60
    last_reaped = soak.reaped()
    last_background = started
    samples = []
    try:
        while True:
            now = time.time()
            if now >= deadline:
                break
            # The primary sample is taken right after the supervisor reaped an orphan.
            if soak.reaped() > last_reaped:
                last_reaped = soak.reaped()
                reading = soak.sample("primary", now - started)
                samples.append(reading)
                out.write("primary elapsed={elapsed}s rounds={rounds} reaped={reaped} "
                          "serve_zombies={serve_zombies} host_zombies={host_zombies}\n".format(**reading))
                out.flush()
            # The background snapshot shows the worst case between two polls.
            if now - last_background >= 5:
                last_background = now
                reading = soak.sample("background", now - started)
                samples.append(reading)
                out.write("background elapsed={elapsed}s rounds={rounds} reaped={reaped} "
                          "serve_zombies={serve_zombies} host_zombies={host_zombies}\n".format(**reading))
                out.flush()
            time.sleep(0.02)
        final = soak.sample("final", time.time() - started)
        samples.append(final)
        out.write("final elapsed={elapsed}s rounds={rounds} reaped={reaped} "
                  "serve_zombies={serve_zombies} host_zombies={host_zombies}\n".format(**final))
        rounds, reaped = soak.rounds(), soak.reaped()
        out.write(f"summary minutes={args.minutes} samples={soak.samples} rounds={rounds} "
                  f"reaped={reaped} max_serve_zombies={soak.max_serve_zombies} "
                  f"max_background_zombies={soak.max_background_zombies} "
                  f"max_host_zombies={soak.max_host_zombies} "
                  f"final_serve_zombies={final['serve_zombies']}\n")
        out.flush()
        ok = (reaped >= rounds and final["serve_zombies"] == 0 and soak.max_serve_zombies == 0)
        out.write(f"verdict {'pass' if ok else 'fail'}: the supervisor reaped every orphan "
                  f"({reaped}/{rounds}) and kept {final['serve_zombies']} zombie children\n")
        out.flush()
        return 0 if ok else 1
    finally:
        soak.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="The idle lane serve zombie soak of #1817.")
    parser.add_argument("--minutes", type=float, default=30.0, help="the duration of the soak")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="the seconds between two poll rounds of the fake executor")
    parser.add_argument("--out", default="-", help="the log file, - for the standard output")
    parser.add_argument("--workdir", default=str(ROOT.parent / ".openswarm-tmp" / ("z%d" % os.getpid())),
                        help="the scratch directory of the soak (the lane socket path has to stay short)")
    args = parser.parse_args(argv)
    if args.out == "-":
        return run(args, sys.stdout)
    with open(args.out, "w", encoding="utf-8") as stream:
        return run(args, stream)


if __name__ == "__main__":
    sys.exit(main())
