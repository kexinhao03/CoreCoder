# ReliAgent P0 Development Roadmap

**Goal:** 在CoreCoder中逐步交付可恢复、可审计、可评测的工具型Agent Runtime，并以CI失败诊断与仓库维护Workflow完成端到端证明。

**Spec:** `ReliAgent-需求文档-v1.1.md`

## 承载决策

ReliAgent作为CoreCoder中的增量能力实现，而不是复制一份改名仓库：

- 运行时核心放在`corecoder/runtime/`；
- 自动评测放在`corecoder/evals/`；
- 仓库维护案例放在`corecoder/workflows/repo_maintenance/`；
- 现有`Agent`通过后续Adapter接入Runtime，第一阶段不直接改动Agent Loop；
- 不覆盖当前工作区中已有的`fetch_url`相关未提交改动。

## 开发与沟通节奏

每一阶段遵循同一节奏：

1. 阶段开始前说明目标、文件范围、关键设计和验收命令；
2. 每项行为先写测试并确认按预期失败；
3. 写最小实现使测试通过；
4. 阶段结束时运行新增测试、全量测试、`compileall`和ruff；
5. 展示关键代码入口、状态变化和测试证据；
6. 用户掌握并确认后再进入下一阶段。

## 阶段划分

### 阶段0：基线、隔离与计划

**目标：** 保护现有修改，确认测试环境，锁定模块边界与执行顺序。

**交付物：**

- 本Roadmap；
- 阶段1详细实施计划；
- worktree或明确的原地开发决策；
- 可运行的pytest/ruff环境；
- 基线测试报告。

**完成条件：** 用户确认工作区策略，基线测试结果已记录。

### 阶段1：运行时状态与SQLite存储

**目标：** 建立不依赖LLM的事实层，完成Run、ToolCall、Event的状态契约和事务化持久化。

**核心文件：**

- `corecoder/runtime/state.py`
- `corecoder/runtime/models.py`
- `corecoder/runtime/store.py`
- `tests/runtime/test_state.py`
- `tests/runtime/test_store.py`

**核心证明：**

- 非法状态转换被拒绝；
- 状态更新和Event写入在同一SQLite事务中；
- 同一Run的Event sequence严格递增；
- 重试创建新ToolCall并通过`retry_of`关联；
- 已终结的ToolCall不能回到`running`。

**详细计划：** `docs/superpowers/plans/2026-09-15-reliagent-phase1-runtime-foundation.md`

### 阶段2：审批、subprocess执行与恢复

**目标：** 建立工具执行边界，覆盖审批、进程组超时/取消、有限重试与中断恢复。

**预计核心文件：**

- `corecoder/runtime/policies.py`
- `corecoder/runtime/executor.py`
- `corecoder/runtime/recovery.py`
- `corecoder/runtime/approvals.py`
- `tests/runtime/test_policies.py`
- `tests/runtime/test_executor.py`
- `tests/runtime/test_recovery.py`

**核心证明：**

- 只读工具自动执行，高风险工具没有Approval不会启动；
- subprocess超时按进程组终止；
- Run取消后不启动新ToolCall；
- 启动扫描把遗留`running`调用标记为`interrupted`；
- 未知高风险副作用自动重放次数为0；
- 只有幂等且明确可重试的失败会创建下一attempt。

**阶段开始前动作：** 基于阶段1实际API编写独立详细计划，不提前猜测接口。

### 阶段3：Trace、指标与自动评测

**目标：** 把运行事实转成可查询Trace和可复现Baseline/Full报告。

**预计核心文件：**

- `corecoder/runtime/tracing.py`
- `corecoder/evals/models.py`
- `corecoder/evals/assertions.py`
- `corecoder/evals/runner.py`
- `corecoder/evals/report.py`
- `tests/evals/`

**核心证明：**

- JSON Trace按sequence稳定导出并脱敏；
- 汇总耗时、工具成功率、超时、重试、审批等待和成本；
- 8—10个确定性故障场景可以批量运行；
- Baseline与Full使用相同任务、工具、模型和Prompt；
- 报告同时给出恢复收益与正常路径开销。

**阶段开始前动作：** 基于阶段2实际事件Schema编写独立详细计划。

### 阶段4：仓库维护Workflow、CLI与求职交付

**目标：** 用固定CI失败fixture证明Runtime不是孤立组件，并完成可演示、可复查的交付物。

**预计核心文件：**

- `corecoder/workflows/repo_maintenance/`
- `corecoder/reliagent_cli.py`
- `tests/workflows/test_repo_maintenance.py`
- `evals/datasets/repo_maintenance/`
- `docs/reliagent/`
- `README.md`
- `README_CN.md`

**核心证明：**

- 在fixture副本中完成诊断、审批、修复和测试验证；
- 人为中断后能安全恢复；
- 已确认成功的调用不重放；
- 未知高风险调用停止并等待人工确认；
- 生成Markdown评测报告和3—5分钟演示脚本；
- 简历中的每个数字都能由仓库命令重新生成。

**阶段开始前动作：** 基于阶段3评测接口编写独立详细计划。

## 全局非目标

- P0不实现FastAPI、SSE或Web前端；
- P0不实现通用Step动态编排；
- P0不连接线上GitHub账号，不自动push或创建PR；
- P0不实现分布式Worker、消息队列或完整Event Sourcing；
- P0不宣称任意外部副作用的Exactly-once；
- P0不修改或清理与ReliAgent无关的现有代码。

## 最终验收命令

```bash
python -m pytest tests/ -v
python -m compileall -q corecoder tests
ruff check corecoder tests
python -m corecoder.reliagent_cli eval evals/datasets/repo_maintenance
```

最终一项命令在阶段4才存在；此前阶段只运行已经落地的验收入口。
