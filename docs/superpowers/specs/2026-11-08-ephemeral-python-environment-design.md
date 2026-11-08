# 缓存槽位占满时的 Python 临时执行

用户要求 Harness 自主安装依赖、构建、测试、诊断与修复，不能把内部可恢复资源问题交还用户。真实普通用户 run `11e7a4d9-bd1d-406b-949c-e5a8486a5958` 在模型前被 `python_project_environment_quota` 拒绝。实机缓存8/8槽位、820350737B/1GiB，磁盘准入通过；5个旧Hybrid的已部署严格终态证明均拒绝，另2个直接模式不确定项和最新未过期项也必须保留。

## 选择

已核查现有受控回收路径，不能通过降低证明标准释放这些环境。提高8槽/1GiB或降低1GiB磁盘保留不解决底层生命周期约束，也不在本次范围。采用当前执行独有的临时环境：正常缓存路径继续优先；仅在 Root 证明新缓存键被槽位容量阻塞、且原磁盘/源/产物/生产者预算仍可满足时，允许本次执行不写缓存。

这是 Main 在用户明确要求持续实施、不再询问的授权下作出的实现选择。临时执行不是已有能力；只有完整服务链与实机验证通过才算完成。

## 原则

- 缓存上限8、1GiB；环境生产者192MiB；磁盘保留1GiB；原source/product/CPU/memory/deadline/审批边界保持。
- 不能捕获所有 `cache_quota` 一律继续。新增精确的槽位容量拒绝类型；不确定、损坏、挂载类型异常、字节或磁盘不足继续拒绝。
- 新执行仍必须具备原先安装批准、可信解释器与精确wheel/recipe/input/source/protected合约。不能使用模型提供的主机路径、缓存键或资源数值授权。
- 默认持久缓存语义和已有 `PythonEnvironmentReference` 不变。临时分支绝不伪造可复用reference，不能被inspect/reuse/release当成缓存环境。
- 通过版本化opt-in协商新语义。旧客户端未opt-in时不得收到临时成功证明；响应不能混合持久reference与临时证明。
- Root独立核验导出环境的字节上限、解释器/recipe/input/environment/source摘要、原项目保护和测试报告；必须先证明unit+cgroup静默，再验证、移交产物和移除本次环境。清理UNKNOWN仍失败，不发行成功证明。
- 不删除或修改任何现有缓存、历史运行、用户源码、未知/保留证据。仅当前stage/export内的新环境可在精确核验后移除。
- 构建/测试失败的自然结果仍是失败报告；不得因临时模式把失败、取消、超时或未验证产物升级为成功。

## 接口

在 `python_project_protocol.py` 定义 `PythonEphemeralEnvironmentEvidence`。字段为 `schema_version=1`、`storage='ephemeral_current_execution'`、`scope: PythonProjectScope`、`execution_id`、`recipe_sha256`、`environment_input_sha256`、`environment_sha256`、`source_sha256`、`interpreter_sha256`、`environment_bytes`、`unit_quiet=True`、`environment_removed=True`、`cache_written=False`。标识与摘要使用现有严格类型；`require_for(invocation)` 核对完整作用域、执行与配方输入。它不包含可用作主机IO授权的路径。

`PythonProjectBrokerRequest.ephemeral_environment_version` 为可选 `Literal[1]`，默认None；`PythonProjectBrokerResponse.ephemeral_environment` 可选该证明。Root只能为opt-in且外层`ok=True`的run动作发行证明；外层失败、probe、inspect、release、维护动作均不能带该证明。它仅证明本次环境已核验并移除，不证明构建或测试成功：安装已通过、严格native报告为自然build/test失败且已证明静默和移除时仍返回该生命周期证据，报告保持failed；安装失败、取消、超时或UNKNOWN不发行。run响应reference与proof互斥。持久响应及旧请求边界保持。

`PythonProjectRunnerPlan.environment_storage` 为 `Literal['persistent_cache','ephemeral_current_execution']`，默认持久。临时值禁止同时提供reused environment，只由Root写入的只读plan选择。Runner使用现有私有安装/build/test及export机制，不能自行选择回退。公共runner报告的schema2/checks仍保持，临时证据走Root响应。

缓存提供精确槽位压力异常与只读锁定的临时执行上下文：在现有全局锁下重新确认catalog、全新键、安装权限、当前合法用量及磁盘。临时上下文不产生cache reservation/reference/entry。保持锁的现有生命周期语义，不引入新常驻进程。

在 `python_project_reports.py` 增加独立 `PythonEphemeralProjectReport`，不放松 `PythonProjectReport` 的强制reference。它用Root证明校验同一passed receipt，暴露正常report与明确临时证据；不能充当可复用缓存引用。

服务保留原preparation外层作用域，新增明确版本化的临时观察载荷，不改变持久观察的严格集合。后续候选验证若使用已核验临时观察，必须在同一已批准的精确recipe/input边界下重新构建当前执行环境，不能把过去的临时证明用作当前环境存在/复用证据。生成项目与原项目消费者都支持该变体，直连/派单/讨论/混合共享同一验证服务。

## 验证

先写失败测试，证明满8个受保护槽位的新执行应可选择临时路径，旧语义仍拒绝。覆盖scope/recipe/source/保护合约篡改、混合证明、越权opt-in、故障/超时/清理UNKNOWN、字节与磁盘拒绝、缓存不变、真实环境移除及产物留存。再验证原项目观察→修改→重新安装/build/test→同一ZIP/wheel/CLI交付链。

便携协议测试与严格双平台mypy在本地；目录FD、systemd/cgroup及真实安装测试在服务器隔离范围验证。组件测试不替代最终普通用户交付。既有已消费运行不重放，不自动追加付费试跑。
