"""Best-effort cgroup v2 boundaries for executor process trees.

A reservation may only be released automatically when an executor ran inside a
dedicated cgroup and that cgroup is empty after the executor returns. If the
host does not delegate writable cgroups, callers must retain the reservation.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import uuid


_active = threading.local()


def _unescape_mount_path(value):
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def _cgroup2_mount():
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.rstrip().split()
                separator = fields.index("-")
                if fields[separator + 1] == "cgroup2":
                    return Path(_unescape_mount_path(fields[4])), Path(_unescape_mount_path(fields[3]))
    except (OSError, ValueError, IndexError):
        return None
    return None


def _current_cgroup_path():
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("0::"):
                    return Path(line[3:].strip())
    except OSError:
        pass
    return None


class CgroupBoundary:
    """A private cgroup v2 directory inherited by executor descendants."""

    def __init__(self, path):
        self.path = Path(path)
        self.proven = False

    @classmethod
    def create(cls, mount=None, current=None):
        """Create a child cgroup, returning None when the host cannot provide one."""
        if os.name != "posix":
            return None
        if mount is None:
            mount = _cgroup2_mount()
        if not mount:
            return None
        mountpoint, mount_root = map(Path, mount)
        if current is None:
            current = _current_cgroup_path()
        if current is None:
            return None
        current = Path(current)
        try:
            relative = current.relative_to(mount_root)
            parent = mountpoint / relative
            path = parent / f"swrm-{os.getpid()}-{uuid.uuid4().hex}"
            path.mkdir()
            if not (path / "cgroup.procs").is_file():
                path.rmdir()
                return None
            return cls(path)
        except (OSError, ValueError):
            return None

    def attach_current_process(self):
        """Popen pre-exec hook: join the cgroup before the command can fork."""
        try:
            fd = os.open(self.path / "cgroup.procs", os.O_WRONLY)
        except OSError:
            # Keep the executor usable on hosts where the cgroup is present but
            # delegation does not permit moving a process. finish() will then
            # refuse to treat an empty cgroup as proof.
            return
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)

    def members(self):
        try:
            raw = (self.path / "cgroup.procs").read_text(encoding="ascii")
            return [int(value) for value in raw.split()]
        except (OSError, ValueError):
            return None

    def finish(self):
        """Return (safe_to_release, remaining_pids); remove empty cgroup if possible."""
        members = self.members()
        if members is None:
            return False, None
        self.proven = self.proven or bool(members)
        if members:
            return False, members
        if not self.proven:
            self._remove_empty()
            return False, []
        self._remove_empty()
        return True, []

    def _remove_empty(self):
        try:
            self.path.rmdir()
        except OSError:
            pass


@contextmanager
def track(boundary):
    """Expose a boundary to run_proc while an executor invocation is active."""
    previous = getattr(_active, "boundary", None)
    _active.boundary = boundary
    try:
        yield
    finally:
        _active.boundary = previous


def popen_kwargs():
    boundary = getattr(_active, "boundary", None)
    if boundary is None:
        return {}
    return {"preexec_fn": boundary.attach_current_process}


def observe_spawn():
    boundary = getattr(_active, "boundary", None)
    if boundary is not None:
        members = boundary.members()
        if members:
            boundary.proven = True

