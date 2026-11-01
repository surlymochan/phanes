# Per-host deploy version record

A release deploys one phanes commit to the box and to every remote. Each of those machines
leaves a small, machine readable record of what is running there, so a later check can prove
that the fleet really runs one commit.

The record is a JSON object:

```json
{
  "host": "ib1",
  "sha": "9f1c0dea1b2c3d4e5f60718293a4b5c6d7e8f90",
  "ts": "2026-11-01T09:12:33+00:00",
  "extra": {"role": "remote"}
}
```

`host`, `sha` and `ts` are required; `extra` is optional and holds additional JSON values
under their own key, so it can never shadow the required fields.

`ts` must be a valid ISO 8601 timestamp. The value is parsed completely, not only matched
against a shape: an impossible date (`2026-02-30`), an impossible time (`25:00:00`) and
trailing characters (`2026-11-01T09:12:33x`) are rejected, so a record always carries a
usable timestamp.

## Path convention

- The conventional path of the record is `~/.phanes/deploy-version.json` on the machine that
  runs the deploy, and the same path per remote host. The library exposes it as
  `version_record.default_record_path()`.
- Records live **outside the repository**. A runtime path such as
  `$OPENSWARM_HOME/deploy/versions/<host>.json` is equally fine; what matters is that the
  path is not tracked by git and is not part of a checkout.
- One file per host is the default shape (the file name carries the host), so the box and the
  remotes can be compared with a single `check --paths` call. A single file holding the
  records of all hosts works too when the deploy writes it on the box.
- A record holds a host name, a commit SHA and a timestamp. **Never** write a private key,
  a password, a token or any other credential into a record, and never commit a record that
  was produced on a real host.

## Writing a record

```bash
python3 openswarm/lib/deploy/version_record.py write \
  --path ~/.phanes/deploy-version.json --host box \
  --sha "$(git rev-parse HEAD)" --ts "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
```

`--ts` is optional and defaults to the current UTC time; `--extra` takes a JSON object.
The file is written atomically (a temporary file in the same directory plus `os.replace`),
so a reader never sees a half written record. An unusable timestamp is refused with exit
code `2` and nothing is written.

The same thing from Python:

```python
from openswarm.lib.deploy import version_record

version_record.write_record("~/.phanes/deploy-version.json", "ib1", sha, ts_iso)
record = version_record.read_record("~/.phanes/deploy-version.json")
```

## Checking the hosts

```bash
python3 openswarm/lib/deploy/version_record.py check \
  --paths ~/.phanes/deploy-version.json,/tmp/ib1-deploy-version.json
```

The command prints a JSON report and exits `0` when all records run the same commit, `1` when
a host runs another commit (the report names it) and `2` when a record is missing or broken.

```json
{"hosts": ["box", "ib1"], "mismatched": ["ib2"], "ok": false, "sha": "9f1c0de..."}
```

`version_record.assert_same_sha(records)` returns the same list of hosts from Python.

## Scope

The module only writes and compares records. It does not connect over SSH and does not deploy
anything: how a record reaches a host is the caller's business.

Refs #1482 (part of #1468).
