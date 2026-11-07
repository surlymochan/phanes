# TDD role fixtures

Frozen evidence bundles for `src/roles/tdd.js`. They are the reducer's input
contract: one JSON object per stage, shaped like the evidence the controller
freezes between the red and the green turn.

Every fixture carries the same `spec` (one public scenario `S1`, a scenario
mapping to `test/feature.test.js`, and an approved file scope) so that a
difference in the verdict always comes from the evidence under test and
never from the spec.

| fixture | what it proves |
|---|---|
| `red/accepted.json` | an assertion failure with a replay receipt, a sealed snapshot and a scenario mapping is accepted |
| `red/posthoc-production.json` | a red commit that already carries production code is refused |
| `red/not-assertion.json` | a red that fails on an import error is refused |
| `red/empty-tests.json` | a red that ran no test at all is refused |
| `red/missing-snapshot.json` | a red without a sealed snapshot cannot be replayed |
| `red/unmapped-scenario.json` | a scenario without a test mapping is refused |
| `green/accepted.json` | the same frozen tests turning green is accepted |
| `green/test-changed.json` | a green that edits the frozen test is refused |
| `green/config-changed.json` | a green that edits the frozen runner configuration is refused |

The digests are opaque content addresses. They are not the digest of any
file in this repository and carry no meaning outside the fixtures.
