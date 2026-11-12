"""Recover a SQLite backup into a new file for offline cutover."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3

def recover(source, destination):
    if not source.is_file():
        raise ValueError('找不到备份')
    # Reserve only a new destination. Never replace a live database or its WAL files.
    if any(Path(str(destination)+suffix).exists() for suffix in ('-wal','-shm')):
        raise ValueError('目标位置已有数据库附属文件')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('备份完整性检查失败')
            for table in ('files','system_users','directory_grants','archive_rules','download_requests'):
                dst.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone()
            dst.execute('DELETE FROM login_sessions')
            dst.commit()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    recover(args.source,args.destination)
    print('已生成并验证恢复库。停服后可将 INDEX_DB_PATH 切换到该文件。')
