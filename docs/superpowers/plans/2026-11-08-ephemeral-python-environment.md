# Ephemeral Python Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在受保护缓存槽位满时，仍能在原隔离与资源边界内真实安装、构建、测试并交付。

**Architecture:** Root识别精确槽位压力，运行私有临时环境并签发一次执行证据；持久缓存和reference合同保持。服务显式消费临时证据，后续验证重新安装，所有模式共享服务路径。

**Tech Stack:** Python/Pydantic、现有systemd broker、只读plan、原生目录FD与SQLAlchemy服务。

**Spec:** `docs/superpowers/specs/2026-11-08-ephemeral-python-environment-design.md`

## Global Constraints

- 缓存8/1GiB、producer192MiB、reserve1GiB及原source/product/CPU/memory/deadline保持。
- 不删除旧缓存/用户源码/UNKNOWN；只清理本次已证明静默的临时环境。
- 不伪造可复用reference；不放松持久passed DTO；临时成功须显式opt-in与完整Root证明。
- 凭据只RAM；只用当前任务的子Agent，不新建聊天。原16 staged报告保持不变。
- 不把组件通过记作产品验收；不重放已消费scope或自动追加付费试跑。

## Review Focus

- 同时槽位满和磁盘不足必须拒绝，由Root预算判定，不靠模型参数。
- 成功报告配上异作用域/异执行/已被移除证明必须拒绝。
- 临时原始观察不能允许随后候选复用不存在的环境。
- stage清理/静默不确定必须保留fence，不能发行passed证明。
- 旧持久客户端与所有模式继续使用原语义，不因新增字段得到隐含安装权。

### Task 1: Root协议、精确压力与临时验证

**Owner:** 子Agent实现，Main独立审查。

**Files:** `skills/sandbox/python_project_protocol.py`、`python_project_phase.py`（如plan在此）、`python_project_cache.py`、`python_project_backend.py`、`python_project_runner.py`、`python_project_copy.py`（如需提取精确移除原语）；测试放 `tests/unit/skills/sandbox/test_python_ephemeral_environment_nov08.py` 及按职责拆分的同主题测试。

**Interfaces:** 产生规范中的 `PythonEphemeralEnvironmentEvidence`、request opt-in、response互斥证明和Root plan storage。持久路径接口不改；临时环境只在Root精确full-slots gate下运行。

- [ ] 先为opt-in/old request、混合证明、跨scope/recipe/source、精确槽位与资源拒绝写失败测试并记录RED。
- [ ] 实现严格协议类型与`require_for`，保持其他动作互斥校验。
- [ ] 拆分精确槽位拒绝，加入不写cache的锁定临时上下文；其他quota/unsafe/UNKNOWN不得降级。
- [ ] 用原producer/report/保护检查执行并Root核验导出，确认静默及本次环境移除后返回临时证明；失败路径保持原语义。
- [ ] 运行改动范围测试、ruff、strict Linux/win32类型检查。Main审查权限、删除与原预算后再提交；不自行部署/网络/付费。

### Task 2: Sandbox客户端与通过报告

**Owner:** Main；Task1协议稳定后实施，不并发编辑Task1文件。

**Files:** `skills/sandbox/python_project.py` 或现有SystemdSandbox所在模块、`capabilities/python_project_reports.py`、`tests/unit/capabilities/test_python_ephemeral_reports_nov08.py`。

**Interfaces:** 增加独立 `PythonEphemeralProjectReport`，保留原 `PythonProjectReport`；客户端只在支持版run请求opt-in，并验证Root证明再构造该DTO。

- [ ] 先写非法缺reference持久报告仍拒绝、合法Root临时报告显式接受、混合/篡改证据拒绝的RED测试。
- [ ] 实现独立DTO及解析分派；不让普通报告/模型文本构造Root证明，不更改公共schema2 checks语义。
- [ ] 核对旧客户端/无opt-in不会收到临时成功；完成本层测试与类型检查并独审。

### Task 3: 原项目与生成项目贯通

**Owner:** Main实施、子Agent独立审查。

**Files:** `capabilities/python_project_service.py`、`python_generated_service.py`、`python_project_context.py`（按实际上下文职责）、相应原始观察/报告校验模块及 `tests/unit/capabilities/test_python_ephemeral_project_flow_nov08.py`。

**Interfaces:** 明确版本化临时观察载荷；持久观察严格集合不变。临时观察后每次候选验证取得本执行证明，不复用过去reference。

- [ ] 写原始观察→候选验证至少两次独立执行的RED测试；拒绝伪造旧证明、recipe变化与缺安装授权。
- [ ] 贯通原项目、生成项目及所有模式共享消费者；保留所有保护文件/原始合约/当前源摘要和失败判定。
- [ ] 核验终态维护不会对临时证明发起cache release；正常产物转移、自然语言报告与ZIP/wheel绑定不变。
- [ ] 执行本切片完整测试及原持久回归，不重复已通过且无影响的其他套件。

### Task 4: 实机与发布闭环

- [ ] 独立审查全切片（不可把同一实现者自审当独审），核对没有删除旧cache或提高任何原预算。
- [ ] 在服务器隔离范围使用8受保护READY缓存模拟真实压力，实际安装/build/test、验证环境移除和同一产物；保留失败/UNKNOWN边界。此步不调用模型。
- [ ] 核验源/依赖/完整资源map一致后部署，实机健康与普通用户界面验证；不重放11e/eac。
- [ ] 原用户新scope的最终构建与交付按既有授权/预算准备，真实交付后运行同ZIP/wheel/CLI/说明验收；未通过则继续根因修复，不能用native通过替代。
- [ ] 推送GitHub并检查实际CI直到通过或真实外部阻塞；更新简洁HANDOFF。
