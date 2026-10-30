### Added

- Agents can send independent coding work to an openswarm lane or detached run from their own chat.
  OpenBot returns the run id, then adds the result to the same chat when the run ends. Review its
  branch, commit, summary, and verify result before reporting success. Set the openswarm binary,
  default lane, and default fleet host in Settings under provider detection.
- Dispatch results appear as script events in chat and history, not as messages from the person.
- Risky coding dispatches wait for the person's approval before an executor starts. Fixed rules
  check migrations, schema changes, releases, version bumps, tags, git push or merge or force,
  publishing, credential changes, and renderer-to-main trust boundary work. Agents can also set
  `risk: true`. Only an explicit auto-approve grant for that agent can answer automatically.
- The agent reviews each dispatch result against the card's scope, expected files, verify evidence,
  and excluded or risky work. One corrected retry is permitted. A second failure goes to the person.
- More risky coding dispatches now require approval, including database deletion, production
