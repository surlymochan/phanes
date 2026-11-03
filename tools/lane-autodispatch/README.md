# lane-autodispatch (repo mirror)

Runtime copy lives on the box at `~/phanes-ctl/bin/autodispatch.py`.
Backend starvation rules: [`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md).

Two versions, one source: this file is the repo mirror, `~/phanes-ctl/bin/autodispatch.py` on the
box is the deployed runtime the control plane syncs from it (sync steps:
[`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md)); a change here is
only live on the box after that sync.

Base-commit validation: the base commit must exist in the target repo before a lane is assigned
(`git -C <repo> cat-file -e <base>^{commit}`). A missing base is fetched into the target repo and
the check is repeated (Refs #1810); a base that is still missing is reported and backed off as
before. A failing fetch aborts the dispatch right away: a later successful check (e.g. a concurrent
tick that fetched the object in the meantime) must never mask it.

```bash
python3 tools/lane-autodispatch/test_can_take.py
python3 tools/check-ad-base-fetch-on-miss.py
python3 tools/lane-autodispatch/test_harvest_self_check.py
```

## Harvest self-check (Refs #1729)

`harvest_self_check.py` is the machine gate the harvest path runs **before** a lane
branch is pushed (`autodispatch.py: finish()`):

| gate | trigger | effect |
| --- | --- | --- |
| binary / build artefact | the diff adds a binary file, a dependency directory (`node_modules`, `dist`, `vendor`, …) or an archive | the harvest is refused, the file list is published to the harvest log, the issue and the applicable PR |
| `no-tests` | the diff changes code but no test file | the PR is labelled `no-tests` |
| `missing-failure-tests` | a control-plane change (`dispatch` / `autodispatch` / `capacity` / `pause`) whose **changed** test content adds no failure-path test | the PR is labelled `missing-failure-tests` |

The result is written to the harvest log (`autodispatch.log` + `brain/log.md`), to the
issue comment and to the applicable PR comment; the rejected files are listed with
their reason (`binary`, `dependency-dir`, `build-artefact`). The PR labels are created
when they are missing, added to the PR and read back to verify that they took effect;
a label that cannot be created or added is logged and reported on the PR
(`Labels not applied: …`). A broken self-check never blocks a harvest: it fails open
and the warning is published instead. A test diff too large to judge is not judged
either — the gate reports it instead of guessing.

Legitimate binary resources (test fixture images for instance) are exempted with the
allowlist file `.harvest-self-check-allow` in the repository root — one glob per line,
`#` starts a comment:

```
# fixture images are legitimate
tests/fixtures/*.png
```

The box copies `autodispatch.py` alone into `~/phanes-ctl/bin/`; the self-check module
is looked up next to that file and in the repo mirror (`/workspace/phanes-repo`), and a
missing module only means the gate fails open.
