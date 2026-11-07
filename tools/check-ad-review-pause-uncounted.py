#!/usr/bin/env python3
"""Offline contract: an AD review failure during a box VM pause is not counted (Refs #1607).

Context (AD hotfix on the control plane, 11-02 08:3x):
    a review run of a *local* lane that ends with `exec_error` while the box VM was
    paused (`box-pauses.jsonl`: `gap_s >= 120`, `from` inside `[dispatch - 5s, now]`)
    is reported as

        box VM paused <gap>s (<from>→<to>) during run → not counted

    and must NOT raise the per-PR failure counter kept in `reviews.json`
    (the 07:31→07:49 pause broke the reviews of PR #1563/#1572/#1577 and #1572 was
    counted twice).

This check is offline and only locks that fix down (the AD itself gains no feature):

  * the hotfixed AD (`--ad`, default `~/phanes-ctl/bin/autodispatch.py`) is imported
    with `importlib`;
  * `box_pause_during(cfg, st, path)` is driven as a table:
        pause inside the window        -> that record is returned
        from = dispatch - 5s           -> returned (window endpoint)
        gap_s = 120                    -> returned (threshold)
        from = dispatch - 6s           -> None (outside the window)
        gap_s = 119                    -> None (below the threshold)
        remote lane (host not local)   -> None
        malformed JSONL row            -> skipped
  * the AD's `finish_review` no-verdict branch is called with fake `gh`/`ssh` stubs:
        pause hit  -> `reviews.json` keeps the PR fail count and the log carries the
                      pause marker, the paused interval and "not counted"
        pause miss -> `reviews.json` fails + 1

Isolation: `HOME` and `OPENSWARM_CTL_STATE_DIR` point at a temporary directory
*before* the AD is imported, so every `expanduser("~")` path lands inside it (the AD
binds `D = f"{HOME}/phanes-ctl/autodispatch"` at import time,
`tools/lane-autodispatch/autodispatch.py:11`). The AD's own reviews.json /
box-pauses.jsonl paths are asserted to be inside that temporary tree before anything
is called, and its log targets are redirected into it as well.

The hotfix only exists in the box runtime copy, so a host without it reports SKIP
instead of pretending the contract holds; `--ad PATH` always uses the given file.
"""

import argparse
import importlib.util
import inspect
import json
import os
import tempfile
import time
from pathlib import Path


REVIEW_PR = 1607
REVIEW_SHA = "b0e6f5" + "0" * 34
LANE = "CX2"
BACKEND = "pi"
MIN_GAP_S = 120          # AD hotfix threshold: gap_s >= 120
WINDOW_S = 5             # window: from >= dispatch - 5s
DISPATCH_AGO_S = 600     # the review run was dispatched 10 minutes ago
INITIAL_FAILS = 4
PAUSE_MARKERS = ("box vm paused", "vm_paused", "vm pause")
# The hotfix lives in the box runtime copy (tools/lane-autodispatch/README.md:3).
AD_CANDIDATES = ("~/phanes-ctl/bin/autodispatch.py", "~/phanes-ctl/bin/lane-autodispatch",
                 "~/phanes-ctl/autodispatch.py",
                 "/workspace/phanes-repo/tools/lane-autodispatch/autodispatch.py")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def wall(stamp):
    """The AD's wall clock format: `time.strftime("%F %T")`.

    The dispatch time is stored that way (`tools/lane-autodispatch/autodispatch.py:837`,
    `swrm/lib/dispatch/engine.py:1814`) and the AD-parity dispatcher reads it back
    as the run start when the run info carries no timestamps
    (`swrm/lib/dispatch/engine.py:1244`).
    """
    return time.strftime("%F %T", time.localtime(stamp))


def inside(path, root):
    """True when `path` resolves inside `root`."""
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def path_constants(ad):
    """Import-time path constants of the AD (upper case names, no whitespace)."""
    found = {}
    for name, value in vars(ad).items():
        if not name.isupper() or not isinstance(value, str):
            continue
        if " " in value or "\n" in value or "/" not in value:
            continue
        found[name] = value
    return found


def ad_files(ad, suffix):
    """AD data files ending with `suffix`, taken from the AD's own constants."""
    return sorted({value for value in path_constants(ad).values() if value.endswith(suffix)})


def local_hosts(ad):
    """The AD's own list of local hosts (the hotfix only excuses local lanes)."""
    for name in ("LOCAL_HOSTS", "LOCAL_HOST", "local_hosts", "LOCAL_HOSTS_SET", "LOCAL"):
        value = getattr(ad, name, None)
        if isinstance(value, (set, frozenset, list, tuple)) and value:
            hosts = {str(host) for host in value}
            if all("/" not in host for host in hosts):
                return hosts, name
        if isinstance(value, str) and value and "/" not in value:
            return {value}, name
    constants = sorted(name for name in vars(ad) if name.isupper())
    raise AssertionError("AD exposes no local host list (checked LOCAL_HOSTS/LOCAL_HOST/"
                         f"local_hosts/LOCAL); upper-case constants: {constants}")


def load_ad(path):
    spec = importlib.util.spec_from_file_location("ad_review_pause_contract", path)
    check(spec is not None and spec.loader is not None, f"cannot import AD module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pause_row(start, gap_s):
    """One production `box-pauses.jsonl` row.

    The real log stores local wall clock strings plus the explicit gap
    (`swrm/tests/test_dispatch_pause_window.py:29-32`:
    `{"from": "2026-11-02 07:31:44", "to": "2026-11-02 07:49:46", "gap_s": 1082}`),
    so `to - from` equals `gap_s` here as well.
    """
    return {"from": wall(start), "to": wall(start + gap_s), "gap_s": gap_s}


def lane_state(host, dispatch, run="review-run-1"):
    """The AD lane record of a dispatched review run.

    `at` is the AD's dispatch wall clock (`tools/lane-autodispatch/autodispatch.py:837`,
    `swrm/lib/dispatch/engine.py:1814`); `review_pr`/`review_sha` identify the
    reviewed PR (`swrm/lib/dispatch/engine.py:1814`,
    `swrm/tests/test_dispatch_engine.py:497`). `started_at`/`dispatched_at` are
    epoch aliases of the same instant so the dispatch time is found under either name.
    """
    return {
        "issue": f"PR{REVIEW_PR}",
        "run": run,
        "host": host,
        "base": REVIEW_SHA,
        "at": wall(dispatch),
        "started_at": float(dispatch),
        "dispatched_at": float(dispatch),
        "verify": "",
        "review_pr": REVIEW_PR,
        "review_sha": REVIEW_SHA,
    }


def as_record(result):
    """The pause record returned by the AD (a dict, or a tuple holding one)."""
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, dict):
                return item
    raise AssertionError(f"AD returned no pause record: {result!r}")


def call_values(cfg, st, info, state):
    """Fixture values for the AD's `finish_review` parameters, by parameter name."""
    return {
        "lane": LANE, "lane_name": LANE,
        "cfg": cfg, "lc": cfg, "config": cfg, "lane_cfg": cfg,
        "st": st, "item": st, "lane_state": st,
        "info": info, "result": info, "run_info": info,
        "state": state, "states": state,
        "run": st["run"],
        "pr": REVIEW_PR, "pr_number": REVIEW_PR, "review_pr": REVIEW_PR,
        "sha": REVIEW_SHA, "review_sha": REVIEW_SHA,
    }


def call_finish_review(ad, values):
    """Call the AD's `finish_review` with the fixture, following its own signature.

    Every parameter without a default has to be matched by name; an unknown required
    parameter is a hard error (the signature is printed) instead of handing `None` to
    the AD.
    """
    signature = inspect.signature(ad.finish_review)
    args, kwargs = [], {}
    for name, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        key = name.lower()
        if key in values:
            value = values[key]
        elif param.default is not inspect.Parameter.empty:
            continue
        else:
            raise AssertionError(f"cannot safely populate finish_review parameter {name!r}; "
                                 f"signature is {signature}; known names: {sorted(values)}")
        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[name] = value
    return ad.finish_review(*args, **kwargs)


def log_text(captured, root):
    """Everything the AD logged: the captured log() calls plus the log files."""
    parts = list(captured)
    for path in sorted(root.rglob("*.log")):
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ad", default="~/phanes-ctl/bin/autodispatch.py",
                        help="path to the hotfixed autodispatch.py")
    args = parser.parse_args()
    ad_path = Path(os.path.expanduser(args.ad))
    if not ad_path.is_file():
        for candidate in AD_CANDIDATES:
            fallback = Path(os.path.expanduser(candidate))
            if fallback.is_file():
                ad_path = fallback
                break
    if not ad_path.is_file():
        # No AD runtime copy on this host (the hotfix only exists there): the
        # contract cannot be verified here, so say so loudly instead of pretending.
        print("AD review pause uncounted contract: SKIP (no AD runtime copy on this host, tried: "
              f"{', '.join(AD_CANDIDATES)}; run with --ad PATH to verify)")
        return

    saved = {name: os.environ.get(name) for name in ("HOME", "OPENSWARM_CTL_STATE_DIR", "PATH")}
    with tempfile.TemporaryDirectory(prefix="ad-pause-contract-") as temp:
        root = Path(temp)
        home = root / "home"
        ad_dir = home / "phanes-ctl" / "autodispatch"
        ad_dir.mkdir(parents=True)
        # The AD binds D = f"{HOME}/phanes-ctl/autodispatch" at import time
        # (tools/lane-autodispatch/autodispatch.py:11): redirect HOME *before* the
        # import so every expanduser("~") path lands inside the temporary tree.
        os.environ["HOME"] = str(home)
        os.environ["OPENSWARM_CTL_STATE_DIR"] = str(root / "ctl-state")

        ad = load_ad(ad_path)
        print(f"AD under test: {ad_path}")
        check(callable(getattr(ad, "box_pause_during", None)),
              "AD has no box_pause_during(cfg, st, path)")
        check(callable(getattr(ad, "finish_review", None)), "AD has no finish_review")
        pause_signature = inspect.signature(ad.box_pause_during)
        positional = [p for p in pause_signature.parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        check(len(positional) >= 3, f"unexpected box_pause_during signature: {pause_signature}")

        # The AD's own data files must live inside the temporary tree.
        ad_dir_real = Path(getattr(ad, "D", ad_dir)).expanduser()
        reviews_candidates = ad_files(ad, "reviews.json") or [str(ad_dir_real / "reviews.json")]
        pause_candidates = ad_files(ad, "pauses.jsonl") or [str(ad_dir_real / "box-pauses.jsonl")]
        for candidate in reviews_candidates + pause_candidates:
            check(inside(candidate, root), f"isolation: AD data file {candidate} is outside the "
                                           f"temporary tree {root}")
        reviews_path = Path(reviews_candidates[0]).expanduser()
        pauses_path = Path(pause_candidates[0]).expanduser()
        reviews_path.parent.mkdir(parents=True, exist_ok=True)
        pauses_path.parent.mkdir(parents=True, exist_ok=True)

        # Log targets outside the temporary tree are redirected into it; the log()
        # calls are captured so nothing is written outside the sandbox.
        for name, value in list(path_constants(ad).items()):
            if value.endswith((".log", ".md")) and not inside(value, root):
                setattr(ad, name, str(root / Path(value).name))
        captured = []
        if callable(getattr(ad, "log", None)):
            ad.log = lambda message, *rest, **options: captured.append(str(message))

        hosts, host_source = local_hosts(ad)
        local_host = sorted(hosts)[0]
        remote_host = next((name for name in ("remote-host", "offsite-host", "not-a-local-host")
                            if name not in hosts), None)
        check(remote_host is not None, f"no host name outside the AD local host list "
                                       f"{sorted(hosts)} (from {host_source})")

        now = int(time.time())
        dispatch = now - DISPATCH_AGO_S
        cases = [
            ("pause inside the window (dispatch - 3s)", pause_row(dispatch - 3, 150), local_host, True),
            ("pause inside the window (dispatch - 4s)", pause_row(dispatch - 4, 150), local_host, True),
            (f"window endpoint from = dispatch - {WINDOW_S}s",
             pause_row(dispatch - WINDOW_S, 150), local_host, True),
            (f"gap_s = {MIN_GAP_S} (threshold)", pause_row(dispatch - 3, MIN_GAP_S), local_host, True),
            (f"pause from = dispatch - {WINDOW_S + 1}s (outside the window)",
             pause_row(dispatch - WINDOW_S - 1, 150), local_host, False),
            (f"gap_s = {MIN_GAP_S - 1} (below the threshold)",
             pause_row(dispatch - 3, MIN_GAP_S - 1), local_host, False),
            ("remote lane (host not local)", pause_row(dispatch - 3, 150), remote_host, False),
        ]
        for label, row, host, expected in cases:
            pauses_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            cfg = {"host": host, "backend": BACKEND}
            st = lane_state(host, dispatch)
            result = ad.box_pause_during(cfg, st, str(pauses_path))
            if not expected:
                check(result is None, f"{label}: expected None, got {result!r}")
                continue
            check(result is not None, f"{label}: expected the pause record, got None")
            record = as_record(result)
            check(record.get("from") == row["from"],
                  f"{label}: wrong pause returned {record.get('from')!r} != {row['from']!r}")
            check(record.get("to") == row["to"],
                  f"{label}: wrong pause returned {record.get('to')!r} != {row['to']!r}")
            check(record.get("gap_s") == row["gap_s"],
                  f"{label}: wrong pause returned {record.get('gap_s')!r} != {row['gap_s']!r}")

        # A malformed row is skipped, the valid row behind it still counts.
        good = pause_row(dispatch - 3, 150)
        pauses_path.write_text("not json\n" + json.dumps(good) + "\n", encoding="utf-8")
        result = ad.box_pause_during({"host": local_host, "backend": BACKEND},
                                     lane_state(local_host, dispatch), str(pauses_path))
        check(result is not None, "malformed JSONL row was not skipped")
        record = as_record(result)
        check(record.get("from") == good["from"],
              f"malformed JSONL row: wrong pause returned {record.get('from')!r} != {good['from']!r}")

        # ---- finish_review: the no-verdict branch ---------------------------------
        fake_bin = root / "bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        gh_stub = fake_bin / "gh"
        gh_stub.write_text("#!/bin/sh\n"
                           "case \"$*\" in\n"
                           f"  *\"pr view\"*) echo '{{\"headRefOid\": \"{REVIEW_SHA}\", \"state\": \"OPEN\", "
                           f"\"headRefName\": \"fleet/{LANE}-issue1607\", \"body\": \"Closes #{REVIEW_PR}\"}}' ;;\n"
                           "esac\n"
                           "exit 0\n", encoding="utf-8")
        gh_stub.chmod(0o755)
        ssh_stub = fake_bin / "ssh"
        ssh_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        ssh_stub.chmod(0o755)
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{saved.get('PATH') or os.environ.get('PATH', '')}"

        def review_fixture():
            """A fresh lane state / run info pair (independent copies per case)."""
            cfg = {"host": local_host, "backend": BACKEND}
            st = lane_state(local_host, dispatch)
            info = {"status": "exec_error", "error_class": "exec_error", "backend": BACKEND,
                    "model": BACKEND, "summary": "stream interrupted",
                    "started": float(dispatch), "finished": float(now),
                    "branch": None, "commit": None}
            return cfg, st, info

        def fails_of():
            reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
            entry = reviews.get(str(REVIEW_PR))
            check(entry is not None, f"PR #{REVIEW_PR} missing from {reviews_path}: {reviews}")
            return entry.get("fails"), reviews

        # 1. pause hit: the fail counter stays, the log says "not counted"
        reviews_path.write_text(json.dumps({str(REVIEW_PR): {"fails": INITIAL_FAILS}}), encoding="utf-8")
        pauses_path.write_text(json.dumps(pause_row(dispatch - 3, 150)) + "\n", encoding="utf-8")
        cfg, st, info = review_fixture()
        captured.clear()
        call_finish_review(ad, call_values(cfg, st, info, {}))
        fails, reviews = fails_of()
        check(fails == INITIAL_FAILS,
              f"pause hit: fails changed from {INITIAL_FAILS} to {fails!r} in {reviews}")
        text = log_text(captured, root)
        check(any(marker in text.lower() for marker in PAUSE_MARKERS),
              f"pause hit: log has no VM pause marker: {text!r}")
        check("not counted" in text.lower(), f"pause hit: log does not say 'not counted': {text!r}")
        check(pause_row(dispatch - 3, 150)["from"] in text,
              f"pause hit: log does not mention the paused interval: {text!r}")

        # 2. pause miss: the pause happened before the dispatch window -> fails + 1
        reviews_path.write_text(json.dumps({str(REVIEW_PR): {"fails": INITIAL_FAILS}}), encoding="utf-8")
        pauses_path.write_text(json.dumps(pause_row(dispatch - WINDOW_S - 1, 150)) + "\n", encoding="utf-8")
        cfg, st, info = review_fixture()
        captured.clear()
        call_finish_review(ad, call_values(cfg, st, info, {}))
        fails, reviews = fails_of()
        check(fails == INITIAL_FAILS + 1,
              f"pause miss: expected fails {INITIAL_FAILS + 1}, got {fails!r} in {reviews}")

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    print("AD review pause uncounted contract: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: {error}")
        raise SystemExit(1)
