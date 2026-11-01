# Paper2Run 后端总体设计

**状态：设计基线，待用户书面审阅**  
**日期：2026-09-25**  
**适用范围：首版后端、Worker、Python Kernel、Runner、独立评测与证据链**

## 1. 决策摘要

Paper2Run 采用**架构借鉴、独立实现**路线：只吸收 OpenAI4S 的 `Science as Sessions`、`Code as Action`、持久 Kernel、append-only Action Ledger、版本化 Artifact、Environment Provenance、Checkpoint、恢复与分支等设计原则，不直接引入 OpenAI4S 运行时代码、依赖或 Git 子模块。

首版只实现 Python 持久 Kernel。Kernel 可以在受控权限下提交正式实验意图，但不能直接启动训练进程、访问 GPU、修改已确认实验协议或决定复现结论。正式训练、预测和独立评测必须经过 Paper2Run Control Plane、独立 Runner 和 Evaluator。

后端采用：

- 模块化单体 FastAPI Control Plane；
- 独立 Python Worker；
- 独立 Python Kernel 进程/沙箱；
- 独立 Linux Runner；
- 独立 Evaluator；
- PostgreSQL + SQLAlchemy 2 + Alembic；
- 本地 Artifact Store 抽象，后续可替换对象存储；
- 首版不强制 Redis、Celery、Kubernetes、向量数据库；
- 浏览器公共 API 继续遵循 `docs/engineering/09-前后端公共接口契约.md` v0.2。

## 2. 产品与可信边界

Paper2Run 是证据驱动的论文实验复现工作台，不是任意论文或任意代码自动执行平台。首版优先支持 CIFAR-10 图像分类，部署目标为邀请制、单团队、多项目、单 GPU 执行槽。

实验模式与运行模式必须分离：

- 实验模式：`smoke`、`scaled`、`reference`；
- 运行模式：`mock-ui`、`local-cpu`、`remote-gpu`、`replay`。

`run.status=completed` 只说明调度链路到达终态；`verdict=matched` 必须由独立 Evaluator 在协议可比、证据完整和固定容差规则成立后产生。

### 2.1 三个信任区

**不可信输入区**包括论文、模型输出、生成代码、用户上传文件、日志、外部文件、Kernel 输出和动态配置。它们必须先经过 schema、白名单、权限和确定性规则校验。

**受控执行区**包括 Kernel、Worker、Runner、数据处理和代码预检。它们受到环境白名单、文件系统隔离、网络策略、超时、资源预算、非 root、Action 审计和取消回收约束。

**可信判定区**包括已确认 Revision、维护者审核的 Dataset/Split/Recipe/Evaluator、RunManifest、验证过 hash、独立评测输出和审计关联。只有可信判定区可以产生最终 `verdict`。

## 3. 系统分层与部署拓扑

```text
Browser
  │ /api/v1
  ▼
FastAPI Control Plane
  ├── Auth / Project / Paper / Revision / Session API
  ├── Permission / Budget / Idempotency / Audit
  ├── SSE event projection
  └── Application Services
       │
       ├── PostgreSQL
       ├── ArtifactStore
       ├── ModelGateway
       └── durable Jobs
              │
              ▼
           Worker
           ├── Session/Action orchestration
           ├── Kernel supervisor
           ├── Checkpoint/recovery
           ├── Runner dispatch
           └── Evaluator dispatch
                │
        ┌───────┴────────┐
        ▼                ▼
  Python Kernel       Independent Runner
  session sandbox     offline non-root container
                           │
                           ▼
                      Independent Evaluator
```

### 3.1 组件职责

| 组件 | 负责 | 明确不负责 |
|---|---|---|
| Browser | 输入、展示、上传、订阅、报告查看 | 调模型、访问 DB/对象存储管理、执行代码、决定 verdict |
| FastAPI | 公共 API、权限、Revision、Session、Run、预算、幂等、审计 | 在 API 进程执行用户/模型代码、直接操作 Docker |
| Worker | 持久任务、状态转移、租约、恢复、调度和对账 | 修改已确认科研协议、绕过 Runner |
| Kernel Supervisor | 启动/停止/重建 Python Kernel，限制能力和资源 | 直接拥有 GPU、任意进程、任意网络、直接写可信状态 |
| Python Kernel | Cell 分析、数据探索、代码预检、提交 ExperimentIntent | 直接训练、修改 evaluator/labels/tolerance、宣布匹配 |
| Runner | 校验 descriptor、执行 recipe、回收日志/预测 | 获取模型 Key、任意 shell、访问 Docker socket |
| Evaluator | 验证预测完整性、加载固定标签、计算 VerifiedMetrics/verdict | 读取生成指标、加载训练代码、接受 stdout 数字 |
| PostgreSQL | 事实状态、Jobs、Lease、Ledger 索引、预算 | 代替 ArtifactStore 存大文件或保存 secret |
| ArtifactStore | 不可变内容和 hash | 直接向浏览器开放路径 |
| ModelGateway | Provider、限流、用量、脱敏、重试 | 修改业务事实或执行实验 |

### 3.2 API 边界

- 浏览器只访问 `/api/v1`；
- Runner、Kernel、Worker 内部接口使用 `/internal/v1` 或本地受控 RPC；
- 浏览器不得访问 Runner、Kernel、数据库、Artifact 管理接口和模型凭据；
- API 进程不执行生成代码；
- 所有内部调用携带 service identity、request id、operation id 和权限上下文；
- Runner 接收签名且已审核的 `ExecutionDescriptor`，不接受任意 argv、任意镜像或用户传入的环境变量。

## 4. 领域模型

### 4.1 可信研究对象

`Project` 是团队和数据隔离边界。`PaperSpecRevision` 保存论文事实、来源定位、数据集、指标和 baseline 候选；只有 `confirmed` Revision 可以被正式 ExperimentSpec 引用。

`ExperimentSpecRevision` 是正式执行协议，包含：

- `paper_spec_revision_id`；
- `experiment_mode` 和 `run_mode`；
- dataset/split/label manifest；
- recipe/version/image digest；
- entrypoint；
- target 和 metric 定义；
- tolerance；
- resource budget；
- evaluator ref；
- content hash、确认人和确认时间。

Revision 只能追加新版本，禁止原地修改。确认后不允许通过 Session、模型或 Kernel 修改目标、标签、评测器、容差、数据 split 和资源上限。

### 4.2 会话执行对象

`Session` 是 Project 下持续的科学工作容器，拥有持久 Python Kernel、Action、Cell、Artifact 和 Checkpoint。一个 Session 可以创建多个 Run；一个 Run 必须绑定一个已确认 ExperimentSpecRevision。

`Action` 是 append-only 科学动作账本，最少记录：actor、session、revision、run、cell、输入/输出 Artifact、environment、代码 hash、权限决定、预算消耗、状态和错误。

`CellExecution` 是 Kernel 的基本执行单元，必须保存完整源码、输入 namespace hash、stdout/stderr、输出 Artifact、Environment 和执行状态。

`Artifact` 是不可变内容对象，带 URI、媒体类型、大小、SHA-256、producer action、父 Artifact、schema version、trust class 和 provenance。

`Environment` 记录 runtime 类型、镜像 digest、Python 版本、依赖 lock hash、git commit、硬件和系统摘要。它提供 provenance，但不单独保证可复现。

`Checkpoint` 不是单纯的内存转储，必须包含 action cursor、namespace snapshot、输入 Artifact、Environment、replay plan 和恢复验证状态。

### 4.3 ExperimentIntent

Kernel 只能提交 `ExperimentIntent`，至少包含：

- session 和 source action；
- base ExperimentSpecRevision；
- requested changes；
- entrypoint ref；
- requested resources/mode；
- input Artifact；
- 生成来源和代码 hash。

Control Plane 对 Intent 执行 schema、Revision、dataset/split、recipe、entrypoint、budget、permission、idempotency 校验。校验通过后进入确认门；确认后由服务端生成不可变 RunManifest。

## 5. 状态机

### 5.1 Session

```text
active → paused → recovering → recovered
                           ├→ partial → recovering
                           └→ failed
active → closed
```

`closed` 不可继续执行。`partial` 必须展示缺失项。`failed` 可以从新分支继续，但不能伪装成原 Session 完整恢复。

### 5.2 Action

```text
queued → validating → awaiting_permission → running → succeeded
                                                   ├→ failed
                                                   ├→ cancelled
                                                   └→ timed_out
```

只读动作可以自动批准，但仍必须写入 `permission_decision=auto_approved`。

### 5.3 Run

```text
created → queued → provisioning → running → collecting → evaluating → completed
                  ├→ cancelling → cancelled
                  ├→ failed
                  └→ timed_out
```

Run 的 `status` 与 `verdict` 分开：`matched`、`not_matched`、`inconclusive`、`unavailable` 只能来自 Evaluator 或确定性可比性检查。

### 5.4 Recovery

恢复过程必须逐项验证：

1. checkpoint manifest 存在且 hash 正确；
2. 输入 Artifact 可读取且未被替换；
3. Environment 可用或有明确 replay fallback；
4. namespace snapshot 可恢复或可重放；
5. action cursor 和 ledger 连续；
6. 恢复后的 Kernel 能通过健康检查。

全部满足为 `recovered`；仅历史和 Artifact 可用但 Kernel 需重建为 `partial`；无法确认输入或状态一致性为 `failed`。

## 6. 持久 Python Kernel 设计

### 6.1 使用边界

首版 Kernel 允许：

- 论文结构化内容读取；
- 已授权 Artifact 读取；
- 数据质量检查和探索；
- 受控可视化；
- 代码生成和静态预检；
- 构造并提交 ExperimentIntent；
- 解释已有 Run/Evaluation。

首版 Kernel 禁止：

- 直接调用 `subprocess`、Docker、GPU 或任意网络；
- 读取模型凭据、数据库密码和宿主机私有路径；
- 修改维护者 recipe、evaluator、labels、split、tolerance；
- 安装任意依赖；
- 直接创建正式 Run；
- 读取受保护标签以推导答案；
- 自行声明 `matched`。

### 6.2 Cell 执行协议

```text
submit_cell(session_id, source, base_checkpoint_id, idempotency_key)
  → schema/size/capability validation
  → permission and budget decision
  → create Action + CellExecution
  → execute in Kernel
  → capture stdout/stderr/result refs
  → register Artifacts
  → update checkpoint cursor
  → emit events
```

Cell 结果必须结构化返回，不能只依赖 stdout。大输出转为 Artifact，小结果可保存为受限 JSON 摘要。输出内容均标记 trust class。

### 6.3 受控发起实验

Kernel 中的 `submit_experiment_intent()` 只创建提案 Action。服务端必须重新读取数据库中的 confirmed Revision，而不信任 Kernel 传回的完整配置。任何不允许字段变化都返回明确的 rejection code。

## 7. Worker、Runner 与 Evaluator 数据流

### 7.1 Worker 任务模型

首版使用 PostgreSQL durable jobs：

- `jobs(id, kind, payload_ref, status, attempt, available_at, lease_until, idempotency_key)`；
- Worker 以短事务领取任务；
- 业务状态和 job outbox 同事务写入；
- Worker 心跳续租；
- 过期租约由 reaper 重新入队；
- 重试次数、退避和不可重试错误由任务类型策略决定。

Worker 不持有长数据库锁，不把大型代码或日志直接塞进 job payload，只传 Artifact/Run/Action 引用。

### 7.2 正式 Run 流程

```text
ExperimentIntent
  → protocol validation
  → user/system confirmation
  → immutable RunManifest
  → queued job
  → GPU lease
  → Runner provisioning
  → descriptor signature verification
  → offline recipe container
  → train/predict
  → output manifest + prediction hash
  → independent evaluator
  → VerifiedMetrics + verdict
  → report/export artifacts
```

Runner 必须执行：非 root、固定 image digest、固定 recipe、只读输入、独立输出目录、CPU/GPU/内存/磁盘/时间限制、默认断网、超时清理和过程回收。

### 7.3 Evaluator 输入

Evaluator 只读取：

- confirmed ExperimentSpecRevision；
- 固定 dataset/split/label manifest；
- Runner 产生且 hash 验证通过的 prediction；
- 固定 evaluator 版本；
- 必要的 execution metadata。

Evaluator 不读取训练 stdout 作为指标，不加载生成训练代码，不接受生成的 `metrics.json` 作为可信结果。

## 8. 数据库与事务设计

核心表建议包括：

```text
users, invites, teams, memberships, projects
papers, paper_spec_revisions, evidence_refs
experiment_spec_revisions, experiment_confirmations
sessions, session_branches, actions, cell_executions
artifacts, artifact_links, environments, checkpoints
experiment_intents, runs, run_manifests, attempts
jobs, leases, budget_ledgers, outbox_events
verified_metrics, evaluations, reports, exports
audit_events, idempotency_records
```

原则：

- 领域状态使用 PostgreSQL；大文件使用 ArtifactStore；
- 状态迁移通过 application service，不在 API handler 中散落；
- append-only 表禁止 update/delete，纠正通过新事件或新版本；
- 所有外部副作用使用 outbox；
- 状态更新使用 optimistic version 或 `SELECT ... FOR UPDATE` 的短事务；
- Artifact 注册与 producer Action 关联在同一事务中；
- 删除采用软删除/保留策略，不能破坏已引用证据；
- Secret 只来自服务端环境或 secret manager，不进入数据库业务 JSON、Prompt、日志和 Artifact。

## 9. API 设计

公共 API 继续以 `docs/engineering/09-前后端公共接口契约.md` v0.2 为唯一浏览器契约来源，不在本设计中重新定义同名 DTO。需补充的 Session/Action/Kernel/Checkpoint 端点如下，具体字段在实现时加入 contracts 并生成 OpenAPI/TypeScript：

```text
POST   /projects/{project_id}/sessions
GET    /projects/{project_id}/sessions
GET    /sessions/{session_id}
POST   /sessions/{session_id}/pause
POST   /sessions/{session_id}/resume
POST   /sessions/{session_id}/branches
GET    /sessions/{session_id}/actions
POST   /sessions/{session_id}/cells
GET    /sessions/{session_id}/cells/{cell_id}
POST   /sessions/{session_id}/checkpoints
GET    /sessions/{session_id}/checkpoints
POST   /sessions/{session_id}/restore
POST   /sessions/{session_id}/experiment-intents
GET    /experiment-intents/{intent_id}
POST   /experiment-intents/{intent_id}/confirm
GET    /sessions/{session_id}/events
```

写操作必须使用 `Idempotency-Key` 和 CSRF；资源修改继续使用 ETag/If-Match；SSE 事件使用有界 cursor，cursor 过期时客户端重新取快照。

内部 API 建议：

```text
/internal/v1/kernel/sessions/{id}/execute
/internal/v1/kernel/sessions/{id}/health
/internal/v1/kernel/sessions/{id}/checkpoint
/internal/v1/runner/executions
/internal/v1/runner/executions/{id}/cancel
/internal/v1/evaluator/evaluations
```

内部接口必须带 service authentication，且不暴露到浏览器网关。

## 10. 幂等、预算、权限与审计

副作用请求统一使用 `Idempotency-Key`。同 key + 同 body 返回原结果；同 key + 不同 body 返回冲突；处理中返回 `IDEMPOTENCY_IN_PROGRESS`。

预算至少分为 Session、Action、Kernel、Run 四层，包含 wall time、CPU、GPU、内存、磁盘、Artifact 数量/大小、模型 token/费用和网络额度。预算只能由 Control Plane 配置并注入，Kernel、Runner 和模型不能扩大预算。

权限策略分为：

- 项目角色：owner/editor/viewer；
- Action capability：read_artifact、execute_cell、submit_intent、confirm_revision、start_run、read_evaluation；
- 运行模式能力：local-cpu、remote-gpu、replay；
- 资源策略：单 GPU lease、队列上限、用户配额；
- 危险操作：任意代码执行、外部网络、数据导出、正式 reference run 必须额外审批或系统策略允许。

每个 Action、模型调用、Runner attempt、Evaluator execution 和状态转移都写入 audit。审计包含 actor、request、revision、code/environment/artifact hash、permission decision、预算消耗和错误分类。

## 11. OpenAI4S 能力采用表

### 采用

- Science as Sessions：Session 作为持续科学工作容器；
- Code as Action：完整 Cell 作为科学动作单元；
- append-only Action Ledger：所有科学动作可追溯；
- 版本化 Artifact 与 lineage；
- Environment provenance；
- Workspace Checkpoint、恢复和分支；
- structured result / host RPC 思路；
- recipe/skill 作为受维护能力包，而不是动态任意插件。

### 改造后采用

- 持久 Kernel：必须加 Paper2Run capability、资源、网络、文件系统和实验协议边界；
- Session recovery：必须区分 `recovered`、`partial`、`failed`；
- Action Timeline：除模型和工具事件外，必须关联 PaperSpec/ExperimentSpec/Run/Evaluator；
- Artifact：增加 SHA-256、trust class、固定 evaluator、prediction 和标签 manifest provenance；
- Host RPC：只暴露 typed domain commands，不暴露任意主机命令。

### 明确不采用

- 直接把 OpenAI4S Runtime 当作生产 Runner；
- 允许 Kernel 自由执行正式训练和评测；
- 允许模型修改目标、标签、评测器、容差或资源预算；
- 让 stdout、模型回复或生成 metrics 文件成为可信结果；
- 动态安装任意 Skill、依赖或外部代码；
- 依赖 OpenAI4S 的环境规格来宣称完美可重运行；
- 首版引入 Python/R 双 Kernel、BYOC、多集群、向量数据库或 Kubernetes。

## 12. 分阶段实施路线

### Phase 0：契约与工程骨架

内容：Python 3.11/3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL、contracts、配置、日志、request id、健康检查、Docker Compose local-cpu。

验收：API 可启动；迁移可重复执行；OpenAPI 与公共契约差异有报告；无真实执行假成功。

### Phase 1：Session + Action Ledger + Python Kernel MVP

内容：Session、Cell、Action、Artifact metadata、Environment、Checkpoint manifest、Python Kernel supervisor、权限和预算最小闭环、SSE。

验收：提交 Cell 后可恢复完整源码、stdout/stderr、结果 Artifact、Action 顺序和环境 hash；进程杀死后可以得到 `recovered/partial/failed` 的正确判定；越权 Cell 被拒绝。

### Phase 2：Experiment Revision + ExperimentIntent

内容：PaperSpec/ExperimentSpec Revision、确认门、Intent 校验、幂等、差异显示、从 Kernel 提交候选。

验收：已确认 Revision 不可变；Intent 不能修改受保护字段；同 key 幂等；确认记录可追溯；合法 Intent 能生成 RunManifest。

### Phase 3：Worker durable jobs + local-cpu Runner

内容：jobs、lease、outbox、reaper、取消/超时、受控 CIFAR-10 smoke recipe、local-cpu Runner。

验收：真实训练、预测和 Artifact 回收成功；Worker 重启不丢任务；重复消息不重复创建 Run；超时可清理；运行模式明确为 local-cpu。

### Phase 4：独立 Evaluator 与 verdict

内容：固定标签 manifest、预测完整性、独立指标计算、VerifiedMetrics、可比性规则、report JSON。

验收：生成代码伪造指标无效；缺预测、重复预测、错误标签和 hash 不匹配均被拒绝；completed 与 matched 分离。

### Phase 5：remote-gpu 单槽与恢复

内容：Linux Runner、GPU lease、固定镜像 digest、非 root、默认断网、资源限制、远程日志、恢复和对账。

验收：至少一次真实 remote-gpu CIFAR-10 smoke；租约、取消、断连、Worker 重启和 Runner 重启均有可审计终态。

### Phase 6：受控代码生成与有限修复

内容：ModelGateway、proposal schema、代码文件清单、静态检查、有限 patch、repair budget、每次 patch 单独 Action/Artifact。

验收：模型不能写入受保护目录和协议字段；每次修复可复盘；超出 repair budget 停止；凭据不进入 Prompt、代码或日志。

### Phase 7：报告、导出与 replay

内容：确定性报告、Artifact bundle、replay 索引、只读演示、前后端联调收口。

验收：报告可从 DB/Artifact 重建；历史 replay 不创建新 Run；报告明确模式、差异、证据缺口和 verdict。

## 13. 测试与验收矩阵

必须覆盖：

- domain unit：状态机、Revision 不可变、容差和可比性；
- contract：Pydantic/OpenAPI/TypeScript 一致性；
- persistence：迁移、并发、唯一约束、幂等；
- kernel security：路径穿越、任意 import、subprocess、网络、资源超限；
- runner security：descriptor 签名、镜像 digest、非 root、默认断网、清理；
- evaluator adversarial：伪造指标、缺预测、重复预测、标签 hash 错误；
- worker failure injection：崩溃、重复消息、过期 lease、网络中断、Runner 无响应；
- e2e：CIFAR-10 smoke 从确认到 report；
- recovery：Session 和 Run 分别验证完整、部分和失败恢复；
- audit：每个可信结论可追溯到 revision、action、artifact、environment 和 evaluator。

完成标准不是“接口返回 200”，而是：

1. 证据对象真实存在且 hash 可验证；
2. 状态迁移符合状态机；
3. 错误和重试不产生重复副作用；
4. 不可信输出无法改变可信判定；
5. 失败、部分恢复和不可比结果被诚实展示；
6. 真实 local-cpu/remote-gpu 记录与 replay 记录明确区分。

## 14. 首个实现任务包

首个写码任务只实现 Phase 0 + Phase 1 的最小闭环：

- 创建 `services/api`、`services/worker`、`services/kernel`、`packages/contracts`、`packages/persistence`、`packages/artifacts` 的最小骨架；
- 建立 Session、Action、CellExecution、Artifact、Environment、Checkpoint 表和迁移；
- 实现 `POST cell`、Action 查询、Checkpoint 创建/查询、Session 恢复和 SSE；
- 实现 Python Kernel supervisor 和受限 Cell executor；
- 实现本地 ArtifactStore；
- 实现预算、权限、幂等和审计最小版本；
- 不接入真实模型、不接入 remote GPU、不执行正式训练、不写前端。

该任务包完成后，必须先通过安全、恢复、幂等和证据链测试，再进入 ExperimentIntent 和 Runner。
