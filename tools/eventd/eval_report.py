#!/usr/bin/env python3
"""Render EVAL.md using only recorded evaluation data."""
import argparse
import json
import eventd
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRICES = {'gpt-6-luna': (0.10, 0.50), 'gpt-6.1-terra': (1.00, 5.00)}


def render(records, log=None):
    meta = next(r for r in records if r.get('kind') == 'log_summary')
    decisions = [r for r in records if 'case_id' in r]
    models = [('openai', 'gpt-6-luna', 'high'), ('openai', 'gpt-6.1-terra', 'medium'), ('decisions', 'gpt-6-luna', None)]
    metrics = {}
    case_ids = {r['case_id'] for r in decisions}
    synthetic = sum(i.startswith('synthetic-') for i in case_ids)
    real = sum(i.startswith('real-') for i in case_ids)
    lines = ['# eventd OpenAI 决策评测', '',
             '基线：`' + meta['baseline'] + '`。先固定案例及期望，再调用当前 `eventd.decide`；Responses 请求限制最多 2048 输出 token；按预算预留控制花费。',
             f'案例：手工构造 {synthetic} 条，脱敏真实事件 {real} 条。',
             '案例标签为人工判断；非唤醒的 defer 与 ignore 合并计为正确，漏唤醒和误唤醒均按该二分类统计。脱敏真实案例主要是空消息和进程退出，不能代表生产分布。', '',
             (f"当前规则真实日志：{len(log)} 条，{sum(eventd.classify(line, {}, {})[0] is None for line in log)} 条进入决策层（{100 * sum(eventd.classify(line, {}, {})[0] is None for line in log) / len(log):.2f}%）；空分组、无消费者覆盖。" if log else f"历史基线真实日志：{meta['total']} 条，{meta['decision']} 条进入决策层（{meta['decision'] / meta['total'] * 100:.2f}%）。当前规则的真实日志比例尚未测量；用 --log 指定日志重算。"), '',
             '| 模型 / effort | 有效 / 总数 | 唤醒准确率 | 精确动作 | 漏唤醒 | 误唤醒 | 延迟均值 / 中位数（ms） | 费用（美元） |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    total_cost = 0.0
    for backend, model, effort in models:
        rows = [r for r in decisions if r['requested_model'] == model and r.get('requested_backend', 'openai') == backend]
        valid = [r for r in rows if not r['reason'].startswith('decision unavailable:') and not any(a.get('error') for a in r['attempts']) and not r['decision_info']['fallback']]
        correct = sum((r['action'] == 'wake') == (r['expected'] == 'wake') for r in valid)
        metrics[backend, model] = (len(valid), correct, sum(r['expected'] == 'wake' and r['action'] != 'wake' for r in valid), {r['case_id'] for r in valid})
        exact = sum(r['action'] == r['expected'] for r in valid)
        missed = sum(r['expected'] == 'wake' and r['action'] != 'wake' for r in valid)
        false = sum(r['expected'] != 'wake' and r['action'] == 'wake' for r in valid)
        pos = sum(r['expected'] == 'wake' for r in valid)
        neg = len(valid) - pos
        pct = lambda n, d: f'{n}/{d} ({100*n/d:.2f}%)' if d else 'n/a'
        latency = f"{statistics.mean(r['elapsed_ms'] for r in valid):.0f} / {statistics.median(r['elapsed_ms'] for r in valid):.0f}" if valid else 'n/a'
        cost = 0.0
        for r in rows:
            for a in r['attempts']:
                u = a.get('usage')
                if u:
                    p = PRICES[a['model']]
                    cost += (u['input_tokens'] * p[0] + u['output_tokens'] * p[1]) / 1_000_000
        total_cost += cost
        lines.append(f'| {backend}: {model} / {effort or "—"} | {len(valid)} / {len(rows)} | {pct(correct,len(valid))} | {pct(exact,len(valid))} | {pct(missed,pos)} | {pct(false,neg)} | {latency} | ${cost:.6f} |')
    base = metrics['openai', 'gpt-6-luna']
    candidate = metrics['decisions', 'gpt-6-luna']
    eligible = candidate[0] > 0 and candidate[3] == base[3] and candidate[2] <= base[2] and candidate[1] / candidate[0] >= base[1] / base[0] - 0.05
    lines += ['', '默认后端决定：' + ('decisions 满足同一案例集上漏唤醒不增加且准确率下降不超过 5 个百分点的门槛。' if eligible else '保持 openai；decisions 未达到同一案例集上漏唤醒不增加且准确率下降不超过 5 个百分点的门槛，或证据不足。'), '比较使用未回退的有效判断；5 个百分点作为本次“不明显更差”的明确阈值。', '官方 [Decisions 指南](https://developers.openai.com/api/docs/guides/decisions) 规定仅支持 gpt-6-luna，choice 返回 answers[].choice；未向模型提供工具。评测前另有一次 HTTP 200 探测，119 输入 token，估计 $0.0000119，不计入表格。']
    unknown = sum(a.get('usage') is None for r in decisions for a in r['attempts'])
    reserved = total_cost + unknown * 0.02
    lines += ['', f'无 usage 的请求 {unknown} 次；按每次 $0.02 预留后的预算计数为 ${reserved:.6f}。', f'有 usage 的请求按未缓存输入计算的费用估计：${total_cost:.6f}；不包含无 usage 的请求，无法精确计费。',
              '单价来源：[OpenAI API 标准价格](https://openai.com/api/)（每百万输入 / 输出 token）：Luna $0.10 / $0.50；Terra $1.00 / $5.00。Decisions 按输入每百万 token $0.10、输出不收费；缓存输入按普通输入估算，未计潜在其他费用。',
              '响应和每次调用的 usage 见 `eval-raw.jsonl`；脚本只统计有效且未回退的模型判断，不将故障时的保守唤醒视为模型结果。运行 `python3 tools/eventd/eval_report.py` 重现本报告。', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    records = [json.loads(line) for line in (HERE / 'eval-raw.jsonl').read_text().splitlines()]
    print(render(records, args.log.read_text().splitlines() if args.log else None), end='')
