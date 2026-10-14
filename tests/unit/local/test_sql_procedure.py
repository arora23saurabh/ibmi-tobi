"""External registrations must be incremental without pretending to be *PGM objects."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from makei import sql_procedure
from makei.build import BuildEnv
from makei.const import TOBI_PATH
from makei.rules_mk import RulesMk


VERSION = ['2026-01-01 12:00:00', None, 'TESTLIB/SAMPLE']


def test_catalog_uses_specific_identity_and_parameters():
    job = MagicMock()
    cursor = job.conn.cursor.return_value
    cursor.fetchone.return_value = VERSION
    assert sql_procedure.procedure_version(job, 'TESTLIB', "O'NE") == VERSION
    statement, params = cursor.execute.call_args.args
    assert params == ('TESTLIB', "O'NE")
    assert "O'NE" not in statement
    assert 'SPECIFIC_NAME = ?' in statement
    assert "ROUTINE_BODY = 'EXTERNAL'" in statement
    cursor.close.assert_called_once()


def test_state_isolated_by_destination_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_procedure.socket, 'gethostname', lambda: 'host1')
    original = sql_procedure.state_directory(tmp_path, tmp_path, 'LIB1', '')
    assert original != sql_procedure.state_directory(tmp_path, tmp_path, 'LIB2', '')
    assert original != sql_procedure.state_directory(tmp_path, tmp_path, 'LIB1', 'IASP1')
    assert original != sql_procedure.state_directory(tmp_path, tmp_path / 'subdir', 'LIB1', '')
    monkeypatch.setattr(sql_procedure.socket, 'gethostname', lambda: 'host2')
    assert original != sql_procedure.state_directory(tmp_path, tmp_path, 'LIB1', '')


def test_validate_success_and_invalidate_catalog_changes(tmp_path, monkeypatch):
    stamp = tmp_path / 'SAMPLE.PROCEDURE'
    monkeypatch.setattr(sql_procedure, 'procedure_version', lambda *args: VERSION)
    sql_procedure.record_success(None, 'TESTLIB', 'SAMPLE', stamp)
    original_time = stamp.stat().st_mtime_ns
    sql_procedure.invalidate_stamp(stamp, VERSION)
    assert stamp.stat().st_mtime_ns == original_time
    sql_procedure.invalidate_stamp(stamp, [VERSION[0], '2026-02-01', VERSION[2]])
    assert not stamp.exists()
    stamp.write_text(json.dumps(VERSION))
    sql_procedure.invalidate_stamp(stamp, None)
    assert not stamp.exists()
    stamp.write_text('interrupted write')
    sql_procedure.invalidate_stamp(stamp, VERSION)
    assert not stamp.exists()


def test_missing_registration_cannot_record_success(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_procedure, 'procedure_version', lambda *args: None)
    stamp = tmp_path / 'SAMPLE.PROCEDURE'
    with pytest.raises(RuntimeError, match='SPECIFIC SAMPLE not found'):
        sql_procedure.record_success(None, 'TESTLIB', 'SAMPLE', stamp)
    assert not stamp.exists()


def test_prepare_validates_catalog_and_propagates_errors(tmp_path, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(sql_procedure, 'catalog_job', factory)
    lookup = MagicMock(return_value=VERSION)
    monkeypatch.setattr(sql_procedure, 'procedure_version', lookup)
    configs = {tmp_path: SimpleNamespace(build={'objlib': 'TESTLIB'})}
    paths = sql_procedure.prepare_procedures(
        tmp_path, {tmp_path: ['SAMPLE.PROCEDURE']}, configs, 'TESTASP')
    factory.assert_called_once_with('TESTASP')
    lookup.assert_called_once_with(factory.return_value, 'TESTLIB', 'SAMPLE')
    stamp = paths[tmp_path] / 'SAMPLE.PROCEDURE'
    assert not stamp.exists()  # Existing catalog row alone is not a successful TOBi build.
    lookup.side_effect = RuntimeError('catalog access denied')
    with pytest.raises(RuntimeError, match='catalog access denied'):
        sql_procedure.prepare_procedures(tmp_path, {tmp_path: ['SAMPLE.PROCEDURE']}, configs, '')


def test_catalog_job_only_sets_iasp(monkeypatch):
    factory = MagicMock()
    monkeypatch.setitem(sys.modules, 'makei.ibm_job', SimpleNamespace(IBMJob=factory))
    monkeypatch.setenv('IBMiEnvCmd', 'CALL SIDEFFECT')
    job = sql_procedure.catalog_job('TESTASP')
    job.run_cl.assert_called_once_with("SETASPGRP ASPGRP('TESTASP')")
    job.run_cl.reset_mock()
    sql_procedure.catalog_job('')
    job.run_cl.assert_not_called()


def test_rules_keep_program_and_registration_separate(tmp_path):
    for filename in ('sample.pgm.rpgle', 'sample.sqlprc'):
        (tmp_path / filename).touch()
    rules = RulesMk.from_str(
        'SAMPLE.PGM: sample.pgm.rpgle\nSAMPLE.PROCEDURE: sample.sqlprc SAMPLE.PGM\n', tmp_path, tmp_path)
    generated = str(rules)
    assert rules.targets['PGMs'] == ['SAMPLE.PGM']
    assert rules.targets['SQLs'] == ['SAMPLE.PROCEDURE']
    assert 'SAMPLE.PROCEDURE_DEP=SAMPLE.PGM' in generated
    assert 'SAMPLE.PROCEDURE_RECIPE=SQLPRC_TO_PROCEDURE_RECIPE' in generated
    legacy = RulesMk.from_str('SAMPLE.PGM: sample.sqlprc\n', tmp_path, tmp_path)
    assert 'SAMPLE.PGM_RECIPE=SQLPRC_TO_PGM_RECIPE' in str(legacy)
    wildcard = RulesMk.from_str('%.PROCEDURE: %.sqlprc\n', tmp_path, tmp_path)
    assert wildcard.targets['SQLs'] == ['SAMPLE.PROCEDURE']


def test_build_env_writes_procedure_search_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'iproj.json').write_text(json.dumps({'objlib': 'TESTLIB'}))
    (tmp_path / 'sample.sqlprc').touch()
    (tmp_path / 'Rules.mk').write_text('SAMPLE.PROCEDURE: sample.sqlprc\n')
    prepare = MagicMock(return_value={Path('.'): tmp_path / 'state'})
    monkeypatch.setattr(sql_procedure, 'prepare_procedures', prepare)
    build = BuildEnv()
    try:
        assert f'PROCEDUREPATH_{tmp_path} := {tmp_path / "state"}' in build.build_vars_path.read_text()
        assert prepare.call_args.args[1] == {Path('.'): ['SAMPLE.PROCEDURE']}
    finally:
        build._post_make()


def make_path(path):
    value = Path(path).resolve().as_posix()
    if os.name == 'nt':
        return '/' + value[0].lower() + value[2:]
    return value


@pytest.fixture
def make_tools():
    make = os.environ.get('TOBI_TEST_MAKE') or shutil.which('make')
    bash = os.environ.get('TOBI_TEST_BASH') or shutil.which('bash')
    if not make or not bash:
        pytest.skip('GNU Make and Bash are required for recipe integration tests')
    return make, bash


def test_real_make_incremental_registration(tmp_path, make_tools):
    """Run the actual Makefile/recipe; only IBM i command execution is stubbed."""
    make, bash = make_tools
    root = make_path(tmp_path)
    repo = make_path(TOBI_PATH)
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    stub = scripts / 'extractPseudoSQLAndLaunch'
    stub.write_bytes(
        b'#!/usr/bin/env bash\n'
        b'set -eu\n'
        b'stamp=${11}\n'
        b'rm -f "$stamp"\n'
        b'echo "$5" >> "${TEST_CALLS}"\n'
        b'[[ "${TEST_FAIL:-0}" = 0 ]] || exit 1\n'
        b'mkdir -p "$(dirname "$stamp")"\n'
        b'printf success > "$stamp"\n')
    stub.chmod(0o755)
    source = tmp_path / 'sample.sqlprc'
    source.write_text('CREATE OR REPLACE PROCEDURE SAMPLE ...;\n')
    (tmp_path / 'sample.pgm.rpgle').touch()
    objects = tmp_path / 'objects'
    objects.mkdir()
    program = objects / 'SAMPLE.PGM'
    program.touch()
    rules = RulesMk.from_str(
        f'SAMPLE.PGM: sample.pgm.rpgle\n\ttouch "{root}/objects/SAMPLE.PGM"\n'
        'SAMPLE.PROCEDURE: sample.sqlprc SAMPLE.PGM\n', tmp_path, tmp_path)
    (tmp_path / '.Rules.mk.build').write_text(str(rules))
    (tmp_path / 'buildvars').write_text(
        f'OBJPATH_{root} := {root}/objects\nPROCEDUREPATH_{root} := {root}/state\n')
    (tmp_path / 'custom.mk').write_text(f'SCRIPTSPATH := {root}/scripts\n')
    calls = tmp_path / 'calls'
    env = dict(os.environ, TEST_CALLS=make_path(calls))
    command = [make, '-f', f'{repo}/src/mk/Makefile', f'TOBI_PATH={repo}', f'TOP={root}',
               f'BUILDVARSMKPATH={root}/buildvars', f'TOBI_CUSTOM_MAKEFILE={root}/custom.mk',
               'SHELL=' + ('/bin/bash' if os.name == 'nt' else make_path(bash)),
               'IBMiRelease=750', 'SYS_ENCODING=utf-8', 'all']

    def run(fail=False):
        env['TEST_FAIL'] = '1' if fail else '0'
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert (result.returncode != 0) == fail, result.stdout + result.stderr
        return result

    run()
    assert calls.read_text().splitlines() == ['SAMPLE.PROCEDURE']
    run()
    assert len(calls.read_text().splitlines()) == 1
    stamp = tmp_path / 'state' / 'SAMPLE.PROCEDURE'
    # Deterministic older stamp avoids sleeps and filesystem timestamp resolution assumptions.
    os.utime(stamp, (1, 1))
    run()
    assert len(calls.read_text().splitlines()) == 2
    os.utime(source, (1, 1))
    os.utime(stamp, (2, 2))
    os.utime(program, (3, 3))
    run(fail=True)
    assert not stamp.exists()
    run()
    assert len(calls.read_text().splitlines()) == 4
    # Changing the destination uses another state directory and forces registration.
    (tmp_path / 'buildvars').write_text(
        f'OBJPATH_{root} := {root}/objects\nPROCEDUREPATH_{root} := {root}/other-state\n')
    run()
    assert len(calls.read_text().splitlines()) == 5


def test_failed_sql_removes_success_record(tmp_path, make_tools):
    _, bash = make_tools
    script = tmp_path / 'extractPseudoSQLAndLaunch'
    script.write_bytes((TOBI_PATH / 'src/scripts/extractPseudoSQLAndLaunch').read_text().encode('utf-8'))
    launch = tmp_path / 'launch'
    launch.write_bytes(b'#!/usr/bin/env bash\nexit 1\n')
    launch.chmod(0o755)
    stamp = tmp_path / 'SAMPLE.PROCEDURE'
    stamp.write_text(json.dumps(VERSION))
    source = tmp_path / 'sample.sqlprc'
    source.write_text('CREATE OR REPLACE PROCEDURE SAMPLE ...;\n')
    result = subprocess.run([bash, make_path(script), 'joblog', 'RUNSQLSTM', '', '', 'SAMPLE.PROCEDURE',
                             make_path(source), 'output', '', '', make_path(tmp_path / 'temp.sql'),
                             make_path(stamp)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not stamp.exists()
    assert not (tmp_path / 'temp.sql').exists()
