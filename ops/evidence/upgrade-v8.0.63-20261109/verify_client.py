import argparse, datetime, hashlib, json, pathlib, time, urllib.request, urllib.error
p=argparse.ArgumentParser();p.add_argument('root',type=pathlib.Path);p.add_argument('--url');p.add_argument('--generate',action='store_true');a=p.parse_args()
values={}
for line in (a.root/'.env').read_text().splitlines():
 if '=' in line and not line.startswith('#'):
  name,value=line.split('=',1);values[name]=value.strip().strip('"').strip("'")
management=urllib.request.Request('http://127.0.0.1:8317/v0/management/config',headers={'Authorization':'Bearer '+values['MANAGEMENT_PASSWORD']})
with urllib.request.urlopen(management,timeout=10) as response:c=json.load(response)
key=c['api-keys'][0];base=a.url or 'http://127.0.0.1:8317' 
report={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'endpoint':base,'catalog_ok':False,'generation_attempted':a.generate}
def request(route,payload=None):
 return urllib.request.urlopen(urllib.request.Request(base+route,data=None if payload is None else json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}))
try:
 with request('/v1/models') as response:catalog=json.load(response)
 ids=[m['id'] for m in catalog['data']];report.update(catalog_ok=True,model_count=len(ids),luna_models=[m for m in ids if 'luna' in m.lower()],kimi_fallback_visible='kimi-k3' in ids)
 if a.generate:
  candidates=[m for m in ids if m=='gpt-6-luna']
  if not candidates:raise ValueError('expected Luna smoke model absent; no alternate model selected')
  model=candidates[0];report['model']=model;start=time.monotonic()
  payload={'model':model,'instructions':'This is a connectivity check. Reply exactly CLIPROXY_UPGRADE_OK.','input':[{'role':'user','content':[{'type':'input_text','text':'Reply exactly CLIPROXY_UPGRADE_OK.'}]}],'stream':True,'store':False,'reasoning':{'effort':'low'},'tools':[]}
  completed=None;deltas=[]
  with request('/v1/responses',payload) as response:
   report['http_status']=response.status
   trace=response.headers.get('X-CPA-TRACE-ID',''); parts=trace.split('-',2)
   report['selected_account_evidence']=bool(trace)
   report['selected_account_sha256']=hashlib.sha256(parts[1].encode()).hexdigest() if len(parts)==3 else None
   for raw in response:
    if not raw.startswith(b'data:'):continue
    try:event=json.loads(raw[5:])
    except ValueError:continue
    if event.get('type')=='response.output_text.delta':deltas.append(event.get('delta',''))
    if event.get('type')=='response.completed':completed=event.get('response',{})
    if event.get('type') in ('response.failed','error','response.incomplete'):report['terminal_error']=event.get('type')
  report.update(elapsed_seconds=round(time.monotonic()-start,2),completed=completed is not None,response_matches=''.join(deltas).strip()=='CLIPROXY_UPGRADE_OK')
  if completed:
   usage=completed.get('usage') or {}
   report['usage']={k:usage[k] for k in ['input_tokens','input_tokens_details','output_tokens','output_tokens_details','total_tokens'] if k in usage}
   report['response_model']=completed.get('model')
except urllib.error.HTTPError as e:report['http_error']=e.code
except Exception as e:report['error_type']=type(e).__name__
print(json.dumps(report,indent=2))
raise SystemExit(0 if report['catalog_ok'] and (not a.generate or (report.get('completed') and report.get('response_matches') and not report.get('terminal_error'))) else 1)
