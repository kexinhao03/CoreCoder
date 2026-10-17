# 可恢复真实 Agent Loop 集成设计

日期：2026-10-14。状态：架构设计待确认；加密保存与本机密钥策略已获用户确认。
基线：功能分支 `codex/runtime-coding-chain`，提交 `4fed390`。

## 1. 目标和现状

复用 CoreCoder 的 LLM 客户端、Agent 推理循环和工具，不开发第二套 Agent。
把 Runtime 从四个工具的执行适配层扩展为所有工具调用和推理续接的持久化边界。

当前 `Agent.chat` 仅把模型响应放入内存，RuntimeToolAdapter 生成新的执行 ID，
与模型 tool_call_id 没有关联；session.py 的手工 JSON 保存不是执行事务。
因此，现有恢复扫描能分类工具孤儿，却无法证明恢复后不会重新生成危险调用。

本设计同时完成：
- 全工具统一准入、审批、执行、异常分类和结果提交。
- 保存用户输入、完整模型响应、原始调用 ID、调用顺序、完整有界结果及有效上下文。
- 原始恢复数据加密；现有审计副本继续脱敏。
- 先消费已有调用，再决定是否允许下一轮推理；不靠重新问模型修复未知副作用。
- CLI 显式恢复原 Run，不把旧会话 JSON 误当成持久化执行记录。

## 2. 方案选择

采用“现有循环 + 持久化推理日志 + Runtime 统一分派”。

仅给所有工具套适配器，仍会丢失模型消息和未执行调用，不满足要求。
重新编写工作流引擎会重复 CoreCoder，并扩大维护面，不采用。

持久化边界属于 Runtime，Agent 只负责调用这些边界和维护内存中的上下文副本。
SQLite 是持久事实来源；内存列表不是恢复依据。

## 3. 身份与数据模型

新增以下与现有 Run/Step/ToolCall/Approval/Event 关联的 SQLite 表。

### agent_sessions

- run_id 主键，关联既有 Run；schema_version。
- phase：ready / llm_inflight / dispatching / blocked / complete。
- next_turn_sequence、revision、checkpoint_generation。
- 加密的 effective_context：messages、完整系统提示词、模型配置、plan_mode、todo 状态、bash cwd、工具与策略指纹。
- owner_token。只有当前进程持有的 session owner 能提交推进；同机每个 Run 使用操作系统文件锁保证只有一个执行者。

### agent_turns

- 主键 (run_id, sequence)，request_id 为本地生成且稳定的标识。
- request_ciphertext：本轮实际传给 LLM 的完整 messages 和工具 schema。
- response_ciphertext：收到的完整 assistant message 与 token usage。
- status：prepared / requesting / responded / complete / outcome_unknown。
- 本地序号与 request_id 独立于供应商响应是否提供 request ID。

### agent_invocations

- 本地 invocation_id 主键，唯一约束 (run_id, turn_sequence, provider_call_id)。
- provider_call_id 原样保存；ordinal 保存模型同批调用顺序。
- tool_name、参数摘要哈希、arguments_ciphertext、工具/策略指纹。
- state：queued / bound / completed / blocked。
- current_attempt_id 关联既有 ToolCall，result_ciphertext 保存完整有界模型可见结果。
- 同一模型调用可对应多个显式重试 attempt，provider_call_id 不变；不同轮可以重复 provider ID，不能跨轮误命中。
- 重复恢复同一 invocation 不会再次创建 attempt。相同身份但名称/参数不一致直接拒绝。

工具 pending 批次先保存在 agent_invocations，不提前创建多个活动 ToolCall。
分派当前调用时，在事务内创建 Step/首个 ToolCall 并绑定 invocation，保持一个 Run
同时一个活动 ToolCall 的既有约束。

## 4. 加密与密钥

用户已选择加密保存，密钥由本机提供；密钥缺失时阻止恢复。

- 使用 cryptography 的 Fernet 认证加密，不自行实现密码算法。
- `RELIAGENT_CHECKPOINT_KEY` 提供 URL-safe base64 编码的随机 32 字节密钥。
- 默认可恢复 CLI 在首次调用模型之前验证密钥；没有密钥不能悄悄退化成明文或声称可恢复。
  既有 `--no-runtime` 保留为用户明确选择的非持久化模式。
- 密钥不写 SQLite、审计事件、错误、报告、PR 或历史会话文件；不自动生成并放在数据库旁边。
- 模型 API key 不进入 checkpoint；只保存恢复所需非凭据模型配置。
- 恢复时检查密钥、认证标签、payload 中的 run/turn/invocation 身份和 schema version。
  错密钥、密文损坏、跨记录移植和缺失 checkpoint 均在任何工具/LLM 调用前阻断。
- 为避免把恢复密钥传给 shell/MCP 子进程，启动时读取后从默认子进程环境中排除该变量。
- 结果保存工具实际返回给模型的完整有界字符串，不用 2000 字符审计摘要替代。
- 审计导出不会导出密文原文或原始上下文，只提供脱敏摘要与身份/状态。
- Fernet 保护内容和完整性，不隐藏记录数量、数据库结构或 token 时间戳；密钥泄露不在保护范围内。
- 新增 cryptography 运行依赖；实现时选择支持 Python 3.10–3.13 的稳定发行版并在 CI 安装验证。

参考：https://cryptography.io/en/latest/fernet/

## 5. 正常执行提交顺序

1. 保存用户输入及有效上下文，提交 ready checkpoint。
2. 在事务内记录本轮请求、request_id，设置 llm_inflight，然后调用既有 LLM.chat。
3. 完整模型响应返回后，在一个事务中保存 assistant message 和整批 invocation，设置 dispatching。
   此事务失败时不得启动任何工具；不把流式半成品当作可执行调用。
4. 按 ordinal 逐个取 queued invocation；事务内绑定 Step/ToolCall attempt。
5. 根据工具风险策略创建/解析 Approval；审批绑定具体 invocation、attempt 和参数摘要。
6. 通过既有状态转换门禁提交 tool.started 后才允许真正执行。
7. 完成后在同一个 SQLite 事务中提交 ToolCall 终态、Step 结果、审计 Event、
   invocation 加密完整结果和对应 tool message。输出使用原 provider_call_id。
8. 整批结果齐备后提交新的有效上下文、状态快照和 turn.complete，才允许下一轮 LLM。
9. 没有工具调用的最终 assistant message 也先落盘，再向调用方确认完成。

不能在工具成功记录、完整结果和模型消息之间留下独立提交的成功窗口。
扩展 Store 现有事务内部逻辑；不使用“先写一个文件再写数据库”的双写方案。

## 6. 恢复与禁止危险再生成

`corecoder --resume-run <run_id> --runtime-workspace <workspace>` 明确恢复原 Run。
不把 `-r` 加载的任意 JSON 对话自动当作可恢复 Run。

恢复前先获取同机 Run 文件锁、验证 checkpoint/key/schema/model/tool/policy 指纹，
再基于 SQLite 中的状态重建内存上下文。锁争用时拒绝第二个执行者；进程退出由 OS 释放锁。
跨主机共享 SQLite 并发执行不在本轮支持范围。

| 已落盘事实 | 恢复动作 | 是否允许调用 LLM |
| --- | --- | --- |
| 完整 assistant 批次已保存，当前调用未启动 | 复用 invocation/provider ID；按原顺序继续审批与分派 | 否 |
| 调用 completed 且结果存在 | 从加密结果补齐同一个 tool message，不执行工具 | 整批齐备后允许 |
| 工具成功但完整结果缺失（旧记录/损坏） | 阻断，不能用摘要伪造原结果 | 否 |
| 已提交 started、没有确定终态 | 标记 interrupted/blocked，人工核对副作用 | 否 |
| 审批尚未决定 | 恢复同一审批请求，不重新生成工具调用 | 否 |
| 已批准但未 started | 原 attempt、参数和策略完全一致时可消费一次审批；加密参数不改变旧 audit replayability | 否 |
| 已确定拒绝、失败或超时并且结果齐备 | 返回既有真实结果，模型可正常决定下一步 | 是 |
| LLM 请求发出但完整响应未提交 | llm outcome_unknown；需显式授权重新请求，不能假装供应商 exactly-once | 否，默认阻断 |
| 密钥缺失/错误、内容损坏、身份或指纹不一致 | 显示具体阻断原因 | 否 |
| cancelled Run | 可查看历史，不继续原 Run | 否 |
| ready checkpoint，无未完成调用 | 从保存的有效上下文继续 | 是 |

有未知调用时，新用户输入也不能绕过门禁触发新 LLM 请求。
不能通过伪造 `[interrupted]` 的 tool result 来让模型重新决定未知副作用。

人工核对提供三类显式决议：
- confirmed_completed：提交经人工确认的完整模型可见结果及证据引用，不再执行。
- confirmed_not_executed：记录核对证据，旧 attempt 终态收敛后为原 invocation 创建新 attempt，重新审批。
- unresolved：保留阻断。

不可直接把 interrupted 改为 succeeded；使用专用事务核对 API。
只读调用也不在恢复时静默重新执行；需要重试时使用原 invocation 的新 attempt，
保留 retry_of 并满足现有幂等性、策略、次数与失败类型条件。

## 7. 全工具统一入口

Agent 在 Runtime 模式下把原始 ToolCall 交给统一 dispatch，包含未知工具名与无效参数。
策略拒绝、计划模式阻断、参数错误、未知工具均生成持久化的模型可见结果，不能绕过日志。
工具本体保留输入校验和业务逻辑；审批、尝试次数、恢复和异常分类由 Runtime 控制。

- read_file/glob/grep：只读策略，记录结果和身份；不预设跨崩溃结果不变。
- write_file/edit_file：变更策略，单 attempt 审批，未知结果要求核对。
- bash：受管理子进程，持久化 argv/cwd/timeout；命令不自动重试。
- todo_write：Runtime 管理的本地状态更新，可自动准入；完成事务同时保存新的 todo 快照。
  不把它错误标成具有外部幂等保证的读操作；内部状态更新使用单独的策略类别。
- agent：父调用经过风险准入；创建独立 child Run，绑定父 invocation/attempt，详见下一节。
- MCP：默认外部副作用策略、需审批、无自动重试；连接超时或断开不能证明远程未执行，
  因而归入未知结果并阻断，不能作为普通失败回填后让模型自动重试。
- fetch_url：若已注册，同样经过 dispatch；它不在当前开发工作区的八个内建工具中，
  本轮不从主目录拷入额外功能。

## 8. 子 Agent、上下文与 hooks

### 子 Agent

包装器不能依赖 isinstance 检测来给原 AgentTool 绑定父 Agent。
显式注入 child factory，并在 child Run 中初始化独立的 Runtime/session/tool 实例和锁。
父/子关联先持久化再调用 child；恢复时查找原 child Run，不重新派生第二个子 Agent。
子 Agent 仍不持有 agent 工具，保留现有禁止递归派生规则。
父调用仅在 child 最终输出可恢复并持久化后完成；child blocked 时父也阻断。
父取消关闭子 Run 新调用准入，再通知正在执行的受管理工作；不能强杀进程内 callable。
父子 todo 明确定义为各自独立状态，避免两个 checkpoint 覆盖同一内存任务表。

### 有效上下文

checkpoint 保存最近一次实际传给模型的有效 messages，以及原始 append-only 对话记录。
压缩只改工作上下文，不删除审计链中的原始调用与结果。
压缩的模型调用也记录为 LLM 请求；未收齐工具结果时禁止任何压缩模型请求。
plan_mode、todo、bash cwd、模型/系统提示词/schema/策略版本均纳入恢复指纹。
`/reset` 只允许在没有 pending/unknown 工作时开启新会话段；不能清掉未完成执行事实。
Runtime 模式的 `/save` 导出只含 Run 引用和脱敏元数据，不把解密历史复制到旧明文 session JSON。
`/undo` 具有文件副作用，必须作为明确的 Runtime 操作准入；不保留绕过门禁的直接写文件路径。

### Hooks

模型调用意图在执行任何 hook 前已落盘。可能执行 shell 的 hook 视为外部副作用，
在主调用审批后执行并记录独立的子操作身份和结果；hook 崩溃时不自动重放。
Post hook 的失败不撤销已完成工具结果，也不导致工具重执行；未确认 hook 结果仍阻断下一轮推理。

## 9. 文件边界

- corecoder/agent.py：增加可选 Runtime 会话入口；复用现有 LLM 客户端和循环。
- corecoder/agent_runtime.py：创建/加载会话、统一分派、resume/reconcile 门禁和 child factory。
- corecoder/runtime/agent_store.py：推理日志/调用映射/加密 checkpoint 事务；与现有 Store 共用连接和事务。
- corecoder/runtime/checkpoints.py：认证加解密、身份 envelope、key 验证，不实现自定义密码算法。
- corecoder/runtime/store.py、executor.py：支持 invocation 绑定、已保存参数的受控恢复和结果原子提交。
- corecoder/runtime/policies.py：加入内部状态动作策略；MCP 不确定错误分类。
- corecoder/runtime/tool_adapter.py：保留既有库兼容入口，统一 dispatch 不再丢失原 ToolCall 身份。
- corecoder/tools/agent.py、todo.py、mcp.py、hooks.py：显式注入运行上下文与错误语义。
- corecoder/cli.py：resume/status/reconcile 与 key 配置；原始 Agent 输入仍走同一个 Agent.chat。
- tests/runtime/、tests/test_agent_*、tests/integration/：事务、崩溃、真实循环和 opt-in 真实供应商验收。

## 10. 验收矩阵

1. 正式 CLI + 真实 LLM 选择读/写/编辑/测试工具，审批回调与实际执行发生在 Runtime 内。
2. 八个内建工具及模拟/本地 MCP 均产生 invocation/attempt/结果映射；无工具旁路。
3. assistant 批次事务回滚后工具执行次数为零。
4. 响应持久化后、执行前 os._exit：重启复用 provider ID 与原参数，不调用 LLM。
5. 外部效果后、结果事务前 os._exit：文件/marker 已改变，恢复阻断；LLM 和工具新增调用次数均为零。
6. 结果事务已提交、内存回填前 os._exit：从缓存补齐完整结果，效果计数不增长，然后允许下一轮 LLM。
7. 两个待调用中第一个未知：第二个和新 LLM 请求都不能开始。
8. 同一 Run 两个恢复进程：只有一个取得执行权；失去所有权者不能提交或执行。
9. 重复 provider ID 的跨轮调用不串结果；同轮重复 ID、参数身份冲突被拒绝。
10. 审批拒绝后保存结果；旧审批不能授权修改参数或新 attempt。
11. 错密钥、缺密钥、密文损坏、跨 Run 密文替换均零 LLM/工具调用；SQLite dump 无已注入原文。
12. 子 Agent 崩溃：恢复原 child Run，父/子无死锁、不重复派生或执行副作用。
13. 上下文压缩、todo/cwd/plan 恢复后与落盘时一致；未完成工具不触发摘要模型调用。
14. MCP 请求已送出后断开：按未知外部效果阻断，不能自动重试。
15. Windows/Linux/macOS × Python 3.10–3.13 回归、Ruff、build/twine；真实供应商测试独立 opt-in。

当前环境没有模型 API key，也没有项目 .env。真实供应商验收需要用户在本机配置凭据，
不得用 scripted 模型结果替代“真实模型验收通过”。不要求用户在聊天中粘贴任何密钥。

## 11. 非目标与交付次序

不宣称通用 exactly-once、跨主机分布式锁、任意线程强杀、Windows 完整进程树终止，
不引入第二个业务 Workflow、Web UI、模型自动预算或全量事件溯源框架。

实施顺序：加密日志与事务身份 → 现有 Loop 的提交边界和 resume gates →
全工具/子 Agent/MCP/hook 接入 → CLI 人工核对 → 故障注入与真实供应商验收 → CI。
每一阶段先有失败测试，再实现；已有编程链路 Windows 引号错误单独修复，不混入本设计提交。

## Trace 数据来源与 Step 审计（2026-10-17 修订）

Trace 由 Event Journal 和持久化状态表共同构成。Event Journal 提供关键生命周期审计历史，状态表提供当前执行状态和实体关联信息。Agent Journal 与 Checkpoint 负责保存 Agent 恢复所需的数据。ReliAgent 不承诺仅通过事件回放重建全部执行状态。

Agent Step 的创建、开始、审批等待/恢复、成功、失败、取消，按既有 Step 状态转换在同一 SQLite 事务中写入对应事件。工具未知结果保留未终结的 Step，由 ToolCall 的 interrupted 与 Run 的 recoverable 表达恢复门禁。对账成功记录的是对账当时发生的 Step 完成转换，不回填崩溃前的完成事件；历史审计缺口保持可见。
