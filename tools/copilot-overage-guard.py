#!/usr/bin/env python3
"""Copilot overage guard (#1602).

Reads the current month's GitHub billing usage (read-only, via `gh api`),
sums the Copilot rows and:
  * netAmount > 0            -> write PAUSE-BACKEND-copilot (exit 1)
  * grossAmount >= warn      -> print WARN only (exit 3)
  * otherwise                -> exit 0; a stale auto-written pause is removed
  * read failure             -> exit 2, nothing written

Only files whose content starts with AUTO_PREFIX are ever deleted.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

PAUSE_NAME = "PAUSE-BACKEND-copilot"
AUTO_PREFIX = "auto:copilot-overage"
EXIT_PAUSED, EXIT_WARN, EXIT_READ_FAIL = 1, 3, 2


class ReadError(Exception):
    pass


def default_runner(args):
    """Run `gh api <path>`; return stdout text or raise ReadError."""
    try:
        proc = subprocess.run(["gh", "api"] + list(args),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ReadError(f"gh api failed to run: {e}")
    if proc.returncode != 0:
        raise ReadError(f"gh api exit {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def fetch_usage(user, year, month, runner):
    path = f"/users/{user}/settings/billing/usage?year={year}&month={month}"
    try:
        data = json.loads(runner([path]))
    except ReadError:
        raise
    except (ValueError, TypeError) as e:
        raise ReadError(f"unparseable billing response: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("usageItems"), list):
        raise ReadError("billing response has no usageItems list")
    return data["usageItems"]


def summarize(items):
    total = {"quantity": 0.0, "gross": 0.0, "discount": 0.0, "net": 0.0}
    for it in items:
        if str(it.get("product", "")).lower() != "copilot":
            continue
        total["quantity"] += float(it.get("quantity") or 0)
        total["gross"] += float(it.get("grossAmount") or 0)
        total["discount"] += float(it.get("discountAmount") or 0)
        total["net"] += float(it.get("netAmount") or 0)
    return total


def decide(total, warn_gross):
    if total["net"] > 0:
        return "pause"
    if total["gross"] >= warn_gross:
        return "warn"
    return "ok"


def run_guard(user, pause_dir, warn_gross, dry_run, now, runner, year, month):
    """Return (exit_code, result_dict). Never writes on read failure."""
    try:
        items = fetch_usage(user, year, month, runner)
    except ReadError as e:
        return EXIT_READ_FAIL, {"status": "read-failed", "error": str(e)}

    total = summarize(items)
    verdict = decide(total, warn_gross)
    pause_path = os.path.join(pause_dir, PAUSE_NAME)
    action = "none"
    result = {"status": verdict, "year": year, "month": month, "user": user,
              **{k: round(v, 6) for k, v in total.items()}}

    if verdict == "pause":
        content = f"{AUTO_PREFIX} net=${total['net']:.2f} gross=${total['gross']:.2f} at={now}\n"
        action = "write-pause"
        if not dry_run:
            os.makedirs(pause_dir, exist_ok=True)
            with open(pause_path, "w") as f:
                f.write(content)
    else:
        existing = read_text(pause_path)
        if existing is not None and existing.startswith(AUTO_PREFIX):
            action = "remove-stale-auto-pause"
            if not dry_run:
                os.remove(pause_path)

    result["action"] = action
    result["dry_run"] = dry_run
    code = {"pause": EXIT_PAUSED, "warn": EXIT_WARN, "ok": 0}[verdict]
    return code, result


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def self_test():
    import tempfile

    now = "2026-11-02T00:00:00+00:00"

    def make_runner(payload):
        def runner(args):
            if isinstance(payload, Exception):
                raise payload
            return json.dumps(payload)
        return runner

    def item(product, net, gross, qty=0.0):
        return {"product": product, "sku": "Copilot AI Credits" if product == "copilot" else "x",
                "quantity": qty, "grossAmount": gross, "discountAmount": gross - net,
                "netAmount": net}

    ok_payload = {"usageItems": [item("copilot", 0.0, 2.76, 275.7), item("actions", 0.0, 1.0)]}
    over_payload = {"usageItems": [item("copilot", 0.12, 9.0, 900.0)]}
    fail = ReadError("HTTP 404 Not Found")
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as d:
        # Case 1: net 0 -> exit 0, no pause file; stale auto file is removed,
        # but a manual pause file is kept.
        code, res = run_guard("u", d, 8.0, False, now, make_runner(ok_payload), 2026, 11)
        check("net0 exit 0 (warn 8 not reached: gross 2.76)", code == 0)
        check("net0 no pause file", not os.path.exists(os.path.join(d, PAUSE_NAME)))
        with open(os.path.join(d, PAUSE_NAME), "w") as f:
            f.write(AUTO_PREFIX + " net=$0.00 gross=$1.00 at=old\n")
        code, res = run_guard("u", d, 8.0, False, now, make_runner(ok_payload), 2026, 11)
        check("net0 removes stale auto pause", not os.path.exists(os.path.join(d, PAUSE_NAME)))
        with open(os.path.join(d, PAUSE_NAME), "w") as f:
            f.write("operator hold\n")
        run_guard("u", d, 8.0, False, now, make_runner(ok_payload), 2026, 11)
        check("net0 keeps manual pause", read_text(os.path.join(d, PAUSE_NAME)) == "operator hold\n")
        os.remove(os.path.join(d, PAUSE_NAME))

    with tempfile.TemporaryDirectory() as d:
        # Case 2: net 0.12 -> pause file written with the auto content, exit 1.
        code, res = run_guard("u", d, 8.0, False, now, make_runner(over_payload), 2026, 11)
        path = os.path.join(d, PAUSE_NAME)
        check("net>0 exit 1", code == EXIT_PAUSED)
        check("net>0 writes pause file", os.path.exists(path))
        content = read_text(path) or ""
        check("net>0 content prefix", content.startswith(AUTO_PREFIX + " net=$0.12 gross=$9.00"))
        check("net>0 action", res.get("action") == "write-pause")

    with tempfile.TemporaryDirectory() as d:
        # Case 3: read failure -> exit 2, nothing written or deleted.
        with open(os.path.join(d, PAUSE_NAME), "w") as f:
            f.write(AUTO_PREFIX + " old\n")
        code, res = run_guard("u", d, 8.0, False, now, make_runner(fail), 2026, 11)
        check("read fail exit 2", code == EXIT_READ_FAIL)
        check("read fail leaves files alone",
              read_text(os.path.join(d, PAUSE_NAME)) == AUTO_PREFIX + " old\n")
        code, res = run_guard("u", d, 8.0, False, now, make_runner(ValueError("x")), 2026, 11)
        check("bad json exit 2", code == EXIT_READ_FAIL)

    with tempfile.TemporaryDirectory() as d:
        # Case 4: gross >= warn with net 0 -> exit 3, no file.
        warn_payload = {"usageItems": [item("copilot", 0.0, 9.0)]}
        code, res = run_guard("u", d, 8.0, False, now, make_runner(warn_payload), 2026, 11)
        check("warn exit 3", code == EXIT_WARN)
        check("warn writes nothing", not os.path.exists(os.path.join(d, PAUSE_NAME)))

    with tempfile.TemporaryDirectory() as d:
        # Case 5: dry-run never writes.
        code, res = run_guard("u", d, 8.0, True, now, make_runner(over_payload), 2026, 11)
        check("dry-run no write", not os.path.exists(os.path.join(d, PAUSE_NAME)))
        check("dry-run still exits 1", code == EXIT_PAUSED)

    if failures:
        print("SELF-TEST FAIL: " + ", ".join(failures))
        return 1
    print("SELF-TEST OK (5 groups)")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user", help="GitHub user whose billing usage is read")
    p.add_argument("--pause-dir", help="directory holding PAUSE-BACKEND-copilot")
    p.add_argument("--warn-gross-usd", type=float, default=8.0)
    p.add_argument("--year", type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true", help="print result as JSON")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.user or not args.pause_dir:
        p.error("--user and --pause-dir are required")

    now_dt = dt.datetime.now(dt.timezone.utc)
    year = args.year or now_dt.year
    month = args.month or now_dt.month
    code, res = run_guard(args.user, args.pause_dir, args.warn_gross_usd,
                          args.dry_run, now_dt.isoformat(timespec="seconds"),
                          default_runner, year, month)
    if args.json:
        print(json.dumps(res, sort_keys=True))
    elif res["status"] == "read-failed":
        print(f"ERROR: cannot read billing usage: {res['error']}", file=sys.stderr)
    else:
        if res["status"] == "warn":
            print(f"WARN: copilot gross=${res['gross']:.2f} >= ${args.warn_gross_usd:.2f}")
        print(f"copilot {res['status']} net=${res['net']:.2f} gross=${res['gross']:.2f} "
              f"action={res['action']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
