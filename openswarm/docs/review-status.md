# lane-review commit status

车道审查（lane-review）完成后，把结论作为 GitHub **commit status** 发到被审查的提交上，
这样分支保护规则（或人）不用读审查正文就知道这个提交能不能合。

实现：`openswarm/lib/review/status.py`（纯模块，只依赖标准库；父卡负责接线到
`openswarm/bin/openswarm`）。测试：`openswarm/tests/test_review_status.py`。

## context

status 固定发在 `lane-review` 这个 context 下：一个车道审查只占一个 context，便于
分支保护按 context 精确匹配。调用方也可以传别的 context（`build_status(..., context=...)`），
但默认值就是 `lane-review`。

## 门禁配置只有一个来源

review 门禁（仓库、status context、目标分支）只从调度器配置里读：

| 字段 | 含义 | 默认值 |
| --- | --- | --- |
| `repo` | status 发布在哪个仓库 | 无 |
| `review.status_context` | status 发在哪个 context 下 | `lane-review` |
| `base_branch` | 哪个分支的分支保护要 require 这个 context | `main` |

`status.resolve_gate(cfg)` 是唯一的解析入口：发 status 的流程、CI 过滤（把本 context 从
check rollup 里排除）、`openswarm doctor` 的分支保护检查都走它，不会各读各的配置。
调度器把 context 改成 `custom-review`，分支保护就必须 require `custom-review`；doctor 读的
也是同一个 context，不会拿默认的 `lane-review` 去报告 OK。

doctor 读不到调度器配置时（调度器的 `--config` 是必填参数，配置可能在别的路径、别的仓库、
别的 context），门禁就是**未知**而不是默认：doctor 报 WARN 并提示用
`--dispatch-config <调度器配置路径>` 重新跑，不会拿当前 worktree 的 git remote 去猜仓库、
也不会拿默认 context 去报 OK。

## 状态映射

审查结论 markdown 里用 `VERDICT:` 行给出结论（行首、大小写敏感、只认第一处）：

| 审查结论 | commit status | 含义 |
| --- | --- | --- |
| `VERDICT: APPROVE` | `success` | 可以合 |
| `VERDICT: CHANGES` | `failure` | 要改，不合 |
| 没有 `VERDICT:` 行 | `pending` | 审查没给出结论，既不是通过也不是拒绝 |

`parse_verdict(text)` 同时取出第一处 `SUMMARY:` 行作为摘要；摘要只用于人读，不进 status。

description 形如：

```
APPROVE by codex-reviewer (codex/gpt-5) run run-1
```

超过 140 字符（GitHub 上限）就截断到 140 字符并以 `…` 结尾。

## 发送

```sh
gh api -X POST repos/<owner>/<repo>/statuses/<sha> \
    -f state=success \
    -f context=lane-review \
    -f description="APPROVE by codex-reviewer (codex/gpt-5) run run-1"
```

`post_status(repo, payload)` 不抛异常：审查本身不该因为发不出 status 就失败。

- `repo` 为空 → `{"skipped": "no repository configured"}`，不发。
- 环境里既没有 `GH_TOKEN` 也没有 `GITHUB_TOKEN`，并且 `gh auth status` 失败 → `{"skipped": ...}`，不发。
- `gh api` 失败（退出码非 0）→ `{"error": ...}`，不抛异常。

`sha` 必须是 40 位十六进制的提交号，否则 `build_status` 抛 `ValueError`：指不到提交的
status 会挂到错误的提交上。

## head 改写：新 head = 未审

status 描述的是**被审查的那个提交**。任何改写 PR head 的路径都产生了一个没人审过的提交，
因此**一律不发 status**：

- rebase（换基线、压提交）
- CI fix（为了让 CI 绿而追加的修复提交）
- union merge（合并上游/main 后产生的合并提交）

调用方在发 status 和合并之前必须用 `head_changed(reviewed_sha, current_sha)` 判断：
head 变了就返回 True，此时**不发 status、也不合并**，新 head 必须重新审查。
任一侧拿不到 sha 时同样按“变了”处理——未知的 head 不能当作没变。

## 用法

```python
verdict, summary = status.parse_verdict(review_text)
if status.head_changed(reviewed_sha, current_sha):
    return  # head 变了：不发 status、不合并，等重新审查
payload = status.build_status(verdict, reviewed_sha, reviewer, backend, model, run_id)
result = status.post_status(repo, payload)
# result: {"posted": True, ...} / {"skipped": reason} / {"error": ...}
```
