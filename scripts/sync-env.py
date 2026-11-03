#!/usr/bin/env python3
"""Synchronize the Windows editing copy and the Linux VM .env without logging it."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REMOTE_PROGRAM = r'''
import base64, hashlib, json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

request = json.load(sys.stdin)
path = Path(request['path'])
try:
    current = path.read_bytes()
    digest = hashlib.sha256(current).hexdigest()
    if request['op'] == 'read':
        result = {'sha256': digest, 'data': base64.b64encode(current).decode('ascii')}
    elif request['op'] == 'stat':
        result = {'sha256': digest}
    elif request['op'] == 'write':
        if digest != request['expected']:
            raise RuntimeError('REMOTE_CHANGED')
        data = base64.b64decode(request['data'], validate=True)
        if not data.strip() or hashlib.sha256(data).hexdigest() != request['sha256']:
            raise RuntimeError('INVALID_PAYLOAD')
        backups = path.parent / 'state' / 'env-sync'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backups, 0o700)
        fd, backup = tempfile.mkstemp(prefix='vm-before-push-', suffix='.env', dir=backups)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())
        fd, pending = tempfile.mkstemp(prefix='.env-sync-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_bytes() != current:
                raise RuntimeError('REMOTE_CHANGED')
            os.replace(pending, path)
            os.chmod(path, 0o600)
        finally:
            Path(pending).unlink(missing_ok=True)
        result = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'backup': str(backup)}
    else:
        raise RuntimeError('INVALID_OPERATION')
    print(json.dumps(result))
except (OSError, ValueError, RuntimeError) as exc:
    code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    print(json.dumps({'error': code}))
    sys.exit(1)
'''


class SyncError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.env-sync-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def remote_call(host: str, remote: str, op: str, **payload: str) -> dict:
    request = {'path': remote, 'op': op, **payload}
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               host, 'python3 -c ' + shlex.quote(REMOTE_PROGRAM)]
    try:
        result = subprocess.run(command, input=json.dumps(request).encode('utf-8'),
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError('SSH unavailable or timed out; no synchronization confirmed.') from exc
    try:
        response = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncError(f'SSH failed (exit {result.returncode}); check the VM connection.') from exc
    if result.returncode or response.get('error'):
        raise SyncError('VM synchronization failed: ' + response.get('error', 'SSH_ERROR'))
    return response


def choose_direction(local_hash: str | None, remote_hash: str,
                     baseline: str | None, requested: str) -> str:
    if local_hash == remote_hash:
        return 'unchanged'
    if requested != 'auto':
        if requested == 'push' and local_hash is None:
            raise SyncError('Windows .env is missing; cannot push.')
        return requested
    if local_hash is None or (baseline is not None and local_hash == baseline):
        return 'pull'
    if baseline is not None and remote_hash == baseline:
        return 'push'
    raise SyncError('Both copies differ from the last sync, or no baseline exists. '
                    'Both files were preserved. Compare them, then choose --pull or --push.')


def synchronize(local: Path, host: str, remote: str, state_dir: Path,
                direction: str = 'auto', check: bool = False) -> dict:
    local = local.resolve()
    state_path = state_dir / 'last-sync.json'
    identity = {'host': host, 'remote': remote, 'local': str(local)}
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    baseline = state.get('sha256') if all(state.get(k) == v for k, v in identity.items()) else None
    current = local.read_bytes() if local.exists() else None
    local_hash = sha256(current) if current is not None else None
    snapshot = remote_call(host, remote, 'read')
    data = base64.b64decode(snapshot['data'], validate=True)
    remote_hash = snapshot['sha256']
    if not data.strip() or sha256(data) != remote_hash:
        raise SyncError('The VM snapshot is empty or invalid; no files were changed.')
    if check:
        return {'action': 'check', 'identical': local_hash == remote_hash,
                'local_sha256': local_hash, 'remote_sha256': remote_hash}
    action = choose_direction(local_hash, remote_hash, baseline, direction)
    backup = None
    if (local.read_bytes() if local.exists() else None) != current:
        raise SyncError('Windows .env changed during synchronization; retry after saving.')
    if remote_call(host, remote, 'stat')['sha256'] != remote_hash:
        raise SyncError('VM .env changed during synchronization; retry after saving.')
    if action == 'pull':
        if current is not None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = str(state_dir / f'windows-before-pull-{stamp}.env')
            atomic_write(Path(backup), current)
        atomic_write(local, data)
        expected = remote_hash
    elif action == 'push':
        if current is None or not current.strip():
            raise SyncError('Windows .env is empty or missing; no files were changed.')
        reply = remote_call(host, remote, 'write', expected=remote_hash,
                            data=base64.b64encode(current).decode('ascii'), sha256=local_hash)
        if reply['sha256'] != local_hash:
            raise SyncError('VM .env changed after writing; no synchronized baseline recorded.')
        backup = reply['backup']
        expected = local_hash
    else:
        expected = local_hash
    if sha256(local.read_bytes()) != expected or remote_call(host, remote, 'stat')['sha256'] != expected:
        raise SyncError('A copy changed during final verification; no synchronized baseline recorded.')
    record = {**identity, 'sha256': expected, 'synced_at': datetime.now(timezone.utc).isoformat()}
    atomic_write(state_path, (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return {'action': action, 'identical': True, 'sha256': expected, 'backup': backup,
            'local': str(local)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--pull', action='store_true', help='Use VM .env; back up Windows before replacing')
    mode.add_argument('--push', action='store_true', help='Use Windows .env; back up VM before replacing')
    mode.add_argument('--check', action='store_true', help='Compare only; do not modify files or baseline')
    parser.add_argument('--local', type=Path, default=ROOT / '.env')
    parser.add_argument('--host', default='wx@192.168.1.50')
    parser.add_argument('--remote', default='/home/wx/wechat-agent-linux/.env')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state' / 'env-sync')
    args = parser.parse_args()
    direction = 'pull' if args.pull else 'push' if args.push else 'auto'
    try:
        result = synchronize(args.local, args.host, args.remote, args.state_dir,
                             direction=direction, check=args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['identical'] else 2
    except (SyncError, OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, SyncError) else
              f'Configuration synchronization failed ({type(exc).__name__}).', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
