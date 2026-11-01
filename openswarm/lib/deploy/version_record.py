"""Per-host deploy version record: machine readable release evidence (Refs #1482).

One phanes commit is deployed to the box and to every remote.  Each of those
machines leaves a small record of what is running there, so a later check can
tell whether the fleet really runs one commit::

    {"host": "ib1", "sha": "9f1c0de", "ts": "2026-11-01T09:12:33+00:00"}

``write_record`` writes such a record atomically (temp file in the same
directory plus ``os.replace``, so a reader never sees a half written file),
``read_record`` reads it back and validates it, and ``assert_same_sha``
compares the records of several hosts and returns the hosts that do not run
the same commit.

The module is pure standard library and knows nothing about SSH: the caller
decides how a record reaches a host.  That keeps credentials out of the
repository -- a record holds a host name, a commit SHA and a timestamp, never
a key.  Records belong outside the repository; the conventional path is
``~/.phanes/deploy-version.json`` (see ``openswarm/docs/deploy-version-record.md``).

Refs #1482 (part of #1468).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: The fields every record carries.
REQUIRED_FIELDS = ("host", "sha", "ts")

#: The conventional place for the record of the machine that runs the deploy.
DEFAULT_PATH = "~/.phanes/deploy-version.json"

#: Exit code of the CLI when the checked records disagree.
EXIT_MISMATCH = 1

#: Exit code of the CLI when the command itself failed (bad argument, no file).
EXIT_ERROR = 2

_ISO_DATE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[T ](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?)?$"
)


class RecordError(ValueError):
    """A version record is missing, unreadable or malformed."""


def default_record_path() -> str:
    """Return the conventional record path with ``~`` expanded."""
    return os.path.expanduser(DEFAULT_PATH)


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, where: str) -> str:
    """Return ``value`` when it is a non-empty string, raise ValueError otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise RecordError("{} must be a non-empty string, got {!r}".format(where, value))
    return value.strip()


def _parse_offset(text: Optional[str], where: str = "ts_iso") -> Optional[int]:
    """Return the UTC offset of an ISO 8601 timezone suffix, in minutes.

    ``Z`` and a missing suffix mean "no offset given"; ``+02:00`` and
    ``-0530`` are read as minutes east of UTC.  An offset that is out of
    range is rejected.
    """
    if text is None:
        return None
    if text == "Z":
        return 0
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:])
    if hours > 23 or minutes > 59:
        raise RecordError("{} has an invalid UTC offset: {!r}".format(where, text))
    return sign * (hours * 60 + minutes)


def _check_ts(ts_iso: Any, where: str = "ts_iso") -> str:
    """Return the timestamp when it is a valid ISO 8601 timestamp.

    The value is parsed completely instead of only matching a shape: an
    impossible date (``2026-02-30``), an impossible time (``25:00:00``) and
    trailing characters after the timestamp (``2026-11-01T09:12:33x``) are
    all rejected, so a record never carries an unusable timestamp.
    """
    text = _require_text(ts_iso, where)
    match = _ISO_DATE.match(text)
    if not match:
        raise RecordError("{} must be an ISO 8601 timestamp, got {!r}".format(where, ts_iso))
    parts = match.groupdict()
    offset = _parse_offset(parts["tz"], where)
    tzinfo = None
    if offset is not None:
        tzinfo = timezone(timedelta(minutes=offset))
    fraction = parts["fraction"] or ""
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    try:
        datetime(int(parts["year"]), int(parts["month"]), int(parts["day"]),
                 int(parts["hour"] or 0), int(parts["minute"] or 0),
                 int(parts["second"] or 0), microsecond, tzinfo=tzinfo)
    except ValueError as exc:
        raise RecordError("{} must be a valid ISO 8601 timestamp: {}".format(where, exc))
    return text


def _check_extra(extra: Any) -> Optional[Dict[str, Any]]:
    """Return the extra fields when they are a dict of JSON values, else raise."""
    if extra is None:
        return None
    if not isinstance(extra, dict):
        raise RecordError("extra must be a dict or None, got {!r}".format(extra))
    try:
        json.dumps(extra)
    except (TypeError, ValueError) as exc:
        raise RecordError("extra must be JSON serialisable: {}".format(exc))
    return dict(extra)


def build_record(host: str, sha: str, ts_iso: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the record for one host without writing it anywhere."""
    record: Dict[str, Any] = {
        "host": _require_text(host, "host"),
        "sha": _require_text(sha, "sha"),
        "ts": _check_ts(ts_iso),
    }
    checked_extra = _check_extra(extra)
    if checked_extra is not None:
        record["extra"] = checked_extra
    return record


def write_record(path: str, host: str, sha: str, ts_iso: str,
                 extra: Optional[Dict[str, Any]] = None) -> None:
    """Write the version record of one host to ``path``, atomically.

    Args:
        path: where the record is written; missing parent directories are
            created.  Keep the path outside the repository.
        host: the name of the machine the record belongs to.
        sha: the commit that is running there.
        ts_iso: when the deploy happened, as an ISO 8601 timestamp.
        extra: optional additional fields (a dict of JSON values).  They are
            kept under the ``extra`` key so they cannot shadow the fields
            above.

    Raises:
        RecordError: an argument is not usable, or the record cannot be
            written.
    """
    record = build_record(host, sha, ts_iso, extra)
    if not isinstance(path, str) or not path.strip():
        raise RecordError("path must be a non-empty string, got {!r}".format(path))
    target = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(target) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise RecordError("cannot create {}: {}".format(parent, exc))
    payload = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    handle = None
    temp_path = None
    try:
        file_descriptor, temp_path = tempfile.mkstemp(prefix=".deploy-version-", suffix=".tmp",
                                                      dir=parent)
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_path, target)
        temp_path = None
    except OSError as exc:
        raise RecordError("cannot write {}: {}".format(target, exc))
    finally:
        if handle is not None:
            handle.close()
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)


def read_record(path: str) -> Dict[str, Any]:
    """Read the version record at ``path`` and return it.

    Returns:
        The record as a dict with the fields ``host``, ``sha`` and ``ts`` and,
        when the record carries them, ``extra``.

    Raises:
        RecordError: the file is missing, is not JSON, is not a JSON object,
            misses one of the required fields or carries a timestamp that is
            not a valid ISO 8601 timestamp.
    """
    if not isinstance(path, str) or not path.strip():
        raise RecordError("path must be a non-empty string, got {!r}".format(path))
    target = os.path.expanduser(path)
    try:
        with open(target, "r", encoding="utf-8") as stream:
            raw = stream.read()
    except OSError as exc:
        raise RecordError("cannot read {}: {}".format(target, exc))
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise RecordError("{} is not valid JSON: {}".format(target, exc))
    if not isinstance(record, dict):
        raise RecordError("{} must hold a JSON object, got {}".format(target, type(record).__name__))
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    if missing:
        raise RecordError("{} misses the field(s) {}".format(target, ", ".join(missing)))
    for field in REQUIRED_FIELDS:
        _require_text(record[field], "{} field".format(field))
    _check_ts(record["ts"], "ts field")
    if "extra" in record and not isinstance(record["extra"], dict):
        raise RecordError("{} field extra must be a dict".format(target))
    return record


def assert_same_sha(records: Sequence[Dict[str, Any]]) -> List[str]:
    """Return the hosts whose records do not run the same commit.

    The first record sets the reference commit; every other record is compared
    against it.  A record without a usable ``sha`` cannot prove anything, so
    its host is reported as inconsistent as well.

    Args:
        records: the records to compare, in the order they were collected.

    Returns:
        The hosts that do not run the reference commit, without duplicates and
        in the order they appear.

    Raises:
        RecordError: a record is not a dict or does not name a host.
    """
    if isinstance(records, dict) or not isinstance(records, (list, tuple)):
        raise RecordError("records must be a list of dicts, got {!r}".format(records))
    reference: Optional[str] = None
    mismatched: List[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RecordError("record #{} must be a dict, got {!r}".format(index, record))
        host = record.get("host")
        if not isinstance(host, str) or not host.strip():
            raise RecordError("record #{} does not name a host".format(index))
        sha = record.get("sha")
        if not isinstance(sha, str) or not sha.strip():
            mismatched.append(host.strip())
            continue
        if reference is None:
            reference = sha.strip()
            continue
        if sha.strip() != reference:
            mismatched.append(host.strip())
    return list(dict.fromkeys(mismatched))


def load_records(paths: Iterable[str]) -> List[Dict[str, Any]]:
    """Read the records of several paths, in the order the paths are given."""
    records = []
    for path in paths:
        records.append(read_record(path))
    return records


def _parse_extra(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the ``--extra`` argument: a JSON object or nothing."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RecordError("--extra must be a JSON object: {}".format(exc))
    if not isinstance(parsed, dict):
        raise RecordError("--extra must be a JSON object, got {}".format(type(parsed).__name__))
    return parsed


def _split_paths(values: Optional[Sequence[str]]) -> List[str]:
    """Split repeated and comma separated ``--paths`` values into one list."""
    paths: List[str] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                paths.append(part)
    return paths


def _build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the CLI."""
    parser = argparse.ArgumentParser(
        prog="version_record.py",
        description="Write and compare per-host deploy version records.")
    subparsers = parser.add_subparsers(dest="command")

    write = subparsers.add_parser("write", help="write the version record of one host")
    write.add_argument("--path", required=True, help="where the record is written")
    write.add_argument("--host", required=True, help="name of the deployed machine")
    write.add_argument("--sha", required=True, help="commit that is running on the machine")
    write.add_argument("--ts", default=None, help="ISO 8601 timestamp (default: now, UTC)")
    write.add_argument("--extra", default=None, help="extra fields as a JSON object")

    check = subparsers.add_parser("check", help="compare the records of several hosts")
    check.add_argument("--paths", action="append", default=None,
                       help="record files, comma separated or repeated")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI; return the exit code.

    Returns:
        ``0`` on success, ``1`` when the checked records disagree and ``2``
        when the command itself failed.
    """
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        parser.print_usage(sys.stderr)
        return EXIT_ERROR
    try:
        if args.command == "write":
            write_record(args.path, args.host, args.sha,
                         args.ts or now_iso(), _parse_extra(args.extra))
            record = read_record(args.path)
            print(json.dumps(record, sort_keys=True, ensure_ascii=False))
            return 0
        paths = _split_paths(args.paths)
        if not paths:
            raise RecordError("check needs at least one path in --paths")
        records = load_records(paths)
        mismatched = assert_same_sha(records)
        report = {
            "ok": not mismatched,
            "sha": next((record.get("sha") for record in records if record.get("sha")), None),
            "hosts": [record.get("host") for record in records],
            "mismatched": mismatched,
        }
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
        if mismatched:
            print("hosts on a different commit: {}".format(", ".join(mismatched)), file=sys.stderr)
            return EXIT_MISMATCH
        return 0
    except RecordError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
