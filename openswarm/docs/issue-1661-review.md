# #1661 审查返工交付说明

- B1：同一 PR head 的 issue 审查标记增删后，原缓存可能继续保留过期状态。`openswarm/bin/openswarm_external.py:159` 将文档返工判定加入缓存条件，变化时重新计算并发布状态。`openswarm/tests/test_external_backend.py:134` 覆盖同一 SHA 下标记添加、无变化和移除，分别验证失败转成功、不重复发布及成功转失败。
- B2：按聚合行数判断会误把 `docs/` 中的代码或零行非文档文件当作纯文档。`openswarm/sdk/external-lane/external_lane/contract.py:178` 逐文件检查文档类型，只有全部文件符合条件才允许豁免。`openswarm/sdk/external-lane/tests/test_contract.py:63` 覆盖纯文档命中和标记不匹配；第 84 行覆盖代码、测试、其他文件及零行代码文件不命中；`openswarm/tests/test_external_backend.py:120` 覆盖混合 diff 和 `docs/example.py` 的失败状态。
