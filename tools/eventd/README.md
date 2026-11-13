# eventd

依赖 Python 3 标准库。全局选项放在子命令前。启动消费者前先启动汇总器和独立采集器（不要与其他汇总器同时写入同一日志）：

```sh
tools/eventd/eventd --backend fake ingest-runs
tools/eventd/eventd --backend fake serve
tools/eventd/eventd --backend fake group add batch --pattern 'lane-' --expect 3
tools/eventd/eventd --backend fake group ls
tools/eventd/eventd --backend fake wait --consumer coordinator --timeout 300
tools/eventd/eventd --backend fake replay events.log
```

`wait` 输出一行 JSON：`reason`、`events`、`groups`、`cursor`，有唤醒时退出 0；超时退出 1，错误退出 2。空闲时每秒检查一次 SQLite 队列，发现新事件的延迟最多约一秒；等待不会忙等。分组终态进度按消费者和表达式隔离，按新增事件增量更新，并保存在数据目录的 SQLite 缓存中；新增/修改分组表达式会建立对应的进度缓存。每个消费者使用不同名称。`serve` 不启动消费者；Claude Code 可后台运行 `wait`，Codex 可在当前轮次等待同一命令。

分组缓存将每个已计数 lane 单独存为 SQLite 行，并独立保存计数及评估游标。每条新增终态只按主键插入 lane，首次插入时增加计数；不会随 lane 数量增长反复读取、序列化整个分组。lane、计数和游标在同一事务内提交，重连继续增量处理；旧版 JSON 分组缓存首次使用时自动迁移一次。修改 expect 复用已有计数；恢复时若请求较早游标，则重建该位置的快照。

本地回归测试（临时数据全部留在当前工作树，不使用实时控制面）：

```sh
mkdir -p .tmp/test-tmp
TMPDIR="$PWD/.tmp/test-tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/eventd -p 'test*.py' -v
```

性能回归覆盖 20,000 条不同 lane 的终态历史、其中 4,000 条待评估的单次轮询，要求墙钟时间小于 5 秒、CPU 时间小于 3 秒；另验证已有大量 ignore/defer 历史时，空闲等待至少 2.1 秒且 CPU 时间小于 0.2 秒。也覆盖旧缓存迁移、事务失败回滚、重连去重及分组完成唤醒。

`ingest-runs` 每秒扫描 `--runs-dir` / `EVENTD_RUNS_DIR`（默认 `~/.claude/openswarm-executor-candidate/runs`），用 `--watch-command` 指定 swrm 程序，自动跟随新 run，并从 result.json 补报缺失的 DONE。采集器用完整 run ID 作为日志标签；历史别名通过之前最近的 `[title] START id` 映射，支持行首时间及 `[title HH:MM] DONE`。同名 run 并发或缺少 START 时无法可靠消歧，需人工核对。日志旁的锁文件防止两个 eventd 采集器同时运行；不能阻止其他汇总器写入。已有的历史活动 run 会从头跟随。

`MSG` 是 lane 的不可信自由文本，默认按事件类型忽略，即使正文含 `fatal` 或 `status=failed` 也不唤醒；`RISK` 正文即使含 `status=failed`，仍进入决策层。这些正文也不计作分组终态。真实失败由 `DONE` 状态或 `EXITED` 事件表达；`EXITED` 直接唤醒。

`--log` / `EVENTD_LOG` 指定汇总日志；`--data-dir` / `EVENTD_DATA_DIR` 指定数据库、规则和审计目录。`--backend` / `EVENTD_BACKEND` 可设 `fake`、`openai`、`decisions`（默认）、`codex`。OpenAI 需设置环境变量 `OPENAI_API_KEY`，或通过 `--env-file` 加载该变量（已有环境变量优先），默认模型为 `gpt-6-luna`，effort 为 `high`；可用 `--model` / `EVENTD_MODEL` 和 `--effort` / `EVENTD_EFFORT` 配置。禁止使用 gpt-5.4 系列。`decisions` 只用 gpt-6-luna 的 choice 判断，不提供工具；失败时依次回退 Responses API 的 `gpt-6-luna` / `high`、`gpt-6.1-terra` / `medium`，都失败则唤醒。`openai` 后端失败时回退 Terra；审计记录实际模型、effort 和回退状态。缺少密钥时 `serve` 和 `wait` 会报错。默认选择依据：53 个冻结案例中 decisions 漏唤醒 0 次、准确率 98.11%，Responses Luna high 分别为 2 次和 96.23%；样本有限，详见 [EVAL.md](EVAL.md)。文件仅解析密钥赋值，不执行 shell 命令。模型没有提供工具。当前 Codex CLI 无法验证禁用全部工具，因此 Codex 后端保守判定为不可用并唤醒，尚不执行模型调用。可调 `--decision-timeout`、`--hourly-limit`、`--max-silence`、`--health-timeout`（时间单位为秒）。超限及决策错误均唤醒。每条判断写入数据目录 `decisions.jsonl`。

消费者规则文件为数据目录的 `consumers/NAME.json`，例如：

```json
{"overrides": [{"pattern": "routine notice", "action": "ignore"}]}
```

支持 `wake`、`defer`、`ignore`；用于规则层不能确定的事件及 routine ignore（包括 MSG、START、RESUME、HEARTBEAT），允许用 wake 规则唤醒这些事件。失败和分组结束的强制唤醒不能被覆盖。文件修改在下一次扫描新事件时生效。分组完成按匹配终态的不同 lane 数量统计，匹配表达式应包含终态行中的 lane 名。

`serve` 记录源文件身份、完整行偏移及已消费前缀哈希，检测 inode 变化、文件缩短及前缀变化，包含截断后快速增长超过旧偏移的情况。每轮会重读已消费前缀；原样重建此前缀的截断无法通过轮询区分。消费者独立保存交付游标；已判断事件的最大 ID 作为持久化评估游标，ignore/defer 也推进评估位置，空闲轮询不再遍历这些事件。defer 事件保留至后续唤醒或 max-silence 时交付，重启后仍可恢复；请勿同时运行同名消费者。stdout 写出和游标提交无法在两个独立系统之间原子完成：若进程在写出后、提交前崩溃，恢复可能重复交付。消费者应按游标去重；交付语义为至少一次：事件不丢，但崩溃时允许重复；消费者必须按游标去重。当前协议没有 ACK。`replay` 使用 fake 决策器，读取当前分组规则，不改变实时游标；输出 `summary` 包含事件数、唤醒数和决策调用数。可用 `python3 tools/eventd/eval_report.py --log /path/to/events-all.log` 重算当前规则进入决策层的比例。

## 服务示例（仅示意，不自动安装）

先按实际位置替换路径，将密钥写入仅所有者可读的 `/path/to/eventd.env`，内容为 `OPENAI_API_KEY=...`。采集与服务分别运行；消费者也需要相同的 `--env-file`。

launchd：分别保存以下 plist，修改路径与 Label 后用 `launchctl bootstrap gui/$(id -u) 文件路径` 加载。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.example.eventd</string>
  <key>ProgramArguments</key><array><string>/path/to/eventd</string><string>--env-file</string><string>/path/to/eventd.env</string><string>serve</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/path/to/eventd-error.log</string>
</dict></plist>
```

采集器使用同样 plist，将 Label 改为 `org.example.eventd-ingest`，将 `serve` 改为 `ingest-runs`，并在子命令前加 `--watch-command /absolute/path/to/swrm`；两者使用相同 `--log`。

systemd 用户服务 `~/.config/systemd/user/eventd.service`：

```ini
[Unit]
Description=Event wake-up daemon
[Service]
ExecStart=/path/to/eventd --env-file /path/to/eventd.env serve
Restart=always
RestartSec=2
[Install]
WantedBy=default.target
```

采集器 `~/.config/systemd/user/eventd-ingest.service`：

```ini
[Unit]
Description=OpenSwarm event ingestion
[Service]
ExecStart=/path/to/eventd --env-file /path/to/eventd.env --watch-command /absolute/path/to/swrm ingest-runs
Restart=always
RestartSec=2
[Install]
WantedBy=default.target
```

执行 `systemctl --user daemon-reload`，再用 `systemctl --user enable --now eventd.service eventd-ingest.service` 启动。
