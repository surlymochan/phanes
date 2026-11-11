"""Shared connect-based readiness checks for broker integration tests."""
import os
import select
import socket
import tempfile
import time


def short_tmp_root():
    """Keep private workspaces short even when TMPDIR points into a checkout."""
    if os.path.isdir('/tmp') and os.access('/tmp', os.W_OK | os.X_OK):
        return '/tmp'
    return tempfile.gettempdir()


def _wait_listening(family, address, timeout, proc, errors):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            break
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(min(1, max(0.001, deadline - time.monotonic())))
            try:
                probe.connect(address)
                return
            except OSError as exc:
                last = exc
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    status = proc.poll() if proc is not None else None
    detail = ''
    if errors is not None:
        try:
            with open(errors, 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 800))
                detail = stream.read().decode(errors='replace')
        except OSError:
            pass
    elif proc is not None and status is not None and proc.stderr is not None:
        # Never block on a pipe still held open by a descendant of the broker.
        if select.select([proc.stderr], [], [], 0)[0]:
            detail = os.read(proc.stderr.fileno(), 8192).decode(errors='replace')[-800:]
    raise AssertionError('the broker did not accept connections on %s (exit=%s, last=%r): %s'
                         % (address, status, last, detail))


def wait_for_unix_socket(path, timeout=45, proc=None, errors=None):
    """Wait for an accepting Unix listener, failing early if proc exits."""
    path = str(path)
    assert len(os.fsencode(path)) < 100, 'socket path too long: %s' % path
    _wait_listening(socket.AF_UNIX, path, timeout, proc, errors)


def wait_for_tcp(host, port, timeout=45, proc=None, errors=None):
    """Wait for an accepting IPv4 TCP listener, with broker diagnostics."""
    _wait_listening(socket.AF_INET, (host, port), timeout, proc, errors)
