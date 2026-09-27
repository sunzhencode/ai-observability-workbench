from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    shutil.copy(ROOT / 'start.sh', tmp_path / 'start.sh')
    for directory in ['.venv/bin', 'backend/data', 'operations-console/node_modules', 'operations-console/dist', 'bin']:
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / 'backend/data/master.key').write_text('synthetic-test-key')
    (tmp_path / 'operations-console/dist/index.html').write_text('fixture')
    python = str(ROOT / '.venv/bin/python')
    commands = {
        '.venv/bin/python': f'''#!/bin/bash
if [ "$1" = "-m" ]; then echo backend-failed >&2; exit 7; fi
if [[ "$*" == *"os.setsid"* ]]; then shift 2; exec "$@"; fi
exec {python} "$@"
''',
        'bin/npm': '#!/bin/bash\nexit 0\n',
        'bin/lsof': '#!/bin/bash\nexit 0\n',
        'bin/curl': '#!/bin/bash\nexit 0\n',
    }
    for name, content in commands.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith('INCIDENT_OPERATIONS_')}
    env['PATH'] = f"{tmp_path / 'bin'}:{env['PATH']}"
    return tmp_path / 'start.sh', env


def test_backend_failure_is_not_success(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    result = subprocess.run(['bash', str(script), '--configured'], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'backend-failed' in result.stderr


def test_help_has_no_execution_text_or_legacy_entrypoint(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    result = subprocess.run(['bash', str(script), '--help'], env=env, capture_output=True, text=True, check=True)
    assert 'set -euo' not in result.stdout
    assert '--validate-monitoring' in result.stdout
    assert not (ROOT / 'start-operations-console.sh').exists()
    assert not (ROOT / 'scripts/local_monitoring_stack.sh').exists()


def test_env_file_is_data_and_process_values_win(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    (tmp_path / 'backend/.env').write_text('INCIDENT_OPERATIONS_PORT=8112\nINCIDENT_OPERATIONS_DATABASE_PATH=data/example.db\n')
    env['INCIDENT_OPERATIONS_PORT'] = '8113'
    result = subprocess.run(['bash', str(script), '--configured'], env=env, capture_output=True, text=True, timeout=15)
    assert '8113' in result.stdout
    assert str(tmp_path / 'data/example.db') in result.stdout


def test_conflicting_modes_are_rejected(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    result = subprocess.run(['bash', str(script), '--mock', '--configured'], env=env, capture_output=True, text=True)
    assert result.returncode == 2


@pytest.mark.parametrize("foreign", [False, True])
def test_monitoring_failure_respects_launch_ownership(tmp_path: Path, foreign: bool) -> None:
    script, env = _fixture(tmp_path)
    state = tmp_path / 'containers'
    network = tmp_path / 'network'
    docker = tmp_path / 'bin/docker'
    docker.write_text(f'''#!{ROOT / '.venv/bin/python'}
import sys,json
from pathlib import Path
state=Path({str(state)!r});network=Path({str(network)!r});tmp=state.parent;a=sys.argv[1:]
for value in a:
 if value.startswith('com.ai-observability-workbench.launch-id='):(tmp / 'launch').write_text(value.split('=',1)[1])
items=json.loads(state.read_text()) if state.exists() else []
if a[0]=='info':sys.exit(0)
if a[:2]==['container','inspect']:
 if a[-1] not in items:sys.exit(1)
 print((tmp / 'launch').read_text() if 'launch-id' in ' '.join(a) else 'true');sys.exit(0)
if a[:2]==['network','inspect']:
 if not network.exists():sys.exit(1)
 print((tmp / 'launch').read_text() if 'launch-id' in ' '.join(a) else 'true');sys.exit(0)
if a[:2]==['network','create']:network.touch();sys.exit(0)
if a[:2]==['network','rm']:network.unlink();sys.exit(0)
if a[0]=='run':
 items.append(a[a.index('--name')+1])
 if {foreign!r}:
  state.write_text(json.dumps(items));(tmp / 'launch').write_text('foreign-launch');sys.exit(1)
if a[0]=='rm':items.remove(a[-1])
state.write_text(json.dumps(items))
''')
    docker.chmod(0o755)
    (tmp_path / 'bin/curl').write_text('#!/bin/bash\nexit 1\n')
    sleep = tmp_path / 'bin/sleep'
    sleep.write_text('#!/bin/bash\nexit 0\n')
    sleep.chmod(0o755)
    result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    if foreign:
        assert 'awo-local-alertmanager' in state.read_text()
        assert network.exists()
    else:
        assert 'did not become ready' in result.stderr
        assert state.read_text() == '[]'
        assert not network.exists()


def test_env_file_does_not_execute_shell(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    marker = tmp_path / 'must-not-exist'
    (tmp_path / 'backend/.env').write_text(f'INCIDENT_OPERATIONS_PORT=$(touch {marker})\n')
    result = subprocess.run(['bash', str(script), '--configured'], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize('signal_name,exit_code', [('SIGINT', 130), ('SIGTERM', 143), ('SIGHUP', 129)])
def test_shutdown_signals_stop_owned_process_group(tmp_path: Path, signal_name: str, exit_code: int) -> None:
    import signal
    import time

    script, env = _fixture(tmp_path)
    child_pid = tmp_path / 'child-pid'
    wrapper = tmp_path / '.venv/bin/python'
    wrapper.write_text(f'''#!/bin/bash
if [ "$1" = "-m" ]; then echo $$ > {child_pid}; exec sleep 100; fi
exec {ROOT / '.venv/bin/python'} "$@"
''')
    output = tmp_path / 'output'
    with output.open('w') as stream:
        process = subprocess.Popen(['bash', str(script), '--configured'], env=env, stdout=stream, stderr=stream)
        try:
            for _ in range(100):
                if 'Ready.' in output.read_text() and child_pid.exists():
                    break
                assert process.poll() is None, output.read_text()
                time.sleep(.05)
            assert child_pid.exists(), output.read_text()
            process.send_signal(getattr(signal, signal_name))
            assert process.wait(timeout=15) == exit_code
            with pytest.raises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
