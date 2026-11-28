<!-- 离散检查点（期望的执行映射，非定义）：每行把一个 checkpoint 绑到本仓可复现证据。
新增 checkpoint 须同时给出可复现判定方式；禁止仅写"人工确认"。 -->

# Checkpoints — Omni_Re_Formatter

| checkpoint | 应发生的状态变化 | 判定方式（可复现证据） |
|---|---|---|
| CP-01 | 文档与派生事实一致 | `python scripts/doc_inventory.py --check` 退出码 0 |
| CP-02 | 期望映射层每个锚点都能在本仓 STANDARDS 解析（C7） | 同上；失败时会打印 `INDEX.md:<行>: anchor` 提示 |
| CP-03 | 单仓（无兄弟仓）测试仍绿 | `uv run pytest tests -m "not e2e"` → 0 failed |
| CP-04 | 场景库可枚举且引用可解析 | `ls scenarios/*.yaml \| wc -l` ≥ 1；每条 yaml 的 `standard:` 引用指向本仓锚点 |
| CP-05 | 回退（fake）模式不得计入质量证据 | 设 `OMNI_TEST_FAKE_*` 跑同一场景 → 判定不得为 `passed`（见 STANDARDS.md 的 E-01） |
