"""Replay saved human/assistant decisions from documented historical checkpoints.
Does not call models, invent confirmations, or spend money.
"""
import argparse,copy,json,math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];H=ROOT/'labeling/history'
def read(name):return json.loads((H/name).read_text())
def valid(b):
 return all(isinstance(b.get(k),(int,float)) and not isinstance(b[k],bool) and math.isfinite(b[k]) for k in ['left','top','width','height']) and b['left']>=0 and b['top']>=0 and b['width']>0 and b['height']>0 and b['left']+b['width']<=1.000001 and b['top']+b['height']<=1.000001

def rebuild():
 labels=read('original_600.json');assert len(labels)==600
 for r in read('human_audit_600.json')['rows']:
  if r['checked_by_oleg']=='confirmed' and not r.get('oleg_note'):
   labels[r['photo_id']]['boxes']=copy.deepcopy(r['boxes_json'])
 review=read('hw5_adjudication_review_received_v2.json');assert review['schema']=='hw5_adjudication_20261119_v1'
 human={r['photo_id']:r for r in review['rows']};assert len(human)==len(review['rows'])==33
 final=read('final_adjudications.json');resolved=set();provenance={}
 for r in read('adjudicated_provisional.json'):
  pid=r['photo_id'];h=human.get(pid);confirmed=h and h['checked_by_oleg']=='confirmed'
  boxes=copy.deepcopy(r['proposed_boxes']);status=r['status']
  if confirmed:boxes=copy.deepcopy(h['boxes_json']);status='human_confirmed';resolved.add(pid)
  elif h:status='needs_adjudication'
  if pid in final:
   assert not confirmed
   boxes=copy.deepcopy(final[pid]['boxes']);status='assistant_final';resolved.add(pid)
  assert status!='needs_adjudication',pid
  labels[pid]={'boxes':boxes};provenance[pid]=status
 assert resolved==set(human) and len(labels)==991
 manifest=[json.loads(l) for l in (ROOT/'data/manifest.jsonl').read_text().splitlines()]
 assert {r['photo_id'] for r in manifest}==set(labels)
 output=[]
 for r in manifest:
  boxes=labels[r['photo_id']]['boxes'];assert all(valid(b) for b in boxes),r['photo_id']
  output.append({k:r[k] for k in ['photo_id','image_path','width','height']}|{'boxes':boxes})
 return output,provenance

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'build/annotations.jsonl');a=p.parse_args()
 rows,provenance=rebuild();reference=[json.loads(l) for l in (ROOT/'data/annotations.jsonl').read_text().splitlines()]
 assert rows==reference,'Rebuilt annotations differ from released dataset'
 a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
 print(json.dumps({'photos':len(rows),'boxes':sum(len(r['boxes']) for r in rows),'exact_match_with_release':True,'human_confirmed_last_pass':sum(s=='human_confirmed' for s in provenance.values()),'assistant_final':sum(s=='assistant_final' for s in provenance.values())}))
if __name__=='__main__':main()
