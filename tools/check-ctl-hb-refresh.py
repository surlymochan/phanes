#!/usr/bin/env python3
"""Offline static contract check for ctl-loop ib-refresh heartbeat maintenance (Refs #1007).

对照 37min 静默课（brain/lessons-37min-silence.md）：
`ctl-loop` 在执行 `ib-refresh` 续签 instabox 证书期间，墙钟等待循环（受 REFRESH_CAP 约束，默认 420s）
必须持续刷新 `ctl-loop.heartbeat`（记录最新 date 时间戳），避免因刷新等待导致监督进程假死。
本脚本静态扫描 bin/ctl-loop（或 ~/phanes-ctl/bin/ctl-loop），核对：
1. 定义并遵守 REFRESH_CAP 墙钟等待上限；
2. 引用并维护 ctl-loop.heartbeat 心跳文件；
3. 使用 date 命令获取最新时间戳；
4. 墙钟等待循环体内必须包含心跳刷新逻辑。
"""

from __future__ import annotations

from pathlib import Path
import re
import sys


REFRESH_CAP_DEFAULT = 420
HEARTBEAT_FILE = "ctl-loop.heartbeat"


def extract_loops(source: str) -> list[tuple[str, str]]:
    """Extract (header, body) for while/until loops in shell script."""
    loops: list[tuple[str, str]] = []
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.search(r"\b(while|until)\b", line)
        if match:
            header_parts: list[str] = []
            depth = 0
            body_lines: list[str] = []
            found_do = False
            while i < len(lines):
                cur = lines[i]
                if not found_do:
                    if re.search(r"\bdo\b", cur):
                        found_do = True
                        parts = re.split(r"\bdo\b", cur, maxsplit=1)
                        header_parts.append(parts[0])
                        depth = 1
                        if parts[1].strip():
                            body_lines.append(parts[1])
                    else:
                        header_parts.append(cur)
                else:
                    for word in re.findall(r"\b(do|done)\b", cur):
                        if word == "do":
                            depth += 1
                        elif word == "done":
                            depth -= 1
                    if depth == 0:
                        parts = re.split(r"\bdone\b", cur, maxsplit=1)
                        if parts[0].strip():
                            body_lines.append(parts[0])
                        break
                    else:
                        body_lines.append(cur)
                i += 1
            loops.append(("\n".join(header_parts), "\n".join(body_lines)))
        i += 1
    return loops


def scan_ctl_loop_source(source: str) -> None:
    """Statically verify that ctl-loop maintains heartbeat during ib-refresh wait."""
    # 1. 检查 REFRESH_CAP 墙钟上限
    if not re.search(r"\bREFRESH_CAP\b", source):
        raise AssertionError("ctl-loop 契约不满足: 缺失 REFRESH_CAP 墙钟等待上限")

    # 2. 检查 ctl-loop.heartbeat 心跳定义与引用
    if not re.search(r"heartbeat", source, re.IGNORECASE):
        raise AssertionError("ctl-loop 契约不满足: 缺失 ctl-loop.heartbeat 心跳路径引用")

    # 3. 检查 date 时间戳命令
    if not re.search(r"\bdate\b", source):
        raise AssertionError("ctl-loop 契约不满足: 缺失 date 命令用于时间戳更新或超时计算")

    # 4. 检查 refresh 等待循环体内必须写心跳
    loops = extract_loops(source)
    if not loops:
        raise AssertionError("ctl-loop 契约不满足: 未找到等待循环体 (while/until ... done)")

    refresh_wait_loop_found = False
    for header, body in loops:
        combined = header + "\n" + body
        is_refresh_wait = any(token in combined.lower() for token in ("refresh", "refresh_cap"))
        if is_refresh_wait:
            has_heartbeat = bool(re.search(r"heartbeat", body, re.IGNORECASE))
            has_date = bool(re.search(r"\bdate\b", body))
            if has_heartbeat and has_date:
                refresh_wait_loop_found = True
                break

    if not refresh_wait_loop_found:
        raise AssertionError(
            "ctl-loop 契约不满足 (37min静默课): ib-refresh 墙钟等待循环体内必须刷新 ctl-loop.heartbeat (date)"
        )


def run_contract_fixtures() -> None:
    """Run regression tests against canonical positive and negative ctl-loop snippets."""
    # 正例: 具备 REFRESH_CAP、并在 refresh 等待循环体内通过 date 刷新 heartbeat
    valid_snippet = """#!/usr/bin/env bash
REFRESH_CAP=420
HEARTBEAT_PATH="ctl-loop.heartbeat"
start_ts=$(date +%s)
while kill -0 "$refresh_pid" 2>/dev/null; do
    date +%s > "$HEARTBEAT_PATH"
    now=$(date +%s)
    if [ $(( now - start_ts )) -ge "$REFRESH_CAP" ]; then
        break
    fi
    sleep 5
done
"""
    scan_ctl_loop_source(valid_snippet)

    # 负例 1: 缺少 REFRESH_CAP
    missing_cap = """#!/usr/bin/env bash
HEARTBEAT_PATH="ctl-loop.heartbeat"
while kill -0 "$refresh_pid" 2>/dev/null; do
    date +%s > "$HEARTBEAT_PATH"
    sleep 5
done
"""
    try:
        scan_ctl_loop_source(missing_cap)
    except AssertionError as err:
        assert "REFRESH_CAP" in str(err)
    else:
        raise AssertionError("预期拦截缺少 REFRESH_CAP 的脚本")

    # 负例 2: 循环外写心跳，循环体内未刷新心跳（违反 37min 静默课要求）
    hb_outside_loop = """#!/usr/bin/env bash
REFRESH_CAP=420
HEARTBEAT_PATH="ctl-loop.heartbeat"
start_ts=$(date +%s)
while kill -0 "$refresh_pid" 2>/dev/null; do
    now=$(date +%s)
    if [ $(( now - start_ts )) -ge "$REFRESH_CAP" ]; then
        break
    fi
    sleep 5
done
date +%s > "$HEARTBEAT_PATH"
"""
    try:
        scan_ctl_loop_source(hb_outside_loop)
    except AssertionError as err:
        assert "37min静默课" in str(err)
    else:
        raise AssertionError("预期拦截循环体外写心跳的脚本")

    # 负例 3: 循环体内未用 date 更新
    missing_date = """#!/usr/bin/env bash
REFRESH_CAP=420
HEARTBEAT_PATH="ctl-loop.heartbeat"
while kill -0 "$refresh_pid" 2>/dev/null; do
    echo "alive" > "$HEARTBEAT_PATH"
    sleep 5
done
"""
    try:
        scan_ctl_loop_source(missing_date)
    except AssertionError as err:
        assert "date" in str(err)
    else:
        raise AssertionError("预期拦截缺少 date 的脚本")


def resolve_ctl_loop_path(arg: str | None = None) -> Path | None:
    """Resolve target ctl-loop script path."""
    if arg:
        p = Path(arg).expanduser()
        if p.is_file():
            return p
        raise FileNotFoundError(f"指定的 ctl-loop 文件不存在: {p}")
    candidates = [
        Path("bin/ctl-loop"),
        Path.home() / "phanes-ctl/bin/ctl-loop",
        Path("/root/phanes-ctl/bin/ctl-loop"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def main() -> int:
    target_arg = sys.argv[1] if len(sys.argv) > 1 else None
    target_path = resolve_ctl_loop_path(target_arg)

    # 1. 运行自测正反用例，确保契约断言严格有效
    run_contract_fixtures()

    # 2. 如果存在目标 ctl-loop，执行静态扫描
    if target_path is not None:
        source = target_path.read_text(encoding="utf-8")
        scan_ctl_loop_source(source)
        print(f"OK: ctl-loop ib-refresh heartbeat contract verified ({target_path})")
    else:
        print("OK: ctl-loop ib-refresh heartbeat static check contract verified (fixtures)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
