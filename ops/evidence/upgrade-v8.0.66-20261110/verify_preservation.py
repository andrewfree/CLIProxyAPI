from pathlib import Path
import datetime,hashlib,json,re,subprocess,sys
root=Path.cwd();backup=Path(sys.argv[1]);release=json.loads((root/'ops/fleet-release.json').read_text())
c=json.loads(subprocess.check_output(['docker','inspect','cli-proxy-api'],text=True))[0]
i=json.loads(subprocess.check_output(['docker','image','inspect',c['Image']],text=True))[0];arch=i['Architecture']
expected=(backup/'config.yaml').read_text()
oldenv=(backup/'.env').read_text();newenv,n=re.subn(r'(?m)^CLI_PROXY_IMAGE=.*$', 'CLI_PROXY_IMAGE='+release['images'][arch],oldenv);assert n==1
cfg=root/'config.yaml';ports=c['HostConfig']['PortBindings']
expected_ports={str(p)+'/tcp':[{'HostIp':'127.0.0.1','HostPort':str(p)}] for p in [8317,8085,1455,54545,51121,11451]}
proc=subprocess.check_output(['docker','exec','cli-proxy-api','cat','/proc/net/tcp','/proc/net/tcp6'],text=True)
listener=any(len(x.split())>3 and x.split()[1].endswith(':207C') and x.split()[3]=='0A' for x in proc.splitlines())
mounts={m['Destination']:m['Source'] for m in c['Mounts']}
expected_mounts={'/CLIProxyAPI/config.yaml':str(root/'config.yaml'),'/root/.cli-proxy-api':str(root/'auths'),'/CLIProxyAPI/logs':str(root/'logs'),'/CLIProxyAPI/plugins':str(root/'plugins')}
inside=subprocess.check_output(['docker','exec','cli-proxy-api','sha256sum','/CLIProxyAPI/config.yaml'],text=True).split()[0]
checks={'config_content_unchanged':cfg.read_text()==expected,'environment_only_image_changed':(root/'.env').read_text()==newenv,'config_mode_preserved':oct(cfg.stat().st_mode&0o777)==json.loads((backup/'receipt.json').read_text())['files']['config.yaml']['mode'],'image_matches_contract':c['Image']==release['images'][arch],'loopback_ports_preserved':ports==expected_ports,'state_mounts_preserved':all(mounts.get(k)==v for k,v in expected_mounts.items()),'host_container_config_identical':hashlib.sha256(cfg.read_bytes()).hexdigest()==inside,'restart_always':c['HostConfig']['RestartPolicy']['Name']=='always','pprof_listener_absent':not listener,'no_home_bootstrap':not any(e.startswith('HOME_JWT=') for e in c['Config']['Env']) and not any('home-jwt' in x for x in (c['Config']['Cmd'] or []))}
baseline=json.loads((backup/'receipt.json').read_text())
all_ids=subprocess.check_output(['docker','ps','-aq'],text=True).split()
current=json.loads(subprocess.check_output(['docker','inspect',*all_ids],text=True)) if all_ids else []
current_by_name={item['Name']:item for item in current}
unrelated=[item for item in baseline['containers'] if item['name']!='/cli-proxy-api' and not item['name'].startswith('/cliproxy-v8066-')]
missing_unrelated=[item['name'] for item in unrelated if item['name'] not in current_by_name]
checks['existing_unrelated_containers_unchanged']=all(current_by_name[item['name']]['Id']==item['id'] and current_by_name[item['name']]['Image']==item['image'] for item in unrelated if item['name'] in current_by_name)
checks['configuration_inode_preserved']=cfg.stat().st_ino==json.loads((root/'ops/runtime/upgrade-v8.0.66-20261110'/('activation-'+arch+'.json')).read_text())['config_inode']
checks['cursor_plugin_unchanged']=hashlib.sha256((root/'plugins/linux'/arch/release['cursor_plugin']['libraries'][arch]['filename']).read_bytes()).hexdigest()==release['cursor_plugin']['libraries'][arch]['sha256']
print(json.dumps({'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'arch':arch,'container_id':c['Id'],'started_at':c['State']['StartedAt'],'unrelated_container_count':len(unrelated),'missing_unrelated_containers':missing_unrelated,'checks':checks,'passed':all(checks.values())},indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
