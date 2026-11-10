from pathlib import Path
import datetime,hashlib,json,os,shutil,subprocess
root=Path.cwd()
backup=root/'ops/runtime/upgrade-v8.0.63-20261109'/('backup-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
backup.mkdir(mode=0o700,parents=True,exist_ok=False)
files=['.env','config.yaml','docker-compose.yml','docker-compose.override.yml','ops/fleet-release.json','ops/workload-contract.json','ops/instances/will/docker-compose.yml','ops/instances/will/workload-contract.json','ops/instances/will/config-policy.json','ops/verify_fleet_instance.py','ops/fleet_routing.py','ops/instances/will/verify_live.py','ops/FLEET.md']
receipt={'root':str(root),'backup':str(backup),'files':{}}
for item in files:
 src=root/item
 if not src.exists():
  receipt['files'][item]={'exists':False}; continue
 dst=backup/item
 dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 shutil.copy2(src,dst)
 os.chmod(dst,0o600)
 receipt['files'][item]={'exists':True,'sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'mode':oct(src.stat().st_mode&0o777)}
ids=subprocess.check_output(['docker','ps','-aq'],text=True).split()
containers=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True)) if ids else []
receipt['containers']=[{'id':c['Id'],'name':c['Name'],'image':c['Image'],'started':c['State']['StartedAt'],'running':c['State']['Running'],'source':c['Config']['Labels'].get('org.opencontainers.image.revision')} for c in containers]
(backup/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'backup':str(backup),'file_count':sum(x['exists'] for x in receipt['files'].values()),'container_count':len(containers)}))
