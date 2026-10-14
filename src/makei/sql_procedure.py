"""Catalog-validated build state for opt-in external .PROCEDURE targets."""
import hashlib
import json
import os
import socket
import sys
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from makei.utils import escape_special_chars


def catalog_job(iasp):
    # Keep the IBM i dependency lazy: ordinary projects do not need a catalog query.
    from makei.ibm_job import IBMJob
    job = IBMJob()
    if iasp:
        # Only change this query job's ASP group. Do not execute project setup
        # commands while preparing a build (which may be a dry run).
        job.run_cl("SETASPGRP ASPGRP('" + iasp.replace("'", "''") + "')")
    return job


def procedure_version(job, library, specific_name):
    """Identify one external procedure by library and SPECIFIC name, including overloads."""
    with closing(job.conn.cursor()) as cursor:
        cursor.execute(
            "SELECT R.ROUTINE_CREATED, R.LAST_ALTERED, R.EXTERNAL_NAME "
            "FROM QSYS2.SYSROUTINES R JOIN QSYS2.SYSSCHEMAS S "
            "ON R.SPECIFIC_SCHEMA = S.SCHEMA_NAME "
            "WHERE S.SYSTEM_SCHEMA_NAME = ? AND R.SPECIFIC_NAME = ? "
            "AND R.ROUTINE_TYPE = 'PROCEDURE' AND R.ROUTINE_BODY = 'EXTERNAL'",
            (library, specific_name))
        row = cursor.fetchone()
    return [str(value) if value is not None else None for value in row] if row else None


def state_directory(root, source_dir, library, iasp):
    """Do not share successful registration state across build destinations."""
    context = [socket.gethostname(), str(source_dir.resolve()), library.upper(), iasp.upper()]
    key = hashlib.sha256(json.dumps(context).encode('utf-8')).hexdigest()
    return root / '.deps' / 'procedures' / key


def invalidate_stamp(stamp, version):
    try:
        saved = json.loads(stamp.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        saved = None
    if version is None or saved != version:
        stamp.unlink(missing_ok=True)


def prepare_procedures(root, targets, directory_config, iasp):
    """Invalidate stale successes before GNU Make compares source/dependency timestamps."""
    job = catalog_job(iasp)
    paths = {}
    versions = {}
    for directory, names in targets.items():
        library = directory_config[directory].build['objlib']
        state_dir = state_directory(root, directory, library, iasp)
        paths[directory] = state_dir
        for target in names:
            specific = target.removesuffix('.PROCEDURE').replace('\\#', '#')
            specific = specific.replace('HASHESCAPE_', '#').replace('DOLLARESCAPE_', '$')
            key = (library, specific)
            if key not in versions:
                versions[key] = procedure_version(job, *key)
            invalidate_stamp(state_dir / escape_special_chars(target), versions[key])
    return paths


def record_success(job, library, specific_name, stamp):
    """Only record success after the expected registration is visible in the catalog."""
    version = procedure_version(job, library, specific_name)
    if version is None:
        raise RuntimeError(f"External procedure with SPECIFIC {specific_name} not found in {library}; "
                           "check the .PROCEDURE target and SQL SPECIFIC clause")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=stamp.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(version, file)
    try:
        os.replace(temporary, stamp)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    library, specific_name, stamp = sys.argv[1:]
    specific_name = specific_name.replace('HASHESCAPE_', '#').replace('DOLLARESCAPE_', '$')
    try:
        job = catalog_job(os.environ.get('iasp', ''))
        record_success(job, library, specific_name, Path(stamp))
    except Exception as error:
        print(f"Cannot record external procedure build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
