# Paper2Run 真实闭环验证记录（2026-09-30）

## 2026-10-03 追加二：沙箱复现链路（输入论文 → 自动生成代码 → 沙箱执行 → 结果）

目标实现：「输入一篇论文，自建实验环境、复现并出结果」。新增 Recipe `sandbox_python/1` 全链路，遵守交接文档 §6 全部约束（不放宽任何门禁）：

**链路**：论文 → gpt-6-astra 抽取（候选 `sandbox_python/1 supported`）→ `POST /experiments/{eid}/codegen` 真实生成自包含标准库复现脚本（AST 白名单校验：仅 24 个安全标准库模块、禁 eval/exec/compile/__import__、写路径仅限当前目录字面量）→ 代码作为内容寻址 artifact 存档 + `AttemptRecord(source=model, code_manifest_json)` 供审核（GET 读回）→ 确认 spec（seed/max_seconds）→ EnvironmentCheck（新增 sandbox 能力探针，seatbelt 实测验证才判 pass）→ Run 创建门禁 `SANDBOX_CODE_REQUIRED`（无完整 model attempt 拒绝）→ worker 取内容寻址字节并双重校验 sha 后交付沙箱执行 → 真实 predictions/labels → 真实评估 → 报告 `evidence_class=sandboxed_model_code`（独立 limitations/evaluator/metrics_source，交叉配对拒绝）→ 导出内容寻址校验。

**沙箱隔离**（`packages/sandbox/p2r_sandbox/`，安全评审无高危）：子进程 `-I` + 独立进程组；env 白名单构造（PATH/TMPDIR，无 secret）；临时工作目录 finally 删除；rlimit CPU/NPROC/FSIZE（本机 RLIMIT_AS 不可用→RSS 监控+组击杀，stderr 注记非静默）；墙钟超时；增量输出封顶（防 stdout 炸内存）；收集路径 realpath 防符号链接逃逸；macOS seatbelt（deny network* + 限写 workdir，realpath 化；启动探针实测网络/越权写被拒才启用，否则 network_isolated=False 如实降级）；无 shell、字面量 argv。评审遗留两条 v2 改进备忘：RSS 仅监控组长进程（有 NPROC+墙钟兜底）；guest 可读全盘（可加敏感路径拒读）。

**真实 E2E（gpt-6-astra）**：k-NN 论文（加权 5-NN、seed 20260930、120/30）→ 抽取 89s（候选 supported、参数正确）→ 生成 57 行加权 k-NN 脚本（假设声明诚实：类别生成顺序解释、`exact_reference_reproduction_claimed: False`）→ 沙箱执行秒级完成 → **accuracy 0.9333（28/30），落在论文宣称的 0.80–0.95 区间** → 报告/导出 sha 校验一致。

**schema 与运维**：attempts 表扩展 experiment_id/run_id 可空 + 部分唯一索引（迁移 0017；既有 run 级语义零变化）；旧演示库需 `alembic stamp 0016 && upgrade head`；codegen 网关校准（timeout 240s、max_output_tokens 3000、prompt 限制 ~80 行——oproxy 的 CDN 在 ~126s 切长响应 524，op 网关另有间歇 5xx 需重试）。

**执行事故**：workflow 波次 2 因 GLM-5.3 五小时用量上限中断（15:00 重置），R 代理编辑中途被腰斩（runs.py docstring 开行丢失致语法错、LIMITATIONS 措辞被擅改）——由主 Agent 修复并完成剩余集成（runs 门禁/环境检查/报告三分支/两组新测试）。回归：`pytest -q` 601/0；前端 109/0 + build ✓；`git diff --check` 干净。


工作区：`.worktrees/phase0-phase1`（分支 `feat/paper2run-phase0-phase1`）
执行方式：四个 implementer 任务包（workflow，子代理模型 `account:bigmodel-individual-coding-plan/GLM-5.3`），主 Agent 亲自复核。

## 结论

交接文档（`docs/handoff/2026-09-30-paper2run-handoff.md`）的 P0/P1/P2 前四步全部完成：

1. **P1 测试绿化**：21 个旧 export/guided-export 失败全部修复。真实根因与交接文档描述略有不同——`EnvironmentCheckRecord(ready)` 前置插入在早前部分修复中已存在于 `test_export_artifacts_http.py`，剩余门禁是 candidate 缺 `execution_support="supported"`（422 UNSUPPORTED_RECIPE）。实际修复：给 seeded candidate 补 `execution_support/label` 一处 fixture 数据，未触碰产品代码与门禁。
2. **P0 真实 CPU Recipe `real_cpu_baseline/1`**：仓库内置固定数据集（`services/runner/data/real_cpu_baseline_v1.csv`，400 行，SHA-256 `607d8d8c…9854` 执行时校验；生成脚本 `tools/generate_real_cpu_baseline_dataset.py`，种子 20260930，重生成逐字节一致）；纯标准库逻辑回归（train 320 / test 80，确定性 split，seeded minibatch），实测默认参数测试集 accuracy **0.8875**（71/80）；Pydantic 冻结参数 schema（seed/max_epochs/learning_rate/batch_size，越界与未知键拒绝）；墙钟超时 `RecipeTimeout`；evidence_class `real_local_cpu`。
3. **真实链路打通**：`run_worker` 按 revision→experiment 的 recipe 分发（offline_fixture 行为不变）；参数仅取自确认 spec 的 training 四键、不做静默强转；`evaluate_predictions` 用真实 test 标签；`EvaluationRecord.evaluator_version=real-cpu-baseline-evaluator-v1`；报告快照按 recipe 标注 evidence_class 与 limitations（真实 recipe 用 `REAL_LIMITATIONS`、`metrics_source=real_local_cpu_evaluation`；fixture 输出与原先逐字节一致）；report_json 导出下载校验通过。
4. **P1 真实 Provider 链路**：默认 runtime 接入 `SessionExtractionService`（与 `ProviderSecretStore` 同实例）；`OpenAICompatibleProvider` 支持 `secret_resolver` 注入（session 内存 secret，不经环境变量）；session 未显式声明 capabilities 时默认 `text/structured_output=supported`；secret 仍只在进程内存，不入库/DTO/日志/artifact。
5. **P2 Guided UI 真实入口**：`RealFlowPanel`（文件/粘贴文本、Provider 表单、抽取三态、候选徽章、training 参数、EnvironmentCheck 详情、Run/Report/Export readback）；api_key 仅 React state，不写 localStorage；evidence_class 如实展示中英文说明。

## 主 Agent 复核后的补充修改

- `services/api/p2r_api/routes/runs.py`：local Run 的 manifest/target_json 不再统一钉 `local-fixture-v1`，按 recipe 映射（`real_cpu_baseline/1` → `local-cpu-real-v1`），消除任务包 B 报告的外观性失真。改动后全量回归仍 527/0。

## 验证命令（主 Agent 亲自复跑）

```text
PYTHONPATH=. .venv/bin/python -m pytest -q          → 527 passed, 0 failed（修复前 471/21）
npm test --prefix apps/web                           → tests 100 / pass 100 / fail 0（修复前 67）
npm run build --prefix apps/web                      → tsc 0 错误，vite build 成功
git diff --check                                     → 无输出
```

数据集确定性：脚本重生成与 CSV `cmp` 逐字节一致；同 seed 两次运行 predictions 与 metrics_hash 全等。

## 已知边界与未做

- 真实 SSH/远程执行、GPU、生产基础设施：按约束未实现、未授权。
- 真实 Provider 网络调用未在本轮验证（fake transport 测试覆盖协议/错误分类/schema 校验）。
- 真实 recipe 的 pinned target 与执行结果 runner id 现已一致；`run_fixture` 回退分支仍假定注册表内二选一（创建门禁保证）。
- 工作区有既有未提交改动与未跟踪目录（含 `.p2r-guided-verification/`、`.p2r-loopback-fix/`），本轮未清理、未 commit/push。

## 2026-10-03 追加：真实联网 Provider E2E（gpt-6-astra @ oproxy）

用户授权后用真实端点完成全链路：真实文章上传 → **gpt-6-astra 真实抽取**（69s，1532+1477 tokens）→ 候选 `reference_logistic_regression`（real_cpu_baseline/1，supported，参数 seed=7/epochs=60/lr=0.15/batch=32）→ revision/confirm → EnvironmentCheck ready → Run 真实执行 completed（accuracy 0.8875=71/80，两次 Run metrics_hash 逐字节一致，确定性复现 ✓）→ 报告 evidence_class=real_local_cpu → report_json 导出下载 SHA-256 校验一致。

为此修复的真实链路缺陷（全部有最小改动+测试）：

1. `extraction_service._SYSTEM_PROMPT`：注入注册 recipe 集合与「必须包含全部顶层键」指令（模型此前无从知道可执行边界）。
2. 抽取 user message 注入 `PROVENANCE` 块（paper_id + pdf_sha256）——外部模型此前不可能生成通过溯源校验的 source_refs（fake 测试用预计算 sha 掩盖了此缺陷）。相关 5 处测试断言更新为验证溯源头+原文后缀。
3. 新增 `_strict_wire_schema`：pydantic schema → 严格结构化输出兼容的 wire schema（Any→显式标量 anyOf、全字段 required、additionalProperties=false、$defs 全内联、开放 map（parameters/resource_requirements）→ 闭合定形对象、tuple array 补 items、**只在 schema 节点层剥离 title/default 元数据——properties 的键是数据不可剥**）。最后一项是关键 bug：最初版本把根属性 `title` 当元数据剥掉，导致模型永不返回 title。
4. `providers.urllib_transport` 发送 `User-Agent: paper2run/0.1`——默认 `Python-urllib/3.12` 被 oproxy 的 Cloudflare WAF 拦截（error 1010/403）。
5. 抽取 `timeout_seconds` 60→120（网关生成大 schema 响应常超 60s；上限 300）。
6. `reports._snapshot` 用 `paper.sha256`（内容寻址地址）而非路径形式的 `artifact_key` 读源文——此前只有 fixture 种子（sha 形键）能过，真实上传文章必失败。
7. `build_report` 公共 DTO 增加 `evidence_class`（前端 RealFlowPanel 依赖此字段展示证据等级；报告一经构建即冻结，旧报告不回填，新 Run 生成新报告）。

回归：`pytest -q` 527 passed / 0 failed（含更新后的断言）。演示服务：后端 8010（P2R_DEMO_DIR=.p2r-demo-live），前端 5173（P2R_API_TARGET=8010）。

## 执行事故记录（供复盘）

第一轮 Agent 工具派发的 4 个 implementer 实际运行在 `rcola/gpt-5.6-sol`（ZCode Subagents 设置覆盖，非主会话模型继承），被用户发现后全部停止并改用 workflow `subagent_model=GLM-5.3` 重派。其中任务 B 旧代理未被成功停止（TaskStop 返回矛盾状态），继续并发写入同批文件；经 escalate 确认后命令其停笔（已服从），其半成品由 GLM-5.3 工程师按「未信任外部贡献」审计接管：修复了被误删的 attempt 行、移除事务内强转、labels 条件传参等 5 处缺陷后落地。

## 2026-10-03 追加：前端改版——关闭离线教学入口，只留真实实操流

用户要求「把离线教学功能关闭 只留实操 然后优化前端提升设计感」。

**入口裁剪**：`GuidedPage.tsx` 重写为薄壳——品牌头部 + 导语 + `<RealFlowPanel api={api} />` + 页脚，`离线教学示例 / startExample / GuidedController` 模式页签全部移出产品面。离线 fixture（offline_fixture/1）保留为后端回归路径（pytest 仍 601/0），但不再有 UI 入口。

**设计系统**（`guided.css` 全量重写，暗色「科研仪器」风）：CSS 变量（--bg #0a0e14 / --surface / --accent teal #5eead4 / 证据色 --ok/--warn/--bad / --mono 等宽字）、径向渐辉背景、960px 版心、渐变菱形品牌标、stepper 表面轨、卡片 `animation: guided-enter 240ms`（保留测试契约，reduced-motion 下禁用）、徽章前圆点、候选卡 `:has(input:checked)` teal 描边环、代码暗色内嵌块。

**测试更新**（4 处源断言）：`guided-realflow.test.mjs` 末测反转为「产品面只有真实流：无离线教学入口」（doesNotMatch 离线教学/startExample/GuidedController/guided-mode-tabs；match `<RealFlowPanel api={api} />`）；`guided-readback.test.mjs` 3 处 wiring 断言从 GuidedPage.tsx 改指 RealFlowPanel.tsx 的现行模式（readRev/pollRun+pollReport/刷新 effect deps 去 epoch）。

**两轮视觉评审 + 修复**（截图 → 视觉模型评审 → 计算/像素级复核）：
1. 首评发现：file input 白底原生样式、stepper 未激活文字过暗且全局规则 `.guided-page button:disabled{opacity:.38}`（guided.css:126）与 stepper 规则同特异性靠后获胜压到 0.38、主按钮偏暗像禁用。
2. 修复：`color-scheme: dark` 到 `.guided-page`（复选框/滚动条原生控件暗色化）+ `::file-selector-button` 自定义暗色描边按钮（元素级截图像素复核确认融入主题）；--muted/--faint 提亮一档（#9aa7ba/#74829a）；stepper 未激活改 --muted 且 disabled opacity 提到 0.62（提升特异性 `.guided-page .guided-steps button:disabled` 压过全局规则，live 计算值核验 [1,0.62×5]）；主按钮改 `linear-gradient(180deg,#79f0dc,#4fd4bd)` + 投影。
3. 终版整页六项验收（品牌区/导航轨/控件暗色/CTA/页脚/无重叠缺陷）全部通过。

回归：`npm test --prefix apps/web` 109/109，`npm run build` ✓，`git diff --check` 干净。评审注记：整页截图评审曾误报 file input 仍为白底（元素级截图与计算样式证实已是暗色）——对评审结论保留「像素级复核后再修」的流程，避免盲改。
