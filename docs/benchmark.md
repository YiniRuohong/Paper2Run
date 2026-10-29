# Paper2Run 论文复现基准套件（tools/benchmark）

「论文 → 沙箱复现」评测基准：8 篇迷你论文任务，每篇经过
**AST 校验 → 沙箱执行 → 指标计算 → 区间命中** 四段流水线，产出每一段
的成功率数字。**离线模式不需要联网、不需要 API Key**；在线模式留好
Provider 接口，由用户接入真实模型后度量「抽取 → 代码生成 → AST →
沙箱 → 区间命中」五段成功率。

## 如何跑

```bash
# 离线全套（默认 8 篇）
PYTHONPATH=. .venv/bin/python tools/benchmark/run_benchmark.py --mode offline \
    --out results.json --md report.md

# 离线子集
PYTHONPATH=. .venv/bin/python tools/benchmark/run_benchmark.py --mode offline \
    --papers 01-weighted-knn,05-kmeans-purity --md report.md

# 在线模式（需要先实现 Provider，见下文）
PYTHONPATH=. .venv/bin/python tools/benchmark/run_benchmark.py --mode online \
    --provider-module my_pkg.my_provider --out results.json --md report.md
```

退出码：`0` 全部通过；`1` 有任务失败；`2` 用法/配置错误（未知
mode/paper、未配置 Provider 等）。

## 任务一览（当前 8 篇）

| 任务 | 指标 | 声称区间 | 实测值 | 难度设计 |
| --- | --- | --- | --- | --- |
| 01-weighted-knn | accuracy | [0.84, 0.95] | 0.9000 | 特征尺度失衡（x1/x2 相差 3 个数量级） |
| 02-logistic-regression | accuracy | [0.82, 0.93] | 0.8750 | 类别不平衡（90/10） |
| 03-naive-bayes | accuracy | [0.79, 0.91] | 0.8500 | 特征强相关（rho=0.9，违反独立性） |
| 04-decision-stump | accuracy | [0.61, 0.74] | 0.6750 | 非线性结构（同心圆，树桩天花板） |
| 05-kmeans-purity | purity | [0.70, 0.83] | 0.7600 | 簇重叠（中心距 0.9，sigma 0.45） |
| 06-linear-regression-r2 | r2 | [0.67, 0.79] | 0.7304 | 训练集离群值污染（6% 目标 +9） |
| 07-perceptron | accuracy | [0.78, 0.92] | 0.8500 | 标签噪声（5% 翻转，线性不可分） |
| 08-nearest-centroid | accuracy | [0.81, 0.93] | 0.8750 | 方差不等 + 先验偏斜（75/25） |

实测值为该任务 `reference_solution.py` 在本机的固定输出（seed 固定，
重复运行逐字节一致）；声称区间是论文（合成）声称的指标范围，实测值
落入区间才算该篇通过。

## 指标含义

- **accuracy**：测试集预测正确比例。harness 复用
  `packages/evaluation` 的 `evaluate_predictions`（trusted evaluator，
  同时强制 id 集合一致）。
- **purity**（聚类纯度）：每个簇内真实类别的众数计数之和 / 总点数。
  `packages/evaluation` 未提供该指标，由
  `tools/benchmark/run_benchmark.py` 本地实现（`_cluster_purity`，
  与 guest 脚本口径一致并做数值交叉校验）。
- **r2**（决定系数）：`1 − SSE/SST`，在干净测试集上计算。同样由
  harness 本地实现（`_r2_score`）。

### 输出文件约定（guest 脚本契约）

每个参考脚本在沙箱工作目录内写三个文件（沙箱按白名单收集）：

- `predictions.json`：`[{"id": "<str>", "prediction": <int>}, ...]`
- `labels.json`：`{"<id>": <int>, ...}`
- `metrics.json`：`{"metric": "<任务指标名>", "value": <float>}`

harness 对 `metrics.json` 与自己复算的指标做交叉校验（差值 >
1e-9 判失败），guest 自报值永远不会被无条件信任。

**唯一例外（06 回归任务）**：`prediction`/label 为 float（r2 需要
连续值），由 harness 内等价严格的 `_parse_regression` 解析。其余任务
沿用仓库 `validate_output_files` 的严格整数契约。

## 每篇任务的目录结构

```
tools/benchmark/papers/<task_id>/
  paper.md              # ≤40 行合成论文摘要（任务/数据生成/方法/seed/指标/声称区间/难度设计）
  task.json             # metric、claimed_low/high、seed、数据规模、method、difficulty
  reference_solution.py # 自包含标准库脚本（同 seed 逐字节可复现）
```

`task.json` 键集合是**封闭**的（未知键、缺键、task_id 与目录名不一致、
未知 metric 都会被拒绝），保证结果 JSON schema 稳定。

## 如何扩展任务

1. 新建目录 `tools/benchmark/papers/09-<name>/`，目录名即 `task_id`。
2. 写 `paper.md`（≤40 行，必须含 `seed: <n>` 与
   `[<low>, <high>]` 两位小数形式的声称区间——结构测试会核对）。
3. 写 `task.json`（十个必需键；聚类任务可加 `"eval_scope": "all"`）。
4. 写 `reference_solution.py`：仅用 AST 白名单内的标准库模块（math、
   random、json、statistics 等，见
   `packages/orchestration/p2r_orchestration/codegen_service.py` 的
   `IMPORT_ALLOWLIST`），固定 seed，几秒内跑完，按上述契约写三个输出
   文件，且必须能通过 `validate_generated_code`。
5. 实跑脚本得到固定指标值，把声称区间设为实测值附近合理宽度，再在
   `tests/unit/benchmark/test_tasks_structure.py` 的
   `EXPECTED_TASK_IDS` 里登记新任务。

## 离线 / 在线差异

- **离线**：对每篇跑四段（AST → 沙箱 → 指标 → 区间），代码来自仓库
  内预置参考脚本。全程无网络。
- **在线**：五段（抽取 → 代码生成 → AST → 沙箱 → 区间），代码来自
  Provider；指标计算并入「区间命中」段（算不出指标即不算命中）。
  某段失败后，后续段记为 `skipped`，各段成功率 = 通过数 / 总篇数
  （保守口径）。

### 接入 Provider（在线入口）

实现 `ProviderCodegen` 协议（两个同步方法，自行负责网络与密钥）：

```python
class MyProvider:
    def extract(self, paper_md: str, task: dict) -> dict: ...
    def generate_code(self, paper_md: str, task: dict, extraction: dict) -> str: ...
```

然后 `--provider-module my_pkg.my_provider`（默认工厂名
`create_provider`，可用 `--provider-factory` 改）。

推荐做法是适配生产链路
`p2r_orchestration.codegen_service.CodegenService`（其 `generate()`
已是 async，且自带 AST 策略与内容寻址存档），Provider 用
`p2r_orchestration.providers.OpenAICompatibleProvider` + 注入式
`secret_resolver`（密钥只留进程内存）。生成的脚本走与参考脚本相同
的输出契约。默认的 `NotConfiguredProvider` 会拒绝运行并打印上述接线
说明。

## 诚实声明

**离线模式只验证沙箱与评测环节**（AST 门禁、隔离执行、输出解析、
指标计算、区间判定的整条流水线），参考脚本是固定已知的——它
**不代表模型生成代码的成功率**。后者只有在在线模式接入真实
Provider、用五段成功率度量后才成立。单元测试不联网。
