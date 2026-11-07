import json,sys
from pathlib import Path
run=Path(sys.argv[1])
J=[r for r in json.loads((run/'judgement.json').read_text()) if 'judge_error' not in r]
A={}
for ap in [run/'audit.json',*map(Path,sys.argv[2:])]:
    for a in (json.loads(ap.read_text()) if ap.is_file() else []):
        if 'audit_error' not in a:
            A[(a['arm'],a['scenario'])]=a
by={(r['arm'],r['scenario']):r for r in J}
names=sorted({s for a,s in by if a=='paper2run'}&{s for a,s in by if a=='generic'})
n=len(names)
missing=[(arm,s) for s in names for arm in ('generic','paper2run') if (arm,s) not in A]
if missing: sys.exit('audit missing or failed for: '+str(missing))
ge=lambda s:len(A.get(('generic',s),{}).get('errors',[]))
pe=lambda s:len(A.get(('paper2run',s),{}).get('errors',[]))
ok=lambda arm,k:sum(bool(by[(arm,s)].get(k)) for s in names)
caught=[s for s in names if any(x.get('verdict')=='issues' for x in by[('paper2run',s)].get('reviews') or [])]
found=sum(len(x.get('findings',[])) for s in caught for x in by[('paper2run',s)]['reviews'] if x.get('verdict')=='issues')
verified=sum(by[('paper2run',s)].get('outcome')=='verified' for s in names)
NL=chr(10)
disp=['# Paper2Run 与通用 harness 对比（展示版）','','同一模型 zcode/GLM-5.3-Flash、同一容器镜像、同一任务文本；'+str(n)+' 篇 CPU 快速论文，指标均由独立裁判复算。','','| 指标 | 通用 harness | Paper2Run |','|---|---|---|']
disp.append(f'| 最终报告含事实错误的篇数（GLM-5.3 逐条审计） | {sum(ge(s)>0 for s in names)}/{n} | {sum(pe(s)>0 for s in names)}/{n} |')
disp.append(f'| 交付报告中的事实错误总条数 | {sum(ge(s) for s in names)} | {sum(pe(s) for s in names)} |')
disp.append(f'| 过程中被独立评审拦下并修复的报告 | 无评审环节 | {len(caught)}/{n}（共 {found} 条发现） |')
disp.append(f'| 程序判定 verified | 无程序判定 | {verified}/{n} |')
disp.append(f'| 指标与参考实现一致 | {ok("generic","claim_correct") if False else sum(bool(by[("generic",s)].get("claim_correct")) for s in names)}/{n} | {ok("paper2run","claim_correct")}/{n} |')
disp.append(f'| 新容器重跑得到相同预测 | {ok("generic","reproducible")}/{n} | {ok("paper2run","reproducible")}/{n} |')
disp.append('| 指标来源 | 智能体自述 | 程序按冻结预测与真实标签计算，附 95% 置信区间 |')
full=['# 完整记录（内部）','','| 场景 | harness | 状态 | 报告值 | 裁判复算 | 参考 | 数值对 | 可重跑 | 报告事实错误 | 耗时s | 调用 | tokens |','|---|---|---|---|---|---|---|---|---|---|---|---|']
for s in names:
    for arm in ('generic','paper2run'):
        r=by[(arm,s)];c=r.get('cost',{})
        full.append('| '+' | '.join(str(x) for x in [s,arm,r.get('outcome'),r.get('claimed_metric'),r.get('true_metric'),r.get('reference_metric'),r.get('claim_correct'),r.get('reproducible'),len(A.get((arm,s),{}).get('errors',[])),c.get('wall_seconds'),c.get('calls'),c.get('total_tokens')])+' |')
(run/'showcase.md').write_text(NL.join(disp)+NL)
(run/'internal-full.md').write_text(NL.join(full)+NL)
print(NL.join(disp))
