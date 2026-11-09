"""Build a reproducible, balanced manifest for paired L2 experiments."""

import argparse
import json
import random
import re
import sys
from pathlib import Path


RUN_FIELDS = {"run_id", "card_id", "base", "arm", "holdout_suite_digest"}
PAIR_FIELDS = {"pair_id", "runs"}
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def build_manifest(payload, seed):
    _require(isinstance(payload, dict) and set(payload) == {"pairs"},
             "input must contain only a pairs array")
    pairs = payload["pairs"]
    _require(isinstance(pairs, list) and 10 <= len(pairs) <= 20,
             "manifest must contain 10 to 20 pairs")

    seen_pairs = set()
    seen_runs = set()
    seen_cards = set()
    normalized = []
    arms = None
    for pair in pairs:
        _require(isinstance(pair, dict) and set(pair) == PAIR_FIELDS,
                 "each pair must contain pair_id and runs only")
        pair_id = pair["pair_id"]
        _require(isinstance(pair_id, str) and pair_id, "pair_id must be a non-empty string")
        _require(pair_id not in seen_pairs, f"duplicate pair_id: {pair_id}")
        seen_pairs.add(pair_id)
        runs = pair["runs"]
        _require(isinstance(runs, list) and len(runs) == 2,
                 f"pair {pair_id} must contain exactly two runs")
        _require(all(isinstance(run, dict) and set(run) == RUN_FIELDS for run in runs),
                 f"runs in {pair_id} must contain the required fields only")

        for run in runs:
            for key in RUN_FIELDS:
                _require(isinstance(run[key], str) and run[key],
                         f"{key} in {pair_id} must be a non-empty string")
            _require(run["run_id"] not in seen_runs, f"duplicate run_id: {run['run_id']}")
            seen_runs.add(run["run_id"])
            _require(DIGEST_RE.fullmatch(run["holdout_suite_digest"]),
                     f"invalid holdout suite digest in {pair_id}")

        _require(runs[0]["card_id"] == runs[1]["card_id"],
                 f"pair {pair_id} has different card IDs")
        card_id = runs[0]["card_id"]
        _require(card_id not in seen_cards, f"duplicate card_id: {card_id}")
        seen_cards.add(card_id)
        _require(runs[0]["base"] == runs[1]["base"],
                 f"pair {pair_id} has different frozen bases")
        _require(runs[0]["holdout_suite_digest"] == runs[1]["holdout_suite_digest"],
                 f"pair {pair_id} has different holdout suite digests")
        pair_arms = [run["arm"] for run in runs]
        _require(pair_arms[0] != pair_arms[1], f"pair {pair_id} must have two different arms")
        if arms is None:
            arms = sorted(pair_arms)
        _require(sorted(pair_arms) == arms, f"pair {pair_id} has inconsistent arms")
        normalized.append({"pair_id": pair_id, "runs": [dict(run) for run in runs]})

    normalized.sort(key=lambda pair: pair["pair_id"])
    first_arms = [arms[index % 2] for index in range(len(normalized))]
    random.Random(seed).shuffle(first_arms)
    ordered_pairs = []
    for pair, first_arm in zip(normalized, first_arms):
        run_by_arm = {run["arm"]: run for run in pair["runs"]}
        ordered_pairs.append({
            "pair_id": pair["pair_id"],
            "runs": [run_by_arm[first_arm], run_by_arm[arms[1] if first_arm == arms[0] else arms[0]]],
        })
    return {"seed": seed, "pairs": ordered_pairs}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        manifest = build_manifest(payload, args.seed)
        rendered = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        args.output.write_text(rendered, encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"l2_paired_manifest: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
