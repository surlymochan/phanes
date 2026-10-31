"""组织诊断示例的独立交叉审查角色。

`cross_check.py` 核对任务书（brief）、诊断快照（diagnosis）与站点产物（dist）三方一致性，
逐条输出 PASS/FAIL，任一 FAIL 时退出码非 0；
`test_cross_check.py` 用测试内构造的最小样本覆盖通过与各类失败，不依赖其他角色的脚本。
"""
