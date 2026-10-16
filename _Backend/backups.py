"""Consistent SQLite snapshots, bounded retention and non-destructive restore drills."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import database
from config import BASE_DIR


def folder():
    path = Path(os.environ.get('MD57_BACKUP_DIR', str(Path(BASE_DIR)/'backups'/'automated'))).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    try:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Database integrity validation failed.')
        counts = {}
        for table in ('users','reservations','reservations_archive','staff_attendance'):
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        return counts
    finally:
        conn.close()


def digest(path):
    with open(path,'rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def resolve(name):
    if not isinstance(name,str) or Path(name).name != name or not name.startswith('md57_auto_') or not name.endswith('.db'):
        raise ValueError('Choose a listed backup.')
    target = (folder()/name).resolve()
    if target.parent != folder() or not target.is_file():
        raise ValueError('Backup not found.')
    return target


def create_backup():
    target = folder()/('md57_auto_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db')
    source = database.get_db_connection()
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    counts = validate(target)
    meta = {'name':target.name,'created_at':datetime.now().isoformat(timespec='seconds'),
            'bytes':target.stat().st_size,'sha256':digest(target),'counts':counts}
    target.with_suffix('.json').write_text(json.dumps(meta),encoding='utf8')
    keep = max(2,min(90,int(os.environ.get('MD57_BACKUP_KEEP','14'))))
    # Only this module's own validated filenames in its resolved backup directory.
    for old in sorted(folder().glob('md57_auto_*.db'),key=lambda p:p.stat().st_mtime,reverse=True)[keep:]:
        old = resolve(old.name)
        old.unlink()
        old.with_suffix('.json').unlink(missing_ok=True)
    return meta


def list_backups():
    items=[]
    for path in sorted(folder().glob('md57_auto_*.json'),reverse=True):
        try:
            data=json.loads(path.read_text(encoding='utf8'))
            resolve(data['name'])
            items.append(data)
        except (ValueError,KeyError,OSError):
            continue
    return items


def restore_copy(name,target):
    source=resolve(name)
    metadata=json.loads(source.with_suffix('.json').read_text(encoding='utf8'))
    if digest(source)!=metadata['sha256']:
        raise ValueError('Backup checksum does not match.')
    validate(source)
    target=Path(target).resolve()
    if target.exists() or target==Path(database.DB_FILE).resolve():
        raise ValueError('Restore requires a new file, separate from the live database.')
    # Exclusive creation prevents accidental overwrites, including a racing caller.
    with source.open('rb') as src,target.open('xb') as dst:
        import shutil
        shutil.copyfileobj(src,dst)
    return validate(target)


def recovery_drill(name):
    with tempfile.TemporaryDirectory(prefix='md57_restore_') as temp:
        counts=restore_copy(name,Path(temp)/'restored.db')
    return {'verified':True,'counts':counts}


def scheduled_backup():
    items=list_backups()
    hours=max(1,int(os.environ.get('MD57_BACKUP_INTERVAL_HOURS','24')))
    if not items or (datetime.now()-datetime.fromisoformat(items[0]['created_at'])).total_seconds()>=hours*3600:
        result=create_backup()
        recovery_drill(result['name'])


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','list','verify','restore'])
    parser.add_argument('--name')
    parser.add_argument('--target')
    args=parser.parse_args()
    if args.action=='create': result=create_backup()
    elif args.action=='list': result=list_backups()
    elif args.action=='verify': result=recovery_drill(args.name)
    elif args.target: result=restore_copy(args.name,args.target)
    else: parser.error('restore requires --target pointing to a new file')
    print(json.dumps(result,indent=2))
