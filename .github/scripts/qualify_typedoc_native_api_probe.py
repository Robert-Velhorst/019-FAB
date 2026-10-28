"""Owned isolated hosted native API experiment; never full TypeDoc acceptance."""
import base64,datetime,hashlib,io,json,os,pathlib,re,shutil,subprocess,sys,tarfile,tempfile,urllib.parse,urllib.request
assert os.environ.get('GITHUB_REPOSITORY')=='Robert-Velhorst/019-FAB'
assert sys.platform=='linux' and os.uname().machine=='x86_64'
owner=pathlib.Path.cwd();out=owner/'native-api-experiment-evidence';out.mkdir();descriptor=json.loads((owner/'.github/qualification/typedoc-native-api-experiment.json').read_bytes())
def save(name,value):(out/name).write_bytes((json.dumps(value,indent=2)+'\n').encode())
def snapshot():
    rows={}
    for item in subprocess.check_output(['git','ls-files','-s','-z']).split(b'\0'):
        if not item:continue
        left,name=item.split(b'\t');mode,blob,stage=left.decode().split();assert stage=='0';p=owner/name.decode();raw=os.readlink(p).encode() if mode=='120000' else p.read_bytes();assert hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==blob
        rows[name.decode()]={'mode':mode,'blob':blob,'sha256':hashlib.sha256(raw).hexdigest()}
    return {'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],text=True).strip(),'payloads':rows}
before=snapshot();save('owner-source-before.json',before);temporary=pathlib.Path(tempfile.mkdtemp(prefix='owned-native-api-'));project=temporary/'project';project.mkdir()
package={'name':'owned-typescript-native-api-experiment','version':'1.0.0','private':True,'type':'module','engines':{'node':'>=26.12.1'},'dependencies':{'typescript':'7.0.2'}}
(project/'package.json').write_bytes((json.dumps(package,indent=2)+'\n').encode());source=owner/'.github/scripts/typedoc-native-api-contract.test.mjs';shutil.copyfile(source,project/'owned-native-api.test.mjs');case_hash=hashlib.sha256(source.read_bytes()).hexdigest();commands=[]
def run(label,args):
    with (out/(label+'.log')).open('wb') as stream:process=subprocess.run(args,cwd=project,stdout=stream,stderr=subprocess.STDOUT,timeout=180)
    commands.append({'label':label,'argv':args,'exit_code':process.returncode});save('commands.json',commands);return process.returncode
metadata=run('normal-strict-metadata',['npm','install','--package-lock-only','--ignore-scripts','--strict-peer-deps']);assert metadata==0
lock=json.loads((project/'package-lock.json').read_bytes());(out/'generated-package-lock.json').write_bytes((project/'package-lock.json').read_bytes());prereleases=[{'path':n,'version':r.get('version')} for n,r in lock['packages'].items() if n and re.search(r'^\d+\.\d+\.\d+-',r.get('version',''))];save('prerelease-records.json',prereleases);assert not prereleases and all(not r.get('link') for r in lock['packages'].values())
assert lock['packages']['node_modules/typescript']['version']=='7.0.2' and lock['packages']['node_modules/typescript']['integrity']==descriptor['typescript']['integrity']
assert run('normal-frozen-install',['npm','ci','--strict-peer-deps'])==0
assert run('complete-unfiltered-audit',['npm','audit','--json'])==0
assert run('complete-installed-graph',['npm','ls','--all','--json'])==0
sdk=project/'node_modules/typescript';expected={r['path']:r for r in descriptor['typescript']['source_payloads']};sdk_before={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected};assert sdk_before=={n:r['sha256'] for n,r in expected.items()};save('official-typescript-source-before.json',sdk_before)
platform_name='@typescript/typescript-linux-x64';platform=project/'node_modules'/platform_name
with urllib.request.urlopen('https://registry.npmjs.org/'+urllib.parse.quote(platform_name,safe='')+'/7.0.2',timeout=45) as stream:metadata_raw=stream.read(2*1024*1024+1)
assert len(metadata_raw)<=2*1024*1024;platform_metadata=json.loads(metadata_raw);assert platform_metadata['name']==platform_name and platform_metadata['version']=='7.0.2' and lock['packages']['node_modules/'+platform_name]['integrity']==platform_metadata['dist']['integrity']
with urllib.request.urlopen(platform_metadata['dist']['tarball'],timeout=60) as stream:archive=stream.read(64*1024*1024+1)
assert len(archive)<=64*1024*1024 and 'sha512-'+base64.b64encode(hashlib.sha512(archive).digest()).decode()==platform_metadata['dist']['integrity'];platform_payloads={}
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
    for member in tar.getmembers():
        assert member.isfile() or member.isdir()
        if not member.isfile():continue
        parts=pathlib.PurePosixPath(member.name).parts;assert len(parts)>1 and '..' not in parts and member.size<128*1024*1024;name='/'.join(parts[1:]);raw=tar.extractfile(member).read();digest=hashlib.sha256(raw).hexdigest();assert hashlib.sha256((platform/name).read_bytes()).hexdigest()==digest;platform_payloads[name]={'bytes':len(raw),'sha256':digest}
assert 'bin/tsgo' in platform_payloads
save('official-native-platform-before.json',{'package':platform_name,'version':'7.0.2','integrity':platform_metadata['dist']['integrity'],'archive_sha256':hashlib.sha256(archive).hexdigest(),'metadata_sha256':hashlib.sha256(metadata_raw).hexdigest(),'payloads':platform_payloads})
assert run('actual-native-binary-version',[str(platform/'bin/tsgo'),'--version'])==0
assert re.search(r'^Version 7\.0\.2\s*$',(out/'actual-native-binary-version.log').read_text(),re.M)
case_exit=run('owned-native-api-feature-cases',['node','--test','owned-native-api.test.mjs'])
assert hashlib.sha256((project/'owned-native-api.test.mjs').read_bytes()).hexdigest()==case_hash
if (project/'actual-native-api-surface.json').exists():(out/'actual-native-api-surface.json').write_bytes((project/'actual-native-api-surface.json').read_bytes())
sdk_after={n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in expected};assert sdk_before==sdk_after;save('official-typescript-source-after.json',sdk_after)
platform_after={n:{'bytes':(platform/n).stat().st_size,'sha256':hashlib.sha256((platform/n).read_bytes()).hexdigest()} for n in platform_payloads};assert platform_after==platform_payloads;save('official-native-platform-after.json',platform_after)
after=snapshot();assert before==after;save('owner-source-after.json',after)
log=(out/'owned-native-api-feature-cases.log').read_text();counts={}
for key in ['tests','pass','fail','skipped']:
    match=re.search(r'(?:#|ℹ) '+key+r' (\d+)\b',log);assert match;counts[key]=int(match.group(1))
save('qualification-summary.json',{'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'actual_node':subprocess.check_output(['node','--version'],text=True).strip(),'actual_npm':subprocess.check_output(['npm','--version'],text=True).strip(),'typescript_version':'7.0.2','official_base_payloads_verified':len(expected),'official_platform_payloads_verified':len(platform_payloads),'all_publisher_and_owner_bytes_unchanged':True,'owned_case_source_sha256':case_hash,'owned_case_results':counts,'owned_case_exit':case_exit,'prereleases':prereleases,'commands':commands,'publisher_explicitly_labels_native_API_unstable':True,'TypeDoc_original_SDK_or_production_adoption_qualified':False,'all_original_application_and_vendor_cases_helpers_commands_unchanged':True,'full_TypeScript_SDK_or_all_platform_matrix_qualified':False})
assert case_exit==0 and counts=={'tests':6,'pass':6,'fail':0,'skipped':0},'Native API feature failure is retained; no production/SDK compatibility is inferred.'
