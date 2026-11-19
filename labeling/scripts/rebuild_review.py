import json
from pathlib import Path
from collections import Counter,defaultdict
from decimal import Decimal
ROOT=Path(__file__).resolve().parents[2];P=ROOT/'labeling/raw/expansion';OUT=ROOT/'build';OUT.mkdir(exist_ok=True)
bbox=defaultdict(list);cost=defaultdict(Decimal);sources={};pairs=defaultdict(list);failed=[];allkeys=set();excluded=0
for file in P.glob('live_assignments_*.json'):
 kind=file.stem.removeprefix('live_assignments_');rows=json.loads(file.read_text())['items']
 for a in rows:
  cost[a['status']]+=Decimal(str(a['reward']))*Decimal('1.4')
  if a['status'] not in ['SUBMITTED','ACCEPTED']:continue
  if not kind.startswith('review'):
   for t,s in zip(a['tasks'],a.get('solutions') or []):
    if t.get('known_solutions'):continue
    iv=t['input_values'];out=s['output_values'];key=(iv['photo_id'],a['id'])
    bbox[iv['photo_id']].append({'assignment':a['id'],'user':a['user_id'],'boxes':out.get('boxes') or [],'load_error':out.get('load_error',False)})
    sources[key]=a['user_id']
for file in P.glob('live_assignments_review*.json'):
 for a in json.loads(file.read_text())['items']:
  if a['status'] not in ['SUBMITTED','ACCEPTED']:continue
  bad=any(t.get('known_solutions') and s['output_values'].get('verdict')!=t['known_solutions'][0]['output_values']['verdict'] for t,s in zip(a['tasks'],a.get('solutions') or []))
  if bad:failed.append(a['id'])
  for t,s in zip(a['tasks'],a.get('solutions') or []):
   if t.get('known_solutions'):continue
   iv=t['input_values'];key=(iv['photo_id'],iv['bbox_assignment_id']);allkeys.add(key)
   if bad:continue
   if key in sources and sources[key]==a['user_id']:excluded+=1;continue
   pairs[key].append((a['user_id'],s['output_values'].get('verdict')))
result={};duplicate_voters=[]
for key in allkeys:
 votes=pairs[key];unique={u:v for u,v in votes}
 if len(unique)!=len(votes):duplicate_voters.append(key)
 v=list(unique.values());label='insufficient_votes' if len(v)<3 else 'load_error' if 'load_error' in v else 'unanimous_ok' if set(v)=={'ok'} else 'majority_bad' if v.count('bad')>=2 else 'disagreement'
 result['|'.join(key)]={'decision':label,'votes':v}
expected={str(r['photo_id']) for r in json.loads((ROOT/'labeling/history/expansion_routes.json').read_text()) if r['route']!='review_only'}|{'597419379','367623','580638','327719751'}
coverage=Counter(sum(not a['load_error'] for a in bbox[p]) for p in expected)
hard={pid:{'box_counts':[len(a['boxes']) for a in bbox[pid]],'reviews':[result.get(pid+'|'+a['assignment']) for a in bbox[pid]]} for pid in ['597419379','367623','580638','327719751','94780787','375420526']}
summary={'bbox_photos_expected':len(expected),'bbox_coverage':dict(coverage),'review_pairs':len(result),'review_decisions':dict(Counter(v['decision'] for v in result.values())),'photos_with_nonunanimous_review':len({k.split('|')[0] for k,v in result.items() if v['decision']!='unanimous_ok'}),'failed_review_pages':len(failed),'author_review_excluded':excluded,'duplicate_voters':duplicate_voters,'hard_photos':hard,'paid_rub':str(cost['ACCEPTED']),'reserved_submitted_rub':str(cost['SUBMITTED']),'balance_rub':json.loads((ROOT/'labeling/reports/final_pool_statuses.json').read_text())['balance'],'missing_review_pairs':[(pid,a['assignment']) for pid,aa in bbox.items() for a in aa if (pid,a['assignment']) not in allkeys]}
(OUT/'review_pair_decisions.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));(OUT/'review_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False,indent=2))

reference=json.loads((ROOT/'labeling/history/review_pair_decisions.json').read_text())
assert result==reference, 'Review aggregation differs from archived decisions'
print('Review decisions exactly reproduced:',len(result))
