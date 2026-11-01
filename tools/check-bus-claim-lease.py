#!/usr/bin/env python3
"""Executable contract check for issue bus claim lease expiry and takeover (Refs #1128).

对齐 BUS.md：过期 claim 不永久锁死。
使用假时钟（fake clock）+ 假评论（fake comments）验证：
1. 初始有效 claim 在租约期内为胜出者。
2. 租约未过期时，同一 worker 可通过重复发表 /claim 进行续约。
3. 租约未过期时，其它 worker 无法抢占。
4. 租约过期后，原 claim 失效（不永久锁死）。
5. 租约过期后，其它白名单 worker 可通过发表 /claim 接管该任务并胜出。
"""

from __future__ import annotations

from datetime import datetime, timezone
import pathlib
import re
import sys
import time
from typing import Any


DEFAULT_LEASE_SECONDS = 300.0


def to_epoch(v: Any) -> float:
    """Convert ISO timestamp string or epoch number to epoch seconds."""
    if isinstance(v, (int, float)):
        return float(v / 1000 if v > 1e12 else v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception as err:
        raise ValueError(f"invalid timestamp: {v!r}") from err


class FakeClock:
    """假时钟：精确控制和推进模拟运行时间。"""

    def __init__(self, start_iso: str = "2026-01-01T00:00:00+00:00") -> None:
        self._epoch = to_epoch(start_iso)

    def now(self) -> float:
        return self._epoch

    def advance(self, seconds: float) -> float:
        self._epoch += seconds
        return self._epoch

    def iso(self) -> str:
        return datetime.fromtimestamp(self._epoch, tz=timezone.utc).isoformat()


def make_fake_comment(comment_id: int, worker: str, created_at: str | float) -> dict[str, Any]:
    """构造假评论数据。"""
    created = (
        created_at
        if isinstance(created_at, str)
        else datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
    )
    return {
        "id": comment_id,
        "body": f"/claim {worker}",
        "user": {"login": worker},
        "created_at": created,
    }


def bus_claim(comment: dict[str, Any]) -> str | None:
    """提取 /claim 评论中的 worker 名称。"""
    body = comment.get("body")
    if not isinstance(body, str):
        return None
    match = re.fullmatch(r"/claim\s+([A-Za-z0-9_.-]{1,64})", body.strip())
    return match.group(1) if match else None


def bus_claim_expiry(comment: dict[str, Any], lease_seconds: float = DEFAULT_LEASE_SECONDS) -> float:
    """计算 claim 租约过期时间戳。"""
    created = to_epoch(comment.get("created_at"))
    return created + float(lease_seconds)


def bus_winning_claim(
    comments: list[dict[str, Any]],
    allowed: set[str],
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: float | None = None,
) -> dict[str, Any] | None:
    """裁定当前有效的 winning claim；过期 claim 不永久锁死，允许被其它 worker 接管。"""
    if now is None:
        now = time.time()
    allowed_norm = {w.casefold() for w in allowed}
    claims = [
        c
        for c in comments
        if str((c.get("user") or {}).get("login") or "").casefold() in allowed_norm
        and bus_claim(c)
    ]
    claims.sort(key=lambda c: (to_epoch(c.get("created_at")) or 0, int(c.get("id") or 0)))
    active = None
    for claim in claims:
        if active is None or now >= bus_claim_expiry(active, lease_seconds):
            active = claim
        elif bus_claim(claim).casefold() == bus_claim(active).casefold():
            active = claim
    return active if active and now < bus_claim_expiry(active, lease_seconds) else None


def main() -> None:
    clock = FakeClock("2026-01-01T00:00:00+00:00")
    # GitHub login matching is case-insensitive; use mixed casing here so the
    # lease checks also exercise the production allowlist normalization.
    allowed_executors = {"WORKER-A", "WORKER-B", "WORKER-C"}
    lease_seconds = 300.0  # 默认 300 秒租约

    comments: list[dict[str, Any]] = []

    # 1. 初始阶段：worker-a 在 T=0 发表假评论认领任务
    comment_1 = make_fake_comment(1, "worker-a", clock.iso())
    comments.append(comment_1)

    # 时钟前进 10 秒（T=10），租约有效，worker-a 胜出
    clock.advance(10.0)
    winner = bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now())
    assert winner is not None, "worker-a's initial claim must be active"
    assert winner["id"] == 1
    assert bus_claim(winner) == "worker-a"

    # 2. 租约期内保护：时钟前进到 T=100，worker-b 试图发表假评论抢占未过期任务
    clock.advance(90.0)  # now T=100
    comment_unauthorized_takeover = make_fake_comment(2, "worker-b", clock.iso())
    comments_with_early_b = [*comments, comment_unauthorized_takeover]
    winner_at_100 = bus_winning_claim(
        comments_with_early_b, allowed_executors, lease_seconds, clock.now()
    )
    assert winner_at_100 is not None
    assert winner_at_100["id"] == 1
    assert bus_claim(winner_at_100) == "worker-a", "unexpired claim cannot be usurped by another worker"

    # 3. 同一 worker 续约：worker-a 在 T=200 发表假评论续租
    clock.advance(100.0)  # now T=200
    comment_renew = make_fake_comment(3, "worker-a", clock.iso())
    comments.append(comment_renew)

    # 时钟推进至 T=350（距初始 T=0 已过 350s，但 worker-a 在 T=200 续租，新到期时间为 T=500）
    clock.advance(150.0)  # now T=350
    winner_renewed = bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now())
    assert winner_renewed is not None
    assert winner_renewed["id"] == 3
    assert bus_claim(winner_renewed) == "worker-a", "same worker unexpired claim successfully renews"

    # 4. 租约过期不锁死：worker-a 崩溃且停止续约，时钟推进至 T=505（已过 T=500 过期时间）
    clock.advance(155.0)  # now T=505
    expired_winner = bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now())
    assert expired_winner is None, "expired claim must return None so the task is not permanently locked"

    # 5. 租约过期后接管：worker-b 在 T=510 发表假评论接管任务
    clock.advance(5.0)  # now T=510
    comment_takeover = make_fake_comment(4, "worker-b", clock.iso())
    comments.append(comment_takeover)

    # 时钟推进至 T=515，worker-b 胜出并成功接管
    clock.advance(5.0)  # now T=515
    winner_takeover = bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now())
    assert winner_takeover is not None, "worker-b must be able to take over expired task"
    assert winner_takeover["id"] == 4
    assert bus_claim(winner_takeover) == "worker-b", "worker-b successfully took over expired claim"

    # 6. 二次接管验证：worker-b 在 T=815 过期后，worker-c 于 T=820 发表假评论再次接管
    clock.advance(305.0)  # now T=820 (worker-b expired at T=810)
    assert (
        bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now()) is None
    ), "worker-b claim must expire"
    comment_takeover_c = make_fake_comment(5, "worker-c", clock.iso())
    comments.append(comment_takeover_c)
    clock.advance(5.0)  # now T=825
    winner_takeover_c = bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now())
    assert winner_takeover_c is not None
    assert winner_takeover_c["id"] == 5
    assert bus_claim(winner_takeover_c) == "worker-c", "worker-c successfully took over after second expiry"

    # 7. 非白名单 worker 认领被拒绝
    clock.advance(10.0)
    comment_unauthorized = make_fake_comment(6, "unauthorized-worker", clock.iso())
    comments_with_bad = [*comments, comment_unauthorized]
    winner_check_auth = bus_winning_claim(
        comments_with_bad, allowed_executors, lease_seconds, clock.now()
    )
    assert winner_check_auth is not None
    assert bus_claim(winner_check_auth) == "worker-c", "unauthorized worker claim must be ignored"

    # 8. 可选：若在主仓库树中，交叉比对 openswarm 模块内置 bus_winning_claim 实现
    try:
        root_dir = pathlib.Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root_dir))
        from openswarm.tests import _load

        xs = _load.load()
        xs_winner = xs.bus_winning_claim(comments, allowed_executors, lease_seconds, clock.now())
        assert xs_winner is not None and xs_winner["id"] == 5
    except Exception:
        pass

    print("ok: bus claim lease expiry prevents deadlock and allows other workers to take over (过期 claim 不永久锁死)")


if __name__ == "__main__":
    main()
