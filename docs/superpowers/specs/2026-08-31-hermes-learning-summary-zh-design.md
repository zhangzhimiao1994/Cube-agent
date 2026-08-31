# Hermes+ 对话记忆中文学习摘要设计

## 背景

Hermes+ 已经能把运行结果、对话经验和调度观察写入 Hermes learning 台账，但当前 `summary` 更偏系统内部说明，常见内容是英文模板或混合格式。用户在查看“对话记忆学习”时，无法快速判断这条记录到底学到了什么。

## 目标

为每条 Hermes+ 学习记录增加一个用户可读的中文一句话摘要，让用户在台账列表中直接理解“这条对话记忆学习了什么”。

## 非目标

- 不替换原始 `lesson` 字段；`lesson` 仍作为机器调度和匹配使用的经验内容。
- 不引入额外模型调用；本次使用确定性模板生成中文摘要，避免增加延迟、费用和失败点。
- 不改变 Hermes+ 的确认、删除、推荐策略。
- 不实现完整的记忆调取增强链路；该链路作为后续智能化改造方向记录在本文末尾。

## 推荐方案

新增 `learning_summary_zh` 字段。

- 后端 Hermes learning 响应模型增加 `learning_summary_zh: str`。
- Hermes 自动记录运行结果时同步写入中文摘要。
- 手动记录 Hermes 经验时也生成中文摘要。
- 旧数据没有该字段时，在读取 payload 时根据 `category/outcome/lesson/tags/weight` 生成 fallback。
- 前端 Hermes 学习台账增加“学习摘要”列，显示 `learning_summary_zh`。
- 详情页保留原始 summary、lesson、tags、weight，避免丢失机器可用信息。

## 中文摘要规则

摘要必须是一句中文，建议控制在 80 字以内。

生成原则：

- `category=conversation`：强调从对话中学到的可复用经验。
- `category=scheduler`：强调调度层学到的运行/失败/超时/容量观察。
- `outcome=success`：使用“成功经验”语义。
- `outcome=failure`：使用“失败教训”语义。
- `outcome=neutral`：使用“观察记录”语义。

示例：

- 对话成功经验：`本次学习到：需要辩论审查时，优先使用讨论模式并由主 Agent 汇总证据。`
- 调度失败观察：`本次学习到：reviewer 步骤超时时，应先压缩输入并重试，再考虑跳过。`
- 中性观察：`本次记录到：该类任务更依赖明确的对话 ID 和标签来复用经验。`

## 数据流

1. Hermes+ 产生学习记录。
2. 后端生成并存储 `learning_summary_zh`。
3. 管理 API 返回 Hermes learning 列表和详情。
4. 前端列表展示中文摘要栏。
5. 用户可在详情页查看完整 lesson 和系统 summary。

## 兼容性

旧 Hermes payload 不包含 `learning_summary_zh` 时，后端读取层必须补齐中文摘要，避免前端解析失败或显示空值。

## 测试计划

- 后端单元/集成测试：自动记录 Hermes outcome 时 payload 包含 `learning_summary_zh`。
- 后端 API 兼容测试：旧 payload 被读取时会生成中文摘要。
- 前端测试：Hermes 页面显示“学习摘要”列和中文摘要内容。

## 后续智能提升方向

Hermes+ 想让 Agent 更智能，不能只保存记忆，还需要记忆调取功能。推荐后续按四层做：

1. 存储层：保存结构化经验，包括任务类型、模式、角色、结果、标签、中文摘要和机器 lesson。
2. 取回层：运行前根据当前任务、对话 ID、项目、模式候选、角色池和标签取回相关 confirmed 经验。
3. 注入层：把少量高相关经验注入主 Agent 的调度上下文，而不是把所有历史塞进 prompt。
4. 反馈层：运行结束后根据成功、失败、超时和人工确认结果更新经验权重。

本次只实现第一层中的“用户可读摘要”，不会扩大到调度策略改造。
