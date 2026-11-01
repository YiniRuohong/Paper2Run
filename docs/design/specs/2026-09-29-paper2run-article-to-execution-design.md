# Paper2Run 文章到实验执行闭环设计

日期：2026-09-29  
状态：已获用户确认，待用户审阅规格后进入实施计划。  
工作区：`.worktrees/phase0-phase1`；分支：`feat/paper2run-phase0-phase1`。

## 1. 目标与边界

Paper2Run 首版提供一条简洁的单列六步向导：用户提供一篇文章、Provider 请求地址、模型标识和 API Key，系统完成文章结构化分析、实验方案生成、本机环境研判，并在条件满足时经用户确认执行受支持实验。本机条件不足时系统停止，不自动连接远程环境；用户可以主动选择 SSH，验证远程环境后再确认执行。

首版不承诺任意论文自动真实复现。没有受支持 Recipe、缺少数据集/依赖/GPU/网络条件或无法确认关键环境时，只能输出分析、实验方案、要求和修复建议，不创建虚假 Run，不伪造论文指标。报告必须区分 `analysis_only`、`environment_blocked`、`execution_failed` 和真实执行结果。

工作约束：不把 Provider secret 写入浏览器持久化、数据库、日志、SSE、Artifact、报告或导出；不执行模型生成的任意 shell/argv；写操作使用幂等键；未知结果先查 receipt，再以原 key/body 显式重放；Artifact 内容寻址且不可变；不安装新依赖，不联网部署，不调用真实 GPU 或外部 Runner，除非后续实施阶段明确提供受控配置和受支持 Recipe。保留现有无关修改。

## 2. 已确认的产品方向

- UI 使用白/近白背景、黑色主操作、灰阶文本、细边框和小圆角，采用单列向导而非多面板控制台。
- 每一步只展示当前必要输入和一个主操作，完成后以轻量动画过渡；动画不代表业务成功。
- 刷新和服务重启以后以服务端资源状态恢复步骤，不根据浏览器最后一步或列表最后一条记录猜测状态。
- 下载落盘不作为本次入口链路前置条件，已有导出能力保持不变。
- 第一阶段优先复用现有 Paper、Provider、Extraction、Run、Report、Export 和幂等基础设施，不创建第二套平行工作流。

## 3. 总体架构与数据流

```text
文章输入
  ↓
Paper / PaperVersion
  ↓
Provider 结构化抽取
  ↓
Extraction + SourceRefs
  ↓
实验方案与资源要求
  ↓
Environment Check
  ├─ 本机满足 → 用户确认 → Local Runner
  ├─ 本机不足 → 展示缺失项
  │              ├─ 用户选择 SSH → 连接验证 → Remote Runner
  │              └─ 只保存方案
  └─ 论文超出支持范围 → 只保存分析与方案
  ↓
Run / RunEvents
  ↓
Report
  ↓
Export
```

核心资源：

- `Paper`：原始文章及不可变版本。
- `Extraction`：Provider 返回且通过 schema 校验的结构化理解结果。
- `SourceRef`：关键字段对应的页码、段落或原文引用片段；无法验证定位时不推测页码。
- `ExperimentSpec`：候选实验、参数、依赖、数据集、预计资源、支持级别和 Recipe 引用。
- `EnvironmentCheck`：检查器版本、检查时间、检查项、检测值、要求值、结果和修复建议。
- `ExecutionTarget`：`local` 或用户主动选择的 `ssh`。
- `Run` / `RunEvent`：实际执行尝试及持久化状态。
- `Report`：引用实际 Run、版本、证据和限制的不可变结果视图。
- `Export` / `Artifact`：按报告版本和格式生成的可校验交付物。

本次保留现有六步主线，并将职责调整为：

1. **Paper**：上传 PDF/TXT、粘贴文章文本，或使用明确标注的教学示例。
2. **Provider**：填写请求地址、模型标识和 API Key。
3. **Extraction**：调用 Provider，展示结构化字段和来源证据，用户审阅后继续。
4. **Experiment**：展示候选实验、预算和支持级别，用户确认配置。
5. **Environment / Run**：执行本机环境研判；Ready 时确认本机执行，Blocked 时选择 SSH 或保存方案，Unsupported 时只保存方案。
6. **Report / Export**：查看分析或实验结果、限制和证据，按现有导出能力生成文件。

## 4. Provider 与 secret 安全

### 4.1 浏览器可见配置

浏览器可以提交 Provider 配置名称/标识、模型标识、请求地址、loopback HTTP 显式开关、超时和最大输出限制。浏览器不得把 API Key 写入 `localStorage`、IndexedDB、普通业务数据库、URL、SSE、Artifact、报告或导出包。

### 4.2 Provider session

Provider 配置接口与 Paper/Extraction 业务接口分离。推荐的边界为：

```text
POST /provider-sessions
POST /provider-sessions/{id}/test
POST /projects/{project_id}/papers/.../extract
```

浏览器仅持有不可逆的 `provider_session_id` 和安全投影：

```json
{
  "provider_session_id": "ps_...",
  "endpoint_alias": "my-provider",
  "model_id": "my-model",
  "secret_configured": true,
  "secret_last4": "••••"
}
```

`secret_last4` 只用于界面提示，不作为认证依据。刷新后不能恢复明文 Key；运行时 secret 不存在时要求重新输入。

### 4.3 请求地址和响应安全

复用现有 `ProviderConfig` 规则：默认只允许 HTTPS；HTTP 仅允许 `localhost`、`127.0.0.1` 或 `::1`，且必须显式开启；禁止 credentials、query、fragment、空白、控制字符和反斜杠；禁止跟随重定向；限制请求超时、响应大小和输出 token。Provider 响应必须符合 OpenAI-compatible 结构，结构化输出还必须通过调用方 JSON Schema 校验。

错误只返回安全错误码，例如 `PROVIDER_AUTH_INVALID`、`PROVIDER_ENDPOINT_REJECTED`、`PROVIDER_TIMEOUT`、`PROVIDER_INVALID_RESPONSE` 和 `PROVIDER_SCHEMA_MISMATCH`。不记录 Authorization header、secret 或包含 secret 的响应文本。失败不自动重试；未知写结果先查询 receipt。

Extraction 只保存 Provider 配置标识、模型标识、prompt version、context hash、调用状态和校验后的结果，不保存 secret。关闭本地服务或显式清除时删除运行时 secret。

## 5. 结构化抽取与实验方案

Provider 负责理解文章，不负责决定是否可以执行。返回值必须通过版本化 JSON Schema，至少包含：任务、数据集、方法、关键超参数、公式/假设、候选实验、资源要求、风险、限制和 `source_refs`。缺失字段保持缺失状态，不由系统补写。

每个候选实验必须包含：

```text
candidate_id
recipe_id / recipe_version
support_status: supported | review_required | unsupported
parameters
required_dependencies
required_datasets
resource_requirements
estimated_runtime
source_refs
```

模型不可直接提供任意 shell 命令供 Runner 执行。只有后端注册且参数通过白名单校验的 Recipe 才可能进入执行阶段：

```text
ExperimentSpec → registered recipe → validated parameters
```

`review_required` 和 `unsupported` 仍可以形成方案和修复建议，但不能创建表示真实实验的 Run。

## 6. 本机环境研判

环境研判器只负责检查和解释，不自动修复，不自动连接远程机器。每个候选实验产生一份可追溯 `EnvironmentCheck`，检查：

- 操作系统与架构；
- Python 版本及必需依赖版本；
- CPU 核心数；
- GPU 型号、显存和 CUDA 状态；
- 可用内存和磁盘；
- 数据集路径可读性、存在性和 SHA-256；
- 网络要求；
- 预计执行时间和资源等级；
- 检查器版本与检查时间。

每个检查项使用 `pass`、`fail`、`unknown` 或 `not_needed`。`unknown` 不得当作 `pass`。决策规则：

- **Ready**：Recipe 为 `supported`，所有必需项为 `pass`。用户点击确认后才创建 Run。
- **Blocked**：存在 `fail` 或关键项 `unknown`。展示检测值、要求值、缺失原因和建议；默认不创建 Run。
- **Unsupported**：没有注册的受支持 Recipe。只允许保存分析、方案和要求。

## 7. SSH 手动分流

SSH 是用户主动选择的执行目标，不是自动 fallback。选择后进入单独配置与验证流程：主机/IP、端口（默认 22）、用户名、认证方式、本机已有 SSH agent 或受控的本地私钥引用、远程工作目录和是否允许远程 GPU。随后执行远程环境自检，只有连接与自检都成功后，用户才能再次确认执行。

私钥正文、密码和 agent secret 不上传到普通业务 API、不写数据库、不进日志或报告。服务端只接收受控的本地密钥引用或 SSH agent 标识。连接前展示目标主机、端口、用户名和认证方式；首次连接必须点击“验证连接”。验证失败不创建 Run。首版不接受模型生成的 SSH 命令。

## 8. Run、Report 与真实性

本机和 SSH 统一进入现有 Run / RunEvents / Report 体系。Runner 只接受注册 Recipe、经过校验的参数、已通过的 EnvironmentCheck 和明确的 `ExecutionTarget`，不接受浏览器任意 shell、argv 或任意工作目录。

Run 至少登记执行目标、Recipe 版本、环境检查快照、命令安全投影、脱敏 stdout/stderr 摘要、产物 Artifact 的 SHA-256/bytes/MIME、状态和失败原因。结果以持久化状态为准；取消不等于进程已终止；失败或取消不自动创建新 Run。

只有存在实际成功 Run 和完整证据时才能生成实验结果报告。没有实际执行时，报告必须明确为 `analysis_only` 或 `environment_blocked`；执行中止后为 `execution_failed`。固定 smoke 或教学指标不得冒充论文性能。

所有写请求携带 Idempotency-Key。未知结果先查 receipt；显式重放使用原 key 和原请求体。刷新只执行 GET 读回，不触发 Provider 调用、环境检查写入、Run 创建或报告构建副作用。

## 9. 前端交互与恢复

向导使用服务端事实状态驱动步骤，状态包括 `idle`、`working`、`complete`、`blocked`、`unsupported`、`failed` 和写入未知。每一步只有一个主操作，必要输入逐步出现；切换使用约 220–300ms 的淡入和轻微位移，并支持键盘焦点、`aria-live` 和 `prefers-reduced-motion`。

成功流程示意：

```text
Paper → Provider → Extraction → Experiment
  → Checking environment…
  → Ready → Confirm local run → Report
```

阻断流程示意：

```text
Checking environment…
  → Blocked → missing items
      → SSH setup and verify
      → confirm remote run
      或 Save plan
```

页面刷新或服务重启时根据已保存的项目/资源 ID 查询 Paper、Extraction、Experiment、EnvironmentCheck、Run 和 Report，校验资源关联后恢复。只保存非敏感资源引用和操作标识，不保存 secret；不根据列表最后一项猜测当前资源。

## 10. 错误处理与测试验收

必须覆盖：

- PDF/TXT/粘贴文本入口校验与原文 hash；
- Provider 地址校验、secret 不落盘、认证/超时/限流/无效 schema；
- 结构化抽取缺字段、来源引用和 Provider 响应污染；
- Recipe 支持级别与任意命令拒绝；
- 环境 pass/fail/unknown/not_needed 及 Ready/Blocked/Unsupported 决策；
- SSH 验证成功、失败、目标展示和不创建 Run；
- 本机与 SSH 的显式确认、重复点击、未知写结果和 receipt 重放；
- 失败/取消 Run、事件去重、迟到响应和刷新恢复；
- 无 Run 时不得生成实验结果报告；
- 报告引用 Run、revision、Recipe、指标来源、限制和证据；
- 既有 JSON/Markdown/ZIP 导出完整性与 secret 排除；
- 单列向导的桌面、窄屏、键盘焦点、reduced motion 和状态过渡。

验证命令沿用仓库现有工具链：前端 Node 测试、TypeScript/Vite build、后端 `.venv/bin/pytest`（`PYTHONPATH=.`）和 `git diff --check`。实施阶段不使用真实外部模型、真实 GPU 或外部 Runner；Provider/Runner 使用受控 fake transport、fake environment probe 和 fake SSH executor 测试。未执行的浏览器下载落盘验收不阻断本阶段。

## 11. 范围外事项

以下不属于本设计的首版承诺：任意论文自动复现、自动安装依赖、自动寻找或下载数据集、自动 SSH fallback、模型生成任意代码直接执行、AutoDL/Colab 适配、生产多租户鉴权、完整 sandbox/reaper、真实 GPU 性能结论、PDF 渲染导出和未经用户配置的外部 Provider 调用。

## 12. 规格自审结论

- 没有保留 `TODO`、`TBD` 或未决产品选择；执行边界、Provider 安全边界和 SSH 分流均已确定。
- 数据流、六步 UI、Runner 白名单、EnvironmentCheck 和 Report 类型互相一致；没有把分析结果误写成执行结果。
- 方案聚焦于现有六步闭环的文章入口扩展，未引入平行控制台或无关重构。
- `unknown`、`review_required`、`unsupported`、`environment_blocked` 和 `execution_failed` 的处理已分别定义，避免由 UI 文案推断成功。
- 设计文档不修改运行时代码；用户审阅批准后，下一步才编写实施计划。
