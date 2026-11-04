"""Restore the bundled demo dataset without overwriting an existing directory."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import shutil
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def restore():
    archive = ROOT / 'demo_assets' / 'demo-data.tar.gz'
    metadata = json.loads(archive.with_name('manifest.json').read_text(encoding='utf-8'))
    target = ROOT / '网盘模拟数据'
    if target.exists():
        raise SystemExit('网盘模拟数据目录已存在，未覆盖。请备份或移走该目录后再还原。')
    digest = hashlib.sha256()
    with archive.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != metadata['archive_sha256']:
        raise SystemExit('压缩包校验失败，未还原。')
    temporary = Path(tempfile.mkdtemp(prefix='.demo-restore-', dir=ROOT))
    try:
        with tarfile.open(archive, 'r:gz') as bundle:
            members = bundle.getmembers()
            files = [member for member in members if member.isfile()]
            if len(files) != metadata['file_count'] or sum(m.size for m in files) != metadata['total_bytes']:
                raise SystemExit('文件数量或大小与清单不符，未还原。')
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != '网盘模拟数据':
                    raise SystemExit('压缩包包含非法路径，未还原。')
                if not (member.isfile() or member.isdir()) or '\\' in member.name:
                    raise SystemExit('压缩包包含链接或特殊文件，未还原。')
            # Explicit validation also supports Python versions without extraction filters.
            bundle.extractall(temporary, members=members)
        (temporary / '网盘模拟数据').rename(target)
    finally:
        shutil.rmtree(temporary)
    print(f'已还原 {metadata["file_count"]:,} 个模拟文件：{target}')


if __name__ == '__main__':
    restore()
