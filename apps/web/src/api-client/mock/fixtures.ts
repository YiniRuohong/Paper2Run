// Mock 数据基座：全部 ID 全链路关联（project→paper→extraction→experiment→revision→run→attempt→artifact），
// 数字为有机测试值并整体标注 mock；组件永不直接引用这里的任何字面量。
import type {
  Attempt, Capabilities, CodeFile, Experiment, Export, Extraction, Operation,
  Paper, Project, Report, Run, RunCurves, RunManifest, SessionInfo, SpecDraft,
  SpecRevision, TextChunk,
} from '../types';

export interface MutableDb {
  loadedAt: number;
  scenarioId: string;
  session: SessionInfo | null;
  capabilities: Capabilities;
  projects: Project[];
  papers: Paper[];
  extractions: Extraction[];
  experiments: Experiment[];
  drafts: Map<string, SpecDraft>;
  revisions: SpecRevision[];
  runs: Run[];
  attempts: Attempt[];
  filesByAttempt: Map<string, CodeFile[]>;
  artifacts: Map<string, string>;
  reports: Map<string, Report | { kind: 'generating'; operationId: string | null }>;
  manifests: Map<string, RunManifest>;
  curves: Map<string, RunCurves>;
  exports: Map<string, Export[]>;
  operations: Map<string, Operation>;
  conflictOnce: boolean;
  expireNext: boolean;
  seq: number;
  cancelRequestedAt: Map<string, number>;
  uploadReadyAt: Map<string, number>;
  exportCreatedAt: Map<string, number>;
  cancelOutcome?: (runId: string) => 'completed' | 'cancelled';
  tickOverride?: (db: MutableDb, elapsed: number) => void;
}

// 确定性伪随机（fixture 生成 hash 用，避免每次刷新变化）
function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0xffffffff;
  };
}
export function fakeHash(seed: string): string {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) h = (h ^ seed.charCodeAt(i)) * 16777619 >>> 0 >>> 0;
  const rnd = lcg(h);
  return Array.from({ length: 64 }, () => Math.floor(rnd() * 16).toString(16)).join('');
}
export function fakeId(prefix: string, n: number): string {
  return `${prefix}_${n.toString(36).padStart(4, '0')}`;
}
export function iso(msAgo: number): string {
  return new Date(Date.now() - msAgo).toISOString().replace(/\.\d{3}Z$/, 'Z') + '';
}
export function utcFrom(ms: number): string {
  return new Date(ms).toISOString();

}

const CAPABILITIES: Capabilities = {
  contract_version: '0.2',
  execution_backend: 'mock',
  data_mode: 'mock',
  writes_enabled: true,
  upload: { max_pdf_bytes: 26214400, max_pages: 60, arxiv_enabled: true },
  limits: { max_repairs: 5, max_queued_runs_per_user: 2 },
  recipes: [{
    id: 'cv_clf_cifar10', version: '1.4.2', label: 'CIFAR-10 图像分类（残差网络 / 普通 CNN）',
    modes: ['smoke', 'scaled', 'reference'], dataset_ids: ['cifar10'],
    execution_support: 'available', reason: null,
  }],
  models: [
    { purpose: 'parse', display_name: 'Qwen-VL-Max', available: true },
    { purpose: 'plan', display_name: 'Qwen3-Max', available: true },
    { purpose: 'code', display_name: 'Qwen3-Coder', available: true },
  ],
};

export const DEMO_USER: SessionInfo = {
  user: { id: 'usr_801', display_name: '陈立群' },
  session: {
    expires_at: utcFrom(Date.now() + 24 * 3600_000),
    idle_expires_at: utcFrom(Date.now() + 4 * 3600_000),
  },
  csrf_token: 'mock-csrf-token',
  permissions: { can_create_project: true },
  memberships: [{ project_id: 'prj_demo01', role: 'editor' }],
};

function baseProject(): Project {
  return {
    id: 'prj_demo01',
    name: 'ResNet 复现验证',
    description: 'CIFAR-10 残差网络论文复现实验',
    role: 'editor',
    version: 3,
    deletion_state: 'active',
    created_at: iso(6 * 24 * 3600_000),
    updated_at: iso(2 * 3600_000),
  };
}

function basePaper(): Paper {
  return {
    paper_id: 'pap_1001',
    project_id: 'prj_demo01',
    title: 'Deep Residual Learning for Image Recognition',
    status: 'ready',
    sha256: fakeHash('paper-1512.03385v1'),
    arxiv_version: '1512.03385v1',
    page_count: 12,
    content_available: false,
    current_extraction_id: 'ext_1001',
    active_operation_id: null,
    processing_consent: { accepted_at: iso(5 * 24 * 3600_000), policy_version: 'consent-2026-09' },
    error: null,
    created_at: iso(5 * 24 * 3600_000),
  };
}

export function baseExtraction(): Extraction {
  return {
    extraction_id: 'ext_1001',
    paper_id: 'pap_1001',
    schema_version: '1.0',
    fields: {
      task: { value: '图像分类（监督学习）', evidence_status: 'supported', source_refs: ['src_1'] },
      dataset: { value: 'CIFAR-10', evidence_status: 'supported', source_refs: ['src_2'] },
      optimizer: { value: 'SGD + Momentum 0.9', evidence_status: 'supported', source_refs: ['src_3'] },
    },
    methods: [
      { id: 'mth_1', label: '残差连接（identity shortcut）', source_refs: ['src_1'] },
      { id: 'mth_2', label: 'BatchNorm', source_refs: ['src_4'] },
      { id: 'mth_3', label: 'He 初始化', source_refs: ['src_4'] },
      { id: 'mth_4', label: '权重衰减 5e-4', source_refs: ['src_3'] },
      { id: 'mth_5', label: '数据增强（裁剪 + 翻转）', source_refs: ['src_2'] },
    ],
    formulas: [
      { id: 'eq_1', latex: 'y = F(x, {W_i}) + x', source_refs: ['src_1'] },
      { id: 'eq_4', latex: 'F(x) = W_2 · σ(W_1 · x)', source_refs: ['src_1'] },
    ],
    candidate_experiments: [
      {
        id: 'cand_t6',
        label: 'Table 6 · CIFAR-10 测试误差（ResNet-20，182 epochs）',
        source_refs: ['src_5'],
        recipe_id: 'cv_clf_cifar10',
        recipe_version: '1.4.2',
        execution_support: 'supported',
        unsupported_reasons: [],
      },
      {
        id: 'cand_t1',
        label: 'Table 1 · ImageNet Top-5（ResNet-50）',
        source_refs: ['src_6'],
        recipe_id: null,
        recipe_version: null,
        execution_support: 'unsupported',
        unsupported_reasons: ['需 8×GPU 分布式训练，超出单卡执行范围', '首版仅支持 CIFAR-10 规模实验'],
      },
    ],
    source_refs: [
      {
        id: 'src_1', paper_id: 'pap_1001', pdf_sha256: fakeHash('paper-1512.03385v1'), page: 2,
        section: '3.4. Architectures for CIFAR-10', table: null, row: null,
        quote: 'The shortcut connections simply perform identity mapping, and their outputs are added to the outputs of the stacked layers.',
        bbox: [0.08, 0.32, 0.92, 0.38],
      },
      {
        id: 'src_2', paper_id: 'pap_1001', pdf_sha256: fakeHash('paper-1512.03385v1'), page: 5,
        section: '4.2. CIFAR-10 and Analysis', table: null, row: null,
        quote: 'For preprocessing, we normalize the data... and use a padded crop of 32×32 with horizontal flip.',
        bbox: [0.06, 0.51, 0.94, 0.57],
      },
      {
        id: 'src_3', paper_id: 'pap_1001', pdf_sha256: fakeHash('paper-1512.03385v1'), page: 5,
        section: '4.2. CIFAR-10 and Analysis', table: null, row: null,
        quote: 'We use SGD with a mini-batch size of 128... weight decay of 0.0005 and momentum of 0.9.',
        bbox: [0.06, 0.44, 0.9, 0.5],
      },
      {
        id: 'src_4', paper_id: 'pap_1001', pdf_sha256: fakeHash('paper-1512.03385v1'), page: 3,
        section: '3.3. Network Architectures', table: null, row: null,
        quote: 'Batch normalization is adopted right after each convolution and before activation.',
        bbox: null,
      },
      {
        id: 'src_5', paper_id: 'pap_1001', pdf_sha256: fakeHash('paper-1512.03385v1'), page: 7,
        section: 'Table 6', table: 'Table 6', row: 'ResNet-20',
        quote: 'ResNet-20 achieves a 8.75% test error on CIFAR-10 with the standard 5:1 train/test split.',
        bbox: [0.2, 0.2, 0.8, 0.28],
      },
      {
        id: 'src_6', paper_id: 'pap_1001', pdf_sha256: fakeHash('paper-1512.03385v1'), page: 5,
        section: 'Table 1', table: 'Table 1', row: 'ResNet-50',
        quote: 'Single-crop validation error rate of 24.7% / 7.9% on the ImageNet validation set.',
        bbox: null,
      },
    ],
    warnings: ['公式 Eq.4 的符号在正文与图示间存在差异，已按正文版本记录'],
  };
}

function baseExperiment(): Experiment {
  return {
    experiment_id: 'exp_1001',
    project_id: 'prj_demo01',
    paper_id: 'pap_1001',
    status: 'draft',
    head_revision_id: null,
    active_run_id: null,
    latest_run_id: null,
    repairs_used: 0,
    repairs_limit: 5,
    created_at: iso(30 * 3600_000),
  };
}

export function baseDraft(): SpecDraft {
  return {
    experiment_id: 'exp_1001',
    base_revision_id: null,
    policy_hash: fakeHash('policy-2026-09-v3'),
    spec: {
      schema_version: '1.0',
      paper_id: 'pap_1001',
      extraction_id: 'ext_1001',
      candidate_experiment_id: 'cand_t6',
      recipe: { id: 'cv_clf_cifar10', version: '1.4.2' },
      mode: 'smoke',
      method: { family: 'resnet', variant: 'resnet-20', must_include: ['identity shortcut', 'batchnorm'] },
      dataset: {
        id: 'cifar10', version: '2011.06-split5to1', split_policy_id: 'cifar10-standard',
        train_subset_fraction: 0.1,
      },
      training: {
        seed: 20260919, batch_size: 128, max_steps: null, max_epochs: 3,
        optimizer: 'sgd', learning_rate: 0.1, weight_decay: 0.0005,
      },
      targets: [{
        id: 'tgt_top1',
        metric: 'Top-1 Error',
        unit: 'percent',
        direction: 'lower',
        reference_value: 8.75,
        source_ref: 'src_5',
        statistic: 'mean',
        tolerance: 1.0,
        weight: 1,
      }],
      budget: {
        max_model_calls: 60, max_tokens: 4_000_000, max_execution_seconds: 7200,
        max_artifact_bytes: 2_000_000_000, currency: 'CNY', max_cost_micros: 20_000_000,
      },
      recipe_config: {
        scheduler: 'multi_step', milestones: [91, 136], gamma: 0.1,
        augmentation: ['random_crop', 'horizontal_flip'], checkpoint_select: 'best_val',
      },
      assumptions: [
        { path: 'training.batch_size', description: '沿用论文 4.2 节声明的 mini-batch 128', source_refs: ['src_3'] },
        { path: 'dataset.split_policy_id', description: '论文未给出随机种子，采用标准 5:1 划分', source_refs: ['src_5'] },
      ],
      user_changes: [],
    },
    editable_fields: [
      { path: 'mode', kind: 'select', label: '实验模式', required: true, nullable: false, min: null, max: null, options: [
        { value: 'smoke', label: 'smoke · 流程验证（子集 10%，3 epochs）' },
        { value: 'scaled', label: 'scaled · 缩减实验（全量数据，缩减轮数）' },
      ] },
      { path: 'training.seed', kind: 'integer', label: '随机种子', required: true, nullable: false, min: 0, max: 2_147_483_647, options: null },
      { path: 'training.batch_size', kind: 'integer', label: '批大小', required: true, nullable: false, min: 8, max: 1024, options: null },
      { path: 'training.max_epochs', kind: 'integer', label: '训练轮数', required: true, nullable: false, min: 1, max: 400, options: null },
      { path: 'dataset.train_subset_fraction', kind: 'number', label: '训练子集比例', required: true, nullable: false, min: 0.01, max: 1, options: null },
    ],
    issues: [
      { code: 'PAPER_BEST_OF_MULTIPLE_RUNS', message: '论文报告的误差为多次运行统计值，单次运行的实测结果可能偏高', path: 'targets[0]', severity: 'warning' },
    ],
    comparison_precheck: { status: 'not_comparable', reasons: ['SMOKE_MODE', 'TRAINING_BUDGET_REDUCED'] },
  };
}

// ---- 代码 / 日志 / diff 文本（Mock 产物，经 artifact 层下发）----

const CODE_TRAIN = `import torch
from torch import nn
from model import ResNet20
from data import load_cifar10

def main():
    torch.manual_seed(SEED)
    model = ResNet20(num_classes=10).cuda()
    opt = torch.optim.SGD(model.parameters(), lr=0.1,
                          momentum=0.9, weight_decay=5e-4)
    sched = torch.optim.lr_scheduler.MultiStepLR(
        opt, milestones=MILESTONES, gamma=0.1)
    train_loader, val_loader = load_cifar10(
        subset_fraction=SUBSET_FRACTION, batch_size=128)
    for epoch in range(1, MAX_EPOCHS + 1):
        model.train()
        for x, y in train_loader:
            x, y = x.cuda(), y.cuda()
            opt.zero_grad()
            loss = nn.functional.cross_entropy(model(x), y)
            loss.backward()
            opt.step()
        sched.step()
        val_err = evaluate(model, val_loader)
        print(f"epoch={epoch} train_loss={loss.item():.4f} "
              f"val_err={val_err:.2f}% lr={sched.get_last_lr()[0]:.4f}")
    torch.save(model.state_dict(), "checkpoints/final.pt")

if __name__ == "__main__":
    main()
`;

const CODE_MODEL = `import torch
from torch import nn

class BasicBlock(nn.Module):
    def __init__(self, in_ch, out_ch, stride=1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_ch, out_ch, 3, stride, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, 1, 1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_ch)
        self.shortcut = nn.Identity()
        if stride != 1 or in_ch != out_ch:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_ch, out_ch, 1, stride, bias=False),
                nn.BatchNorm2d(out_ch))

    def forward(self, x):
        out = torch.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return torch.relu(out + self.shortcut(x))

def ResNet20(num_classes=10):
    layers = [3, 3, 3]
    def blocknet(in_ch, ch, n, stride):
        mods = [BasicBlock(in_ch, ch, stride)]
        mods += [BasicBlock(ch, ch) for _ in range(n - 1)]
        return nn.Sequential(*mods)
    net = nn.Sequential(
        nn.Conv2d(3, 16, 3, 1, 1, bias=False),
        nn.BatchNorm2d(16), nn.ReLU(inplace=True),
        blocknet(16, 16, layers[0], 1),
        blocknet(16, 32, layers[1], 2),
        blocknet(32, 64, layers[2], 2),
        nn.AdaptiveAvgPool2d(1), nn.Flatten(),
        nn.Linear(64, num_classes))
    for m in net.modules():
        if isinstance(m, nn.Conv2d):
            nn.init.kaiming_normal_(m.weight, mode="fan_out")
    return net
`;

const CODE_DATA = `import torchvision
import torchvision.transforms as T
from torch.utils.data import DataLoader

def load_cifar10(subset_fraction=1.0, batch_size=128):
    mean, std = (0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616)
    train_tf = T.Compose([
        T.RandomCrop(32, padding=4),
        T.RandomHorizontalFlip(),
        T.ToTensor(), T.Normalize(mean, std)])
    test_tf = T.Compose([T.ToTensor(), T.Normalize(mean, std)])
    train = torchvision.datasets.CIFAR10(
        "data", train=True, download=True, transform=train_tf)
    if subset_fraction < 1.0:
        n = int(len(train) * subset_fraction)
        train = torch.utils.data.Subset(train, range(n))
    test = torchvision.datasets.CIFAR10(
        "data", train=False, download=True, transform=test_tf)
    return (DataLoader(train, batch_size, shuffle=True, num_workers=4),
            DataLoader(test, 512, shuffle=False, num_workers=2))
`;

const CODE_EVAL = `import torch

@torch.no_grad()
def evaluate(model, loader):
    model.eval()
    correct, total = 0, 0
    for x, y in loader:
        pred = model(x.cuda()).argmax(1).cpu()
        correct += (pred == y).sum().item()
        total += y.numel()
    return 100.0 * (1 - correct / total)
`;

const CODE_REQ = `torch==2.4.1
torchvision==0.19.1
numpy==1.26.4
`;

const PATCH_DIFF = `--- a/model.py
+++ b/model.py
@@ -12,6 +12,9 @@ class BasicBlock(nn.Module):
         self.bn2 = nn.BatchNorm2d(out_ch)
         self.shortcut = nn.Identity()
         if stride != 1 or in_ch != out_ch:
-            self.shortcut = nn.Sequential(
-                nn.Conv2d(in_ch, out_ch, 1, stride, bias=False))
+            self.shortcut = nn.Sequential(
+                nn.Conv2d(in_ch, out_ch, 1, stride, bias=False),
+                nn.BatchNorm2d(out_ch))
+        # 诊断：shortcut 分支缺少 BatchNorm，导致维度变换后
+        # 统计量漂移，与论文 3.3 节(option B)不一致
`;

export function trainingLogLines(maxEpoch: number, uptoEpoch: number): string[] {
  const lines: string[] = [
    '[system] container started (image cuda12.1-torch2.4, gpu_slot=gpu-0)',
    '[system] dataset cifar10 ready (split_sha256 verified)',
    '[stdout] train files: 50000, val files: 10000',
  ];
  let loss = 2.31;
  let err = 65.0;
  const rnd = lcg(20260919);
  for (let e = 1; e <= Math.min(uptoEpoch, maxEpoch); e++) {
    loss = Math.max(0.02, loss - 0.05 - rnd() * 0.06);
    err = Math.max(8.4, err - (1.8 + rnd() * 2.2));
    lines.push(
      `[stdout] epoch=${e}/${maxEpoch} train_loss=${loss.toFixed(4)} val_err=${err.toFixed(2)}% lr=0.1000`,
    );
  }
  return lines;
}

export function baseRun(statusOverride?: Partial<Run>): Run {
  const run: Run = {
    id: 'run_3001',
    project_id: 'prj_demo01',
    paper_id: 'pap_1001',
    paper_title: 'Deep Residual Learning for Image Recognition',
    experiment_id: 'exp_1001',
    spec_revision_id: 'rev_2001',
    parent_run_id: null,
    status: 'executing',
    stage: 'train_execute',
    version: 12,
    last_event_seq: 36,
    attempt_index: 1,
    repairs_used: 0,
    repairs_limit: 5,
    mode: 'scaled',
    data_mode: 'mock',
    budget: {
      currency: 'CNY', limit_micros: 20_000_000,
      actual_micros: 6_842_000, reserved_micros: 8_000_000, unknown_charges: false,
    },
    resources: {
      cpu_millicores: 4000, ram_limit_bytes: 17_179_869_184,
      gpu_slot: 'gpu-0', gpu_memory_used_bytes: 3_145_728_000,
    },
    report_status: 'pending',
    terminal_reason: null,
    created_at: iso(26 * 60_000),
    started_at: iso(25 * 60_000),
    ended_at: null,
  };
  return { ...run, ...statusOverride };
}

export function baseAttempt(ordinal: number, statusOverride?: Partial<Attempt>): Attempt {
  const a: Attempt = {
    id: `att_400${ordinal}`,
    run_id: 'run_3001',
    attempt_index: ordinal,
    repair_ordinal: ordinal - 1,
    status: 'running',
    code_hash: fakeHash(`attempt-${ordinal}-code`),
    code_manifest_id: `art_cm_400${ordinal}`,
    patch_artifact_id: ordinal > 1 ? `art_patch_400${ordinal}` : null,
    parent_attempt_id: ordinal > 1 ? `att_400${ordinal - 1}` : null,
    error_summary: null,
    diagnosis_summary: null,
    evidence_artifact_ids: [],
    log_artifact_ids: [`art_log_400${ordinal}`],
    created_at: iso((30 - ordinal * 2) * 60_000),
  };
  return { ...a, ...statusOverride };
}

export function baseFiles(attemptId: string): CodeFile[] {
  const entries: [string, number][] = [
    ['train.py', 1042],
    ['model.py', 1286],
    ['data.py', 934],
    ['evaluate.py', 387],
    ['requirements.txt', 71],
  ];
  return entries.map(([path, bytes]) => {
    const artifact_id = `art_${attemptId}_${path.replace(/\W/g, '_')}`;
    ARTIFACT_TEXT.set(artifact_id, fileText(path));
    return { path, artifact_id, bytes, sha256: fakeHash(`${attemptId}:${path}`), media_type: 'text/x-python' };
  });
}

const ARTIFACT_TEXT = new Map<string, string>();

function fileText(path: string): string {
  switch (path) {
    case 'train.py': return CODE_TRAIN;
    case 'model.py': return CODE_MODEL;
    case 'data.py': return CODE_DATA;
    case 'evaluate.py': return CODE_EVAL;
    case 'requirements.txt': return CODE_REQ;
    default: return '';
  }
}

export function artifactText(id: string, offset: number): TextChunk {
  const text = ARTIFACT_TEXT.get(id) ?? '';
  const LIMIT = 65536;
  const slice = text.slice(offset, offset + LIMIT);
  const next = offset + slice.length;
  return { artifact_id: id, text: slice, offset, next_offset: next, eof: next >= text.length };
}

export function registerArtifact(id: string, text: string): void {
  ARTIFACT_TEXT.set(id, text);
}
export function getRegisteredArtifact(id: string): string | undefined {
  return ARTIFACT_TEXT.get(id);
}
export function patchText(): string { return PATCH_DIFF; }

export function createBaseDb(scenarioId: string): MutableDb {
  const db: MutableDb = {
    loadedAt: Date.now(),
    scenarioId,
    session: null,
    capabilities: CAPABILITIES,
    projects: [baseProject()],
    papers: [basePaper()],
    extractions: [baseExtraction()],
    experiments: [baseExperiment()],
    drafts: new Map([['exp_1001', baseDraft()]]),
    revisions: [],
    runs: [],
    attempts: [],
    filesByAttempt: new Map(),
    artifacts: new Map(),
    reports: new Map(),
    manifests: new Map(),
    curves: new Map(),
    exports: new Map(),
    operations: new Map(),
    conflictOnce: false,
    expireNext: false,
    seq: 1,
    cancelRequestedAt: new Map(),
    uploadReadyAt: new Map(),
    exportCreatedAt: new Map(),
  };
  return db;
}

export function cloneDb(scenarioId: string): MutableDb {
  const db = createBaseDb(scenarioId);
  db.loadedAt = Date.now();
  return db;
}
