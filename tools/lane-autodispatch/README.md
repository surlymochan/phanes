# lane-autodispatch (repo mirror)

Runtime copy lives on the box at `~/phanes-ctl/bin/autodispatch.py`.
Backend starvation rules: [`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md).
Which idle lane gets the next card is `dispatch.lane_pick` (`lru` = the lane that was
dispatched longest ago, `first` = the lanes.json order); the dispatch state is
`lane-lru.json` in `$OPENSWARM_HOME` (`~/.claude/openswarm`).

```bash
python3 tools/lane-autodispatch/test_can_take.py
python3 tools/check-ad-lane-lru.py
```
