"""Run the real Linux entrypoints; all service/dependency effects are sandbox shims.

These are Linux process/filesystem contract tests, NOT Ubuntu/systemd acceptance.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

pwd = pytest.importorskip("pwd", reason="Linux-only service account tests")

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux entrypoint contract tests')
REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts/linux'
OLD = 'v0.11.14-' + 'a' * 12
NEW = 'v0.11.15-' + 'b' * 12


def executable(path, content):
    path.write_text('#!' + sys.executable + '\n' + content)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path):
    root = tmp_path / 'install 中文 space'
    root.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    unit = tmp_path / 'systemd'; unit.mkdir()
    rotate = tmp_path / 'logrotate'; rotate.mkdir()
    trace = tmp_path / 'calls.jsonl'
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'active': False, 'enabled': 'disabled'}))
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
               TEST_ROOT=str(root), TEST_STATE=str(state), TEST_TRACE=str(trace),
               TEST_ONCE=str(tmp_path / 'failed-once'), TEST_UNIT=str(unit), TEST_ROTATE=str(rotate),
               PYTHONPATH=str(tmp_path) + os.pathsep + str(SCRIPTS))
    executable(bin_dir / 'systemctl', '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]; cmd=args[0]
p=Path(os.environ['TEST_STATE']); state=json.loads(p.read_text())
with open(os.environ['TEST_TRACE'],'a') as f: f.write(json.dumps(['systemctl',*args])+'\\n')
if cmd=='show': print('loaded' if (Path(os.environ['TEST_UNIT'])/'oopz-capture.service').exists() else 'not-found'); sys.exit(0)
if cmd=='is-active': print('active' if state['active'] else 'inactive'); sys.exit(0 if state['active'] else 3)
if cmd=='is-enabled': print(state['enabled']); sys.exit(0 if state['enabled']=='enabled' else 1)
if cmd=='stop': state['active']=False
if cmd=='disable': state['enabled']='disabled'
if cmd=='enable': state['enabled']='enabled'
if cmd=='start': state['active']=True
if cmd=='daemon-reload' and os.environ.get('TEST_OLD_STATIC') and (Path(os.environ['TEST_UNIT'])/'oopz-capture.service').read_bytes().startswith(b'# actual'): state['enabled']='static'
p.write_text(json.dumps(state))
marker=Path(os.environ['TEST_ONCE'])
if os.environ.get('TEST_FAIL_CMD')==cmd and not marker.exists(): marker.touch(); sys.exit(17)
if cmd=='start' and os.environ.get('TEST_SIGNAL') and not marker.exists():
 import signal
 marker.touch(); os.kill(os.getppid(),signal.SIGTERM)
if cmd=='start' and not os.environ.get('TEST_NO_HEALTH'):
 log=Path(os.environ['TEST_ROOT'])/'shared/logs/feishu_runtime.log'; log.parent.mkdir(parents=True,exist_ok=True)
 with log.open('a') as f: f.write('飞书长连接已就绪\\n')
sys.exit(0)
''')
    fake_python = '''import json,os,sys
from pathlib import Path
with open(os.environ['TEST_TRACE'],'a') as f: f.write(json.dumps(['venv-python',*sys.argv[1:]])+'\\n')
assert Path(os.environ['OOPZ_ENV_FILE']).is_file()
assert (Path.cwd()/'.env').resolve()==Path(os.environ['OOPZ_ENV_FILE']).resolve()
if sys.argv[-1:] == ['setup']:
 with open(os.environ['OOPZ_ENV_FILE'],'a') as f: f.write('SETUP_VALUE="中文 $! \\"quoted\\""\\n')
if os.environ.get('TEST_FAIL_DEPS') and sys.argv[1:3]==['-m','pip']: sys.exit(29)
'''
    executable(bin_dir / 'python3.12', f'''import os,sys,json
from pathlib import Path
assert sys.argv[1:3]==['-m','venv']
p=Path(sys.argv[3])/'bin'; p.mkdir(parents=True,exist_ok=True)
f=p/'python'; f.write_text({('#!' + sys.executable + chr(10) + fake_python)!r}); f.chmod(0o755)
''')
    for name in ('node', 'npm'):
        executable(bin_dir / name, '''import os,sys,json
with open(os.environ['TEST_TRACE'],'a') as f: f.write(json.dumps([os.path.basename(sys.argv[0]),*sys.argv[1:]])+'\\n')
print('v22.12.0')
''')
    # File-operation injection imports the REAL transaction helper before entrypoint.
    (tmp_path / 'sitecustomize.py').write_text('''import os
if os.environ.get('TEST_FAIL_FILE'):
 import release_transaction as t
 from pathlib import Path
 original=t.atomic_write; link=t.replace_link
 def fail(path,data,mode=0o644):
  kind=os.environ['TEST_FAIL_FILE']
  matched=(kind=='unit' and path.name=='oopz-capture.service') or (kind=='rotation' and path.name=='oopz-capture')
  marker=Path(os.environ['TEST_ONCE'])
  if matched and (not marker.exists() or os.environ.get('TEST_FAIL_ALWAYS')):
   marker.touch(); path.write_bytes(b'partial write'); raise OSError('injected partial file write')
  return original(path,data,mode)
 def fail_link(path,target):
  marker=Path(os.environ['TEST_ONCE'])
  if os.environ['TEST_FAIL_FILE']=='current' and not marker.exists():
   marker.touch(); path.unlink(missing_ok=True); raise OSError('injected current replacement failure')
  return link(path,target)
 original_replace=t.os.replace; original_symlink=t.os.symlink
 def injected_replace(source,destination):
  marker=Path(os.environ['TEST_ONCE'])
  if os.environ['TEST_FAIL_FILE']=='replace' and Path(destination).name=='current' and not marker.exists():
   marker.touch(); raise OSError('injected os.replace failure')
  return original_replace(source,destination)
 def injected_symlink(target,link,*args,**kwargs):
  marker=Path(os.environ['TEST_ONCE'])
  if os.environ['TEST_FAIL_FILE']=='symlink' and Path(link).name.startswith('.current-') and not marker.exists():
   marker.touch(); raise OSError('injected os.symlink failure')
  return original_symlink(target,link,*args,**kwargs)
 t.os.replace=injected_replace; t.os.symlink=injected_symlink
 t.atomic_write=fail; t.replace_link=fail_link
''')
    return dict(root=root, bin=bin_dir, unit=unit, rotate=rotate, state=state, env=env, trace=trace, tmp=tmp_path)


def artifact(host, *, missing=None, extra=None):
    path = host['tmp'] / 'release.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        files = list(SCRIPTS.glob('*')) + [REPO / '.env.example', REPO / 'pyproject.toml', REPO / 'scripts/download_sensevoice_model.py']
        for file in files:
            if file.is_file():
                name = str(file.relative_to(REPO))
                if name != missing:
                    archive.writestr(name, file.read_bytes())
        archive.writestr('RELEASE_MANIFEST.json', json.dumps({'release_id': NEW, 'git_commit': 'b' * 40}))
        if extra:
            archive.writestr(*extra)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def call(host, command, *args, env=None):
    wrapper = 'install_release.sh'
    argv = [command]
    if command == 'update': wrapper, argv = 'update_release.sh', []
    if command == 'rollback': wrapper, argv = 'rollback_release.sh', []
    return subprocess.run(['bash', str(SCRIPTS / wrapper), *argv, '--root', str(host['root']),
                           '--user', pwd.getpwuid(os.getuid()).pw_name,
                           '--unit-dir', str(host['unit']), '--logrotate-dir', str(host['rotate']),
                           '--health-timeout', '.03', *args], env=env or host['env'], capture_output=True, text=True, timeout=20)


def prepare(host):
    archive, digest = artifact(host)
    result = call(host, 'prepare', '--artifact', str(archive), '--sha256', digest)
    assert result.returncode == 0, result.stderr
    return archive, digest


def old_install(host, active=True, enabled='enabled'):
    root=host['root']; old=root/'releases'/OLD
    old.mkdir(parents=True,exist_ok=True)
    (root/'current').symlink_to(old)
    (host['unit']/'oopz-capture.service').write_bytes(b'# actual hand-customized old unit\n')
    (host['unit']/'oopz-capture.service').chmod(0o640)
    (host['rotate']/'oopz-capture').write_bytes(b'# actual hand-customized rotation\n')
    host['state'].write_text(json.dumps({'active':active,'enabled':enabled}))
    if enabled=='static': host['env']['TEST_OLD_STATIC']='1'
    return old


def assert_restored(host, old, active=True, enabled='enabled'):
    assert (host['root']/'current').resolve()==old
    assert (host['unit']/'oopz-capture.service').read_bytes()==b'# actual hand-customized old unit\n'
    assert (host['unit']/'oopz-capture.service').stat().st_mode & 0o777==0o640
    assert (host['rotate']/'oopz-capture').read_bytes()==b'# actual hand-customized rotation\n'
    assert json.loads(host['state'].read_text())==dict(active=active,enabled=enabled)
    assert not (host['root']/'.switch-journal.json').exists()


def test_prepare_setup_activate_preserves_shared_config(host):
    prepare(host)
    assert not (host['root']/'current').exists()
    release=host['root']/'releases'/NEW
    assert (release/'.env').is_symlink()
    result=call(host,'setup','--release-id',NEW)
    assert result.returncode==0,result.stderr
    config=(host['root']/'shared/config/.env').read_bytes()
    assert b'SETUP_VALUE' in config
    result=call(host,'activate','--release-id',NEW,'--enable')
    assert result.returncode==0,result.stderr
    assert (release/'.env').read_bytes()==config
    assert (host['root']/'current').resolve()==release
    calls=host['trace'].read_text()
    assert 'venv-python' in calls and 'playwright' in calls and 'npm' in calls


def test_u1_update_runs_real_installer_with_all_helpers(host):
    old_install(host)
    archive,digest=artifact(host)
    result=call(host,'update','--artifact',str(archive),'--sha256',digest)
    assert result.returncode==0,result.stderr
    assert (host['root']/'current').resolve().name==NEW
    assert (host['root']/'releases'/NEW/'DEPLOYED_PYTHON_PACKAGES.txt').is_file()
    assert not list((host['root']/'artifacts').glob('.verified-*'))


@pytest.mark.parametrize('missing',['scripts/linux/release_transaction.py','scripts/linux/release_locks.py','scripts/linux/release_archive.py','scripts/linux/prepare_dependencies.sh'])
def test_missing_helper_rejected_before_any_service_or_dependency(host,missing):
    archive,digest=artifact(host,missing=missing)
    result=call(host,'update','--artifact',str(archive),'--sha256',digest)
    assert result.returncode!=0 and 'helper' in result.stderr
    assert not host['trace'].exists()


@pytest.mark.parametrize('bad_entry',['../escape','/absolute','scripts/../../escape','logs/private','scripts/linux/.prepared.json'])
def test_unsafe_archive_rejected(host,bad_entry):
    archive,digest=artifact(host,extra=(bad_entry,'unsafe'))
    result=call(host,'prepare','--artifact',str(archive),'--sha256',digest)
    assert result.returncode!=0 and not host['trace'].exists()


def test_hash_mismatch_never_executes_archive(host):
    archive,_=artifact(host)
    result=call(host,'update','--artifact',str(archive),'--sha256','0'*64)
    assert result.returncode!=0 and not host['trace'].exists()


def test_failed_prepare_same_package_retry_does_not_touch_current(host):
    old=old_install(host)
    archive,digest=artifact(host)
    result=call(host,'prepare','--artifact',str(archive),'--sha256',digest,env=dict(host['env'],TEST_FAIL_DEPS='1'))
    assert result.returncode!=0
    assert_restored(host,old)
    result=call(host,'prepare','--artifact',str(archive),'--sha256',digest)
    assert result.returncode==0,result.stderr
    assert_restored(host,old)


@pytest.mark.parametrize('failure',['stop','daemon-reload','enable','start','health','unit','rotation','current'])
@pytest.mark.parametrize('active,enabled',[(True,'enabled'),(False,'disabled'),(False,'static')])
def test_u2_restores_actual_files_current_active_enabled_on_every_failure(host,failure,active,enabled):
    old=old_install(host,active,enabled)
    prepare(host)
    env=dict(host['env'])
    if failure in ('unit','rotation','current'): env['TEST_FAIL_FILE']=failure
    elif failure=='health': env['TEST_NO_HEALTH']='1'
    else: env['TEST_FAIL_CMD']=failure
    result=call(host,'activate','--release-id',NEW,'--enable',env=env)
    assert result.returncode!=0,result.stdout
    assert_restored(host,old,active,enabled)


def test_recovery_failure_leaves_stopped_and_journal_then_explicit_recover(host):
    old=old_install(host)
    prepare(host)
    result=call(host,'activate','--release-id',NEW,env=dict(host['env'],TEST_FAIL_FILE='unit',TEST_FAIL_ALWAYS='1'))
    assert result.returncode!=0 and 'RECOVERY FAILED' in result.stderr
    assert not json.loads(host['state'].read_text())['active']
    assert (host['root']/'.switch-journal.json').is_file()
    assert call(host,'activate','--release-id',NEW).returncode!=0
    result=call(host,'recover')
    assert result.returncode==0,result.stderr
    assert_restored(host,old)


@pytest.mark.parametrize('value',['broken','{}','[]','{"pid":0}','{"pid":-1}','{"pid":true}','{"pid":"123"}','{"pid":2147483648}'])
def test_u3_invalid_locks_fail_closed_without_stopping(host,value):
    old=old_install(host)
    prepare(host)
    lock=host['root']/'shared/output/.run.lock';lock.write_text(value)
    before=host['trace'].read_text()
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode!=0 and 'lock' in result.stderr
    assert host['trace'].read_text()==before
    assert_restored(host,old)


@pytest.mark.parametrize('kind',['live','symlink','directory','fifo'])
def test_u3_unsafe_or_live_locks_refuse_switch(host,kind):
    old=old_install(host);prepare(host)
    lock=host['root']/'shared/output/.run.lock'
    if kind=='live': lock.write_text(json.dumps({'pid':os.getpid()}))
    elif kind=='symlink': lock.symlink_to(host['tmp']/'missing')
    elif kind=='directory': lock.mkdir()
    else: os.mkfifo(lock)
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode!=0
    assert_restored(host,old)


def test_valid_dead_lock_allows_switch_but_is_not_deleted(host):
    prepare(host)
    process=subprocess.Popen([sys.executable,'-c','pass']);process.wait()
    lock=host['root']/'shared/output/.run.lock';lock.write_text(json.dumps({'pid':process.pid}))
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode==0,result.stderr
    assert lock.exists()


def test_force_is_explicit_bypass_for_invalid_lock(host):
    prepare(host)
    (host['root']/'shared/output/.run.lock').write_text('invalid')
    result=call(host,'activate','--release-id',NEW,'--force')
    assert result.returncode==0,result.stderr


def test_active_recording_lifecycle_refuses_switch(host):
    prepare(host)
    (host['root']/'shared/output/lifecycle.json').write_text('{"status":"recording"}')
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode!=0 and 'active' in result.stderr


@pytest.mark.parametrize('failure',['unit','rotation','current','daemon-reload','enable','start','health'])
def test_first_install_failure_restores_absent_files_and_stopped_disabled_state(host,failure):
    prepare(host)
    env=dict(host['env'])
    if failure in ('unit','rotation','current'): env['TEST_FAIL_FILE']=failure
    elif failure=='health': env['TEST_NO_HEALTH']='1'
    else: env['TEST_FAIL_CMD']=failure
    result=call(host,'activate','--release-id',NEW,'--enable',env=env)
    assert result.returncode!=0
    assert not os.path.lexists(host['root']/'current')
    assert not (host['unit']/'oopz-capture.service').exists()
    assert not (host['rotate']/'oopz-capture').exists()
    assert not json.loads(host['state'].read_text())['active']
    assert not (host['root']/'.switch-journal.json').exists(),result.stderr


def test_old_ready_log_does_not_satisfy_new_health_check(host):
    old=old_install(host);prepare(host)
    (host['root']/'shared/logs/feishu_runtime.log').write_text('飞书长连接已就绪\n')
    result=call(host,'activate','--release-id',NEW,env=dict(host['env'],TEST_NO_HEALTH='1'))
    assert result.returncode!=0 and 'Timed out' in result.stderr
    assert_restored(host,old)


def test_preparing_current_release_is_refused_without_changes(host):
    archive,digest=prepare(host)
    assert call(host,'activate','--release-id',NEW).returncode==0
    before=(host['root']/'releases'/NEW/'.prepared.json').read_bytes()
    result=call(host,'prepare','--artifact',str(archive),'--sha256',digest)
    assert result.returncode!=0 and 'current' in result.stderr
    assert (host['root']/'releases'/NEW/'.prepared.json').read_bytes()==before


def test_rollback_entrypoint_uses_prepared_release_and_preserves_data(host):
    prepare(host)
    assert call(host,'activate','--release-id',NEW).returncode==0
    root=host['root']; current=root/'releases'/NEW; old=root/'releases'/OLD
    shutil.copytree(current,old,symlinks=True)
    data=root/'shared/output/keep.txt';data.write_text('persistent')
    result=call(host,'rollback','--release-id',OLD)
    assert result.returncode==0,result.stderr
    assert (root/'current').resolve()==old
    assert data.read_text()=='persistent'


def test_unsafe_journal_refuses_without_external_actions(host):
    prepare(host)
    journal=host['root']/'.switch-journal.json'
    journal.write_text(json.dumps({'files':{str(host['tmp']/'outside'): {'kind':'absent'}},'state':{'active':False,'enabled':'disabled'}}))
    journal.chmod(0o600)
    before=host['trace'].read_text()
    result=call(host,'recover')
    assert result.returncode!=0 and 'paths' in result.stderr
    assert host['trace'].read_text()==before


def test_non_shared_output_override_rejected_before_stopping(host):
    prepare(host)
    with (host['root']/'shared/config/.env').open('a') as stream:
        stream.write('\nOOPZ_OUTPUT_ROOT=/outside/output\n')
    before=host['trace'].read_text()
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode!=0 and 'shared deployment' in result.stderr
    assert host['trace'].read_text()==before


@pytest.mark.parametrize('operation',['replace','symlink'])
def test_real_file_syscall_failure_rolls_back(host,operation):
    old=old_install(host);prepare(host)
    result=call(host,'activate','--release-id',NEW,env=dict(host['env'],TEST_FAIL_FILE=operation))
    assert result.returncode!=0
    assert_restored(host,old)


def test_sigterm_during_switch_restores_transaction(host):
    old=old_install(host);prepare(host)
    result=call(host,'activate','--release-id',NEW,env=dict(host['env'],TEST_SIGNAL='1'))
    assert result.returncode!=0
    assert_restored(host,old)


def test_prepared_node_is_used_in_service_and_editable_dependency_call(host):
    prepare(host)
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode==0,result.stderr
    unit=(host['unit']/'oopz-capture.service').read_text()
    assert f'OOPZ_NODE_PATH={host["bin"]}/node' in unit
    assert f'PATH={host["bin"]}:' in unit
    calls=[json.loads(line) for line in host['trace'].read_text().splitlines()]
    assert ['venv-python','-m','pip','install','-e','.[speech,feishu]'] in calls
    marker=json.loads((host['root']/'releases'/NEW/'.prepared.json').read_text())
    assert marker['node_path']==str(host['bin']/'node')


def test_unreadable_task_subdirectory_fails_closed(host):
    if os.getuid()==0:
        pytest.skip('Permission denial requires unprivileged test process')
    old=old_install(host);prepare(host)
    restricted=host['root']/'shared/output/restricted';restricted.mkdir()
    (restricted/'.run.lock').write_text(json.dumps({'pid':os.getpid()}))
    restricted.chmod(0)
    try:
        result=call(host,'activate','--release-id',NEW)
        assert result.returncode!=0 and 'inspect task tree' in result.stderr
        assert_restored(host,old)
    finally:
        restricted.chmod(0o700)


def test_fifo_task_state_refuses_without_blocking(host):
    prepare(host)
    os.mkfifo(host['root']/'shared/output/lifecycle.json')
    result=call(host,'activate','--release-id',NEW)
    assert result.returncode!=0 and 'Non-regular task state' in result.stderr
