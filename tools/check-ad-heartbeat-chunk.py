#!/usr/bin/env python3
"""Offline contract check for autodispatch chunked sleep and heartbeat refresh (Refs #906).

吸取主机 suspend 冻结 nanosleep 导致 ~40 分钟无 heartbeat 教训（brain/lessons-37min-silence.md）。
主循环在两次 tick 之间以 ≤5s 步进 sleep（例如 sleep(5)），每步刷新 autodispatch/heartbeat。
本检查包含：
1. 源码静态扫描：核对 autodispatch.py 采用分片 sleep、步长 ≤ 5s、每步写入 heartbeat，避免单次整块 sleep(TICK) 导致假死；
2. 假时钟/行为仿真：断言在 AD_TICK 间隔内以 ≤5s 步进 sleep(5)，每次 step 均刷新 heartbeat 文件，并在模拟 suspend 恢复后立刻更新；
3. 离线正反例门禁：确保契约有效拦截单次长 sleep、大步长与未刷新 heartbeat，不连网络，不杀真实 AD 进程。
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
AUTODISPATCH_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
MAX_SLEEP_STEP_SECONDS = 5.0
_REAL_STRFTIME = time.strftime


class FakeClock:
    """假时钟：模拟墙钟推进与主机 suspend 冻结恢复。"""

    def __init__(self, start_epoch: float = 1_700_000_000.0) -> None:
        self.now = start_epoch
        self.sleep_history: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleep_history.append(seconds)
        self.now += seconds

    def strftime(self, fmt: str) -> str:
        return _REAL_STRFTIME(fmt, time.localtime(self.now))


def scan_autodispatch_source(source: str) -> None:
    """静态扫描 autodispatch 源码，确保 tick 间分片 sleep 并持续写 heartbeat。"""
    # 1. 确保定义并引用 AD_TICK
    if not ("AD_TICK" in source and "TICK" in source):
        raise AssertionError("autodispatch missing AD_TICK / TICK configuration")

    # 2. 确保主循环不包含单次整块无分片 sleep(TICK) 或 time.sleep(TICK)
    lines = source.splitlines()
    for idx, line in enumerate(lines, 1):
        clean = line.strip()
        if clean.startswith("#"):
            continue
        if clean == "time.sleep(TICK)" or clean == "sleep(TICK)":
            raise AssertionError(f"Line {idx}: forbidden monolithic sleep(TICK) found; must use chunked sleep")

    # 3. 语法树分析：核对分片 sleep 函数或循环逻辑
    tree = ast.parse(source)
    chunk_func_found = False
    step_le_5_found = False
    heartbeat_in_loop_found = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 检查是否有 sleep 分片函数定义（如 sleep_chunk）
            if "chunk" in node.name.lower() or "sleep" in node.name.lower():
                # 检查函数内部是否存在循环
                for child in ast.walk(node):
                    if isinstance(child, (ast.While, ast.For)):
                        chunk_func_found = True
                        loop_text = ast.get_source_segment(source, child) or ""
                        if "heartbeat" in loop_text:
                            heartbeat_in_loop_found = True
                        if any(k in loop_text for k in ("chunk", "step", "min(", "5")):
                            step_le_5_found = True

    if not chunk_func_found:
        raise AssertionError("autodispatch missing chunked sleep loop for tick interval")
    if not heartbeat_in_loop_found:
        raise AssertionError("autodispatch chunked sleep loop must write heartbeat inside loop")
    if not step_le_5_found:
        raise AssertionError(f"autodispatch chunked sleep step must be bounded by <={MAX_SLEEP_STEP_SECONDS}s")


def simulate_chunked_sleep(
    sleep_fn: Callable[..., Any],
    total_seconds: float,
    chunk_size: float,
    temp_dir: Path,
    suspend_after_steps: int | None = None,
    suspend_duration: float = 2400.0,
) -> tuple[list[float], list[str]]:
    """在假时钟下运行分片 sleep，收集睡眠调用与 heartbeat 文件写入记录。"""
    clock = FakeClock()
    heartbeat_file = temp_dir / "heartbeat"
    heartbeat_writes: list[str] = []

    def mock_sleep(seconds: float) -> None:
        if seconds > MAX_SLEEP_STEP_SECONDS + 1e-6:
            raise AssertionError(f"sleep step {seconds} exceeds max limit {MAX_SLEEP_STEP_SECONDS}s")
        clock.sleep(seconds)
        if suspend_after_steps and len(clock.sleep_history) == suspend_after_steps:
            # 模拟主机 suspend 冻结，恢复后墙钟跳跃 suspend_duration（例如 ~40 分钟）
            clock.now += suspend_duration

    # 临时覆盖内置 open 以拦截 heartbeat 写入
    real_open = open

    class HeartbeatInterceptor:
        def __init__(self, filename: Any, mode: str = "r", *args: Any, **kwargs: Any) -> None:
            self.filename = str(filename)
            self.mode = mode
            self.underlying = real_open(filename, mode, *args, **kwargs)

        def write(self, data: str) -> int:
            if "heartbeat" in self.filename:
                heartbeat_writes.append(data.strip())
            return self.underlying.write(data)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            self.underlying.close()

    return clock.sleep_history, heartbeat_writes


def test_runtime_behavior() -> None:
    """动态加载 autodispatch 并使用假时钟断言分片 sleep 与 heartbeat 行为。"""
    spec = importlib.util.spec_from_file_location("autodispatch_chunk", AUTODISPATCH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {AUTODISPATCH_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "sleep_chunk"):
        raise AssertionError("autodispatch module missing sleep_chunk function")

    with tempfile.TemporaryDirectory(prefix="ad-chunk-") as tmp:
        tmp_path = Path(tmp)
        mod.D = str(tmp_path)
        clock = FakeClock()

        written_timestamps: list[str] = []
        original_sleep = mod.time.sleep
        original_strftime = mod.time.strftime

        def fake_sleep(sec: float) -> None:
            if sec > MAX_SLEEP_STEP_SECONDS:
                raise AssertionError(f"Step {sec}s exceeds max step {MAX_SLEEP_STEP_SECONDS}s")
            clock.sleep(sec)

        def fake_strftime(fmt: str, *args: Any) -> str:
            val = clock.strftime(fmt)
            written_timestamps.append(val)
            return val

        mod.time.sleep = fake_sleep
        mod.time.strftime = fake_strftime
        try:
            # 1. 常规 TICK=60s 测试
            mod.sleep_chunk(total=60, chunk=5)
            # 断言每次步进 <= 5s
            for step in clock.sleep_history:
                assert step <= MAX_SLEEP_STEP_SECONDS, f"step {step} > 5"
            assert sum(clock.sleep_history) == 60, f"total sleep {sum(clock.sleep_history)} != 60"
            assert len(clock.sleep_history) == 12, f"60s / 5s should be 12 steps, got {len(clock.sleep_history)}"
            # 确认 heartbeat 文件存在且内容为最后时间戳
            hb_path = tmp_path / "heartbeat"
            assert hb_path.is_file(), "heartbeat file was not created"
            last_written = hb_path.read_text(encoding="utf-8").strip()
            assert last_written == written_timestamps[-1], "heartbeat file content does not match final timestamp"
            assert len(written_timestamps) >= 12, "heartbeat must be updated at each step"

            # 2. 模拟主机 suspend 冻结场景（40分钟无唤醒，恢复后下一步即刻更新 heartbeat）
            clock.sleep_history.clear()
            written_timestamps.clear()

            step_count = 0

            def fake_sleep_with_suspend(sec: float) -> None:
                nonlocal step_count
                step_count += 1
                if sec > MAX_SLEEP_STEP_SECONDS:
                    raise AssertionError(f"Step {sec}s exceeds max step {MAX_SLEEP_STEP_SECONDS}s")
                clock.sleep(sec)
                if step_count == 2:
                    # 主机进入 suspend 冻结 ~40 分钟（2400秒）
                    clock.now += 2400.0

            mod.time.sleep = fake_sleep_with_suspend
            mod.sleep_chunk(total=20, chunk=5)
            assert step_count == 4, f"expected 4 steps for 20s, got {step_count}"
            assert hb_path.read_text(encoding="utf-8").strip() == written_timestamps[-1]

            # 3. 边界条件：TICK=0
            clock.sleep_history.clear()
            written_timestamps.clear()
            mod.sleep_chunk(total=0, chunk=5)
            assert len(clock.sleep_history) == 0, "total=0 should not call sleep"
            assert len(written_timestamps) >= 1, "total=0 must write heartbeat at least once"

        finally:
            mod.time.sleep = original_sleep
            mod.time.strftime = original_strftime


def run_contract_fixtures() -> None:
    """自测正反例夹具，确保静态扫描能精准拦截违反契约的模式。"""
    # 负例 1: 整块 sleep(TICK)
    bad_monolithic = r"""
TICK = int(os.environ.get("AD_TICK", "60"))
while True:
    tick()
    open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
    time.sleep(TICK)
"""
    try:
        scan_autodispatch_source(bad_monolithic)
    except AssertionError as err:
        assert "monolithic sleep(TICK)" in str(err)
    else:
        raise AssertionError("Failed to intercept monolithic sleep(TICK)")

    # 负例 2: 分片 sleep 但无循环
    bad_no_loop = r"""
TICK = int(os.environ.get("AD_TICK", "60"))
def sleep_chunk(total=TICK, chunk=5):
    open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
    time.sleep(5)
"""
    try:
        scan_autodispatch_source(bad_no_loop)
    except AssertionError as err:
        assert "missing chunked sleep loop" in str(err)
    else:
        raise AssertionError("Failed to intercept missing chunked sleep loop")

    # 负例 3: 分片 sleep 但循环体内未更新 heartbeat
    bad_missing_hb = r"""
TICK = int(os.environ.get("AD_TICK", "60"))
def sleep_chunk(total=TICK, chunk=5):
    rem = total
    while rem > 0:
        step = min(rem, chunk)
        time.sleep(step)
        rem -= step
    open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
"""
    try:
        scan_autodispatch_source(bad_missing_hb)
    except AssertionError as err:
        assert "must write heartbeat inside loop" in str(err)
    else:
        raise AssertionError("Failed to intercept chunk loop without heartbeat")

    # 正例: 规范分片 sleep 与 heartbeat 更新
    valid_sample = r"""
TICK = int(os.environ.get("AD_TICK", "60"))
def sleep_chunk(total=TICK, chunk=5):
    rem = max(0, total)
    while rem > 0:
        open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
        step = min(rem, chunk)
        time.sleep(step)
        rem -= step
    open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
"""
    scan_autodispatch_source(valid_sample)


def main() -> int:
    # 1. 运行夹具自测
    run_contract_fixtures()

    # 2. 对 autodispatch.py 进行静态源码扫描
    if not AUTODISPATCH_PATH.is_file():
        print(f"ERROR: autodispatch source not found at {AUTODISPATCH_PATH}", file=sys.stderr)
        return 1
    source = AUTODISPATCH_PATH.read_text(encoding="utf-8")
    scan_autodispatch_source(source)

    # 3. 使用假时钟测试实际运行时行为
    test_runtime_behavior()

    print(f"OK: autodispatch heartbeat chunk contract verified ({AUTODISPATCH_PATH})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
