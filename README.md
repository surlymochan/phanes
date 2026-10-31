# Phanes

> **当前迁移状态：** 本仓库正收敛 OpenBot、OpenSwarm 和连接层。源码导入与构建检查不代表正式部署或真实任务验收完成；最新状态见 [HANDOFF.md](HANDOFF.md)。

- [`openbot/`](openbot/)：OpenBot Hub 和界面。
- [`openswarm/`](openswarm/)：执行器、路由与跨主机并发。
- [`integrations/`](integrations/)：Hub 与执行网络的连接入口。
- Issue、PR 和 Discussions 以本仓库为入口，既有 Phanes Project 保留历史与跨项目视图。

组件保留各自许可证：OpenBot 使用 [PolyForm Noncommercial 1.0.0](openbot/LICENSE)，OpenSwarm 使用 [Apache 2.0](openswarm/LICENSE)。其他文件适用其自身声明。

**A unified agent network: one control plane, distributed heterogeneous execution, unified resource management, and evidence-based self-improvement.**

Phanes brings three existing systems together into a single network for running fleets of AI coding agents:

- **[OpenBot](https://github.com/nibiru-labs/openbot)** is the control plane — user entry point, agent identity, goals and tasks, authorization, and the final accept/reject decision.
- **[openswarm](https://github.com/surlymochan/openswarm)** is the execution plane — resource registry, scheduling across machines and model backends, run/attempt state, cancellation and recovery, and run evidence.
- **RSI (agent-knowledge)** is the learning layer — versioned experience, reusable patterns, evaluation rules, and improvement proposals distilled from completed work.

GitHub sits alongside them as the evidence surface for code work: issues, pull requests, reviews, and releases that reference run records.

> **Status:** The design has passed a multi-model adversarial review and is being built in stages (see Roadmap). This repository contains the component sources and integration entry points as the consolidation proceeds; deployment and end-to-end acceptance remain in progress.

## Core contract

Everything in Phanes flows through one versioned contract:

```
Goal → Task → Run / Attempt → Evidence
```

- A **Goal** states intent and acceptance criteria. A **Task** is a unit of work derived from it.
- Each execution is a **Run** made of **Attempts**, addressed by a stable launch key end to end. A transport retry reuses the same key; the same key with different content is rejected.
- Every attempt produces **Evidence**: outputs, verification results, and review records bound to the exact candidate (commit SHA or artifact digest).
- **Final acceptance belongs to the control plane and must cite independent verification.** A worker's self-report is never an acceptance basis.

Resources (hosts, backends, models) are described separately from agents: capabilities, repository mappings, permissions, concurrency limits, and health. Credentials are stored by reference only.

## Design principles

- **One source of truth per concern.** Run facts live in the execution plane; acceptance decisions live in the control plane. No component keeps a second copy of another's truth.
- **Authority is enforced by real credentials and isolation**, not by prompts. Workers run under dedicated identities in isolated workspaces, with no access to production, deployment, or scheduling credentials.
- **No silent recovery.** A run in an unknown state keeps its slot, raises an alert, and waits for reconciliation or a human decision — it is never auto-released or blindly retried.
- **Deduplication survives reconnects.** Dispatch intent is persisted before a start command is sent; a lost receipt triggers a query, not a duplicate launch. Stale attempts must be confirmed terminated before a retry or reassignment.
- **Self-improvement is evidence-driven and gated.** RSI proposes experience and policy changes; weighted routing stays record-only until controlled comparisons on the same task type and environment justify enabling it, and every change can roll back.

## Roadmap

| Stage | Focus |
|---|---|
| **A** | Stable foundation: hub upgrade and acceptance, contract freeze, integration of audited fixes in risk order |
| **B** | Static unified resource pool: single-writer scheduler with a transactional dispatch log, quota grouped by credential reference |
| **C** | Reliable dispatch and a real closed loop: atomic launch registration, persistent termination marks, independent verification before acceptance |
| **D** | Networking and A2A migration: mesh connectivity pilot, capability-by-capability migration with a rollback path for each |
| **E** | Evidence-driven self-evolution: regression cards, complete outcome/cost records, gated policy changes with small-scope trials |

Formal release of each stage requires passing negative tests (wrong-host receipts, forbidden data access, duplicate and out-of-order results) and a full trace from every accepted result back to its task, base, candidate, verification, review, and policy version.

## Related projects

- [openflow](https://github.com/surlymochan/openflow) — executable delivery workflows with stage contracts and evidence gates
- [openswarm](https://github.com/surlymochan/openswarm) — quota-aware multi-agent scheduling for the execution plane
- [openbot](https://github.com/nibiru-labs/openbot) — the control-plane client and hub
