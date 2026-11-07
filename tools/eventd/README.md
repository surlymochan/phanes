# eventd

依赖 Python 3 标准库。全局选项放在子命令前。启动消费者前先启动汇总器（本模块只消费已有追加日志）：

```sh
tools/eventd/eventd --backend fake serve
tools/eventd/eventd --backend fake group add batch --pattern 'lane-' --expect 3
tools/eventd/eventd --backend fake group ls
tools/eventd/eventd --backend fake wait --consumer coordinator --timeout 300
tools/eventd/eventd --backend fake replay events.log
```

`wait` 输出一行 JSON：`reason`、`events`、`groups`、`cursor`，有唤醒时退出 0；超时退出 1，错误退出 2。每个消费者使用不同名称。`serve` 不启动消费者；Claude Code 可后台运行 `wait`，Codex 可在当前轮次等待同一命令。

`--log` / `EVENTD_LOG` 指定汇总日志；`--data-dir` / `EVENTD_DATA_DIR` 指定数据库、规则和审计目录。`--backend` / `EVENTD_BACKEND` 可设 `fake`、`openai`（默认）、`codex`。OpenAI 需设置环境变量 `OPENAI_API_KEY`，或通过 `--env-file` 加载该变量（已有环境变量优先），默认模型为 `gpt-6-luna`，effort 为 `high`；可用 `--model` / `EVENTD_MODEL` 和 `--effort` / `EVENTD_EFFORT` 配置。禁止使用 gpt-5.4 系列。决策失败时重试一次 `gpt-6.1-terra`、`medium`，仍失败则唤醒；审计记录实际模型、effort 和回退状态。缺少密钥时 `serve` 和 `wait` 会报错。文件仅解析密钥赋值，不执行 shell 命令。模型没有提供工具。当前 Codex CLI 无法验证禁用全部工具，因此 Codex 后端保守判定为不可用并唤醒，尚不执行模型调用。可调 `--decision-timeout`、`--hourly-limit`、`--max-silence`、`--health-timeout`（时间单位为秒）。超限及决策错误均唤醒。每条判断写入数据目录 `decisions.jsonl`。

消费者规则文件为数据目录的 `consumers/NAME.json`，例如：

```json
{"overrides": [{"pattern": "routine notice", "action": "ignore"}]}
```

支持 `wake`、`defer`、`ignore`；只用于规则层不能确定的事件，失败和分组结束不能被覆盖。文件修改在下一次扫描新事件时生效。分组完成按匹配终态的不同 lane 数量统计，匹配表达式应包含终态行中的 lane 名。

`serve` 记录源文件身份、完整行偏移及已消费前缀哈希，检测 inode 变化、文件缩短及前缀变化，包含截断后快速增长超过旧偏移的情况。每轮会重读已消费前缀；原样重建此前缀的截断无法通过轮询区分。消费者独立保存游标；请勿同时运行同名消费者。stdout 写出和游标提交无法在两个独立系统之间原子完成：若进程在写出后、提交前崩溃，恢复可能重复交付。消费者应按游标去重；交付语义为至少一次：事件不丢，但崩溃时允许重复；消费者必须按游标去重。当前协议没有 ACK。`replay` 使用 fake 决策器，读取当前分组规则，不改变实时游标。

## 服务示例（仅示意，不自动安装）

launchd：创建 plist 并按实际安装位置填写 `ProgramArguments`：

```xml
<plist version="1.0"><dict>
<key>Label</key><string>org.example.eventd</string>
<key>ProgramArguments</key><array><string>/path/to/eventd</string><string>serve</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
```

systemd 用户服务：

```ini
[Unit]
Description=Event wake-up daemon
[Service]
ExecStart=/path/to/eventd serve
Restart=always
[Install]
WantedBy=default.target
```
