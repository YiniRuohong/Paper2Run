"""Aggregate one or more judged run directories into summary.json + summary.md.

    .venv/bin/python tools/harness_compare/report.py <run dir> [<run dir> ...] --out <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ARMS = ("generic", "paper2run")
LABEL = {"generic": "通用 harness（Pi + bash/read/write/edit）", "paper2run": "Paper2Run harness"}


def pct(n, d):
    return f"{n}/{d}（{(100 * n / d) if d else 0:.0f}%）"


def fmt(value, digits=4):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for run in args.runs:
        for item in json.loads((run / "judgement.json").read_text()):
            rows.append({**item, "run": run.name})
    args.out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm and "judge_error" not in r]
        n = len(mine)
        summary[arm] = {
            "runs": n,
            "delivered": sum(bool(r.get("delivered")) for r in mine),
            "claim_correct": sum(bool(r.get("claim_correct")) for r in mine),
            "claim_wrong_or_missing": sum(not r.get("claim_correct") for r in mine),
            "labels_correct": sum(bool(r.get("labels_correct")) for r in mine),
            "method_faithful": sum(bool(r.get("method_faithful")) for r in mine),
            "evidence_complete": sum(bool(r.get("evidence_complete")) for r in mine),
            "reproducible": sum(bool(r.get("reproducible")) for r in mine),
            "overclaim_runs": sum(bool(r.get("overclaims")) for r in mine),
            "trustworthy": sum(bool(r.get("delivered") and r.get("claim_correct") and r.get("method_faithful")
                                    and r.get("reproducible") and not r.get("overclaims")) for r in mine),
            "wall_seconds_median": sorted(r["cost"]["wall_seconds"] or 0 for r in mine)[n // 2] if n else None,
            "calls_median": sorted(r["cost"].get("calls") or 0 for r in mine)[n // 2] if n else None,
            "tokens_total": sum(r["cost"].get("total_tokens") or 0 for r in mine),
        }
    (args.out / "summary.json").write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    g, p = summary["generic"], summary["paper2run"]
    lines = ["| 指标 | " + " | ".join(LABEL[a] for a in ARMS) + " |", "|---|---|---|"]
    for key, title in [("delivered", "交付了可评测结果（测试集 id 完全一致）"),
                       ("claim_correct", "报告给用户的指标数值正确"),
                       ("labels_correct", "labels.json 与真实标签一致"),
                       ("method_faithful", "实现与指定方法一致（对照参考实现）"),
                       ("evidence_complete", "证据齐全（代码/固定版本依赖/运行日志/预测/标签）"),
                       ("reproducible", "新容器重跑得到相同预测"),
                       ("trustworthy", "全部满足（可信结果）")]:
        lines.append(f"| {title} | {pct(g[key], g['runs'])} | {pct(p[key], p['runs'])} |")
    lines.append(f"| 出现“复现成功”类过度声明 | {pct(g['overclaim_runs'], g['runs'])} | {pct(p['overclaim_runs'], p['runs'])} |")
    lines.append(f"| 中位耗时（秒） | {g['wall_seconds_median']} | {p['wall_seconds_median']} |")
    lines.append(f"| 中位模型调用次数 | {g['calls_median']} | {p['calls_median']} |")
    lines.append(f"| 总 token | {g['tokens_total']:,} | {p['tokens_total']:,} |")
    detail = ["| 论文场景 | harness | 结果/状态 | 报告值 | 裁判复算值 | 参考实现值 | 数值正确 | 方法一致 | 可重跑 | 评审轮次/发现 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["scenario"], r["arm"], r["run"])):
        if "judge_error" in r:
            detail.append(f"| {r['scenario']} | {r['arm']} | judge error | | | | | | | |")
            continue
        reviews = r.get("reviews")
        review_text = "—" if reviews is None else (" → ".join(f"{x['verdict']}({len(x['findings'])})" for x in reviews) or "无")
        detail.append(" | ".join(["", r["scenario"], r["arm"], str(r.get("outcome")), fmt(r.get("claimed_metric")),
                                  fmt(r.get("true_metric")), fmt(r.get("reference_metric")),
                                  "✓" if r.get("claim_correct") else "✗", "✓" if r.get("method_faithful") else "✗",
                                  "✓" if r.get("reproducible") else "✗", review_text, ""]).strip())
    (args.out / "summary.md").write_text("## 汇总\n\n" + "\n".join(lines) + "\n\n## 逐项\n\n" + "\n".join(detail) + "\n",
                                         encoding="utf-8")
    print("\n".join(lines))
    print()
    print("\n".join(detail))


if __name__ == "__main__":
    main()
