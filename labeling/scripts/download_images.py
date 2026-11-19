"""Download frozen source images and reject changed bytes. No Yandex credentials needed."""
import argparse,concurrent.futures,hashlib,json,os,tempfile,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def fetch(row,dest,verify_only=False):
 target=dest/row['image_path'];target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists():
  if sha(target)!=row['sha256']:raise ValueError('existing file SHA256 mismatch; preserved for inspection')
  return 'verified'
 if verify_only:raise FileNotFoundError('missing image')
 fd,name=tempfile.mkstemp(prefix=target.name+'.',suffix='.part',dir=target.parent);os.close(fd);tmp=Path(name)
 try:
  req=urllib.request.Request(row['image_url'],headers={'User-Agent':'HW5-dataset-reproduction/1.0'})
  with urllib.request.urlopen(req,timeout=60) as response,tmp.open('wb') as f:
   while block:=response.read(1024*1024):f.write(block)
  if sha(tmp)!=row['sha256']:raise ValueError('source changed: SHA256 mismatch; not installed')
  tmp.replace(target);return 'downloaded'
 finally:tmp.unlink(missing_ok=True)
def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,default=ROOT/'data/manifest.jsonl');p.add_argument('--dest',type=Path,default=ROOT/'data');p.add_argument('--workers',type=int,default=4);p.add_argument('--limit',type=int);p.add_argument('--verify-only',action='store_true');a=p.parse_args()
 rows=[json.loads(l) for l in a.manifest.read_text().splitlines()];assert len({r['photo_id'] for r in rows})==len(rows)
 for r in rows:
  path=Path(r['image_path']);assert not path.is_absolute() and '..' not in path.parts
 if a.limit is not None:
  assert a.limit>0;rows=rows[:a.limit]
 assert 1<=a.workers<=16
 errors=[];done=0
 with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
  futures={pool.submit(fetch,r,a.dest,a.verify_only):r for r in rows}
  for f in concurrent.futures.as_completed(futures):
   r=futures[f]
   try:f.result()
   except Exception as e:errors.append({'photo_id':r['photo_id'],'error':str(e)})
   done+=1
   if done%100==0:print(f'{done}/{len(rows)}; errors={len(errors)}',flush=True)
 print(json.dumps({'checked':done,'errors':errors},ensure_ascii=False));raise SystemExit(bool(errors))
if __name__=='__main__':main()
