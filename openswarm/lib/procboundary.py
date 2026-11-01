"""Linux cgroup v2 process boundaries for safely tracking descendants."""
import os
import signal


class CgroupBoundary:
    """A delegated cgroup v2 child used to track a process tree."""

    def __init__(self, path):
        self.path = os.fspath(path)

    def create(self, name):
        if not name or name in ('.', '..') or '/' in name:
            raise ValueError('name must be a single cgroup component')
        path = os.path.join(self.path, name)
        os.mkdir(path)
        return CgroupBoundary(path)

    def attach(self, pid):
        with open(os.path.join(self.path, 'cgroup.procs'), 'w') as stream:
            stream.write('{}\n'.format(int(pid)))

    def members(self):
        with open(os.path.join(self.path, 'cgroup.procs')) as stream:
            return {int(line) for line in stream if line.strip()}

    def kill_all(self, sig=signal.SIGKILL):
        kill_file = os.path.join(self.path, 'cgroup.kill')
        if os.path.exists(kill_file):
            with open(kill_file, 'w') as stream:
                stream.write('1\n')
            return
        for pid in self.members():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def remove(self):
        os.rmdir(self.path)


def detect(root='/sys/fs/cgroup'):
    """Return a boundary factory for this process's writable v2 cgroup."""
    try:
        with open('/proc/self/cgroup') as stream:
            entries = stream.read().splitlines()
        relative = next(line.split('::', 1)[1].lstrip('/') for line in entries
                        if line.startswith('0::'))
        parent = os.path.join(os.fspath(root), relative)
        if not os.path.isfile(os.path.join(root, 'cgroup.controllers')):
            return None
        # Check directory write bits as well as access(): tests and containers may
        # run as root, for whom access() alone ignores a read-only delegation.
        mode = os.stat(parent).st_mode
        if not mode & 0o222 or not os.access(parent, os.W_OK | os.X_OK):
            return None
        if not os.access(os.path.join(parent, 'cgroup.procs'), os.W_OK):
            return None
        return CgroupBoundary(parent)
    except (OSError, StopIteration, IndexError, ValueError):
        return None


def release_decision(boundary, members):
    """Only release a record when its boundary was read and is empty."""
    if boundary is None or members is None:
        return 'retain'
    try:
        return 'release' if len(members) == 0 else 'retain'
    except Exception:
        return 'retain'
