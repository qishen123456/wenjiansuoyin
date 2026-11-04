import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


spec = importlib.util.spec_from_file_location('restore_demo_data', Path(__file__).resolve().parents[2] / 'scripts' / 'restore_demo_data.py')
restore_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore_module)


def bundle(root, name='网盘模拟数据/业务域/日报.csv', link=False):
    assets = root / 'demo_assets'
    assets.mkdir()
    archive = assets / 'demo-data.tar.gz'
    content = '月份,金额\n11,100\n'.encode()
    with tarfile.open(archive, 'w:gz') as tar:
        item = tarfile.TarInfo(name)
        if link:
            item.type = tarfile.SYMTYPE
            item.linkname = '/tmp/elsewhere'
            tar.addfile(item)
        else:
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
    (assets / 'manifest.json').write_text(json.dumps({
        'file_count': 0 if link else 1,
        'total_bytes': 0 if link else len(content),
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
    }))
    return archive


def test_restores_chinese_paths_and_preserves_existing_data(tmp_path, monkeypatch):
    bundle(tmp_path)
    monkeypatch.setattr(restore_module, 'ROOT', tmp_path)
    restore_module.restore()
    restored = tmp_path / '网盘模拟数据/业务域/日报.csv'
    assert '金额' in restored.read_text()
    restored.write_text('本地修改')
    with pytest.raises(SystemExit, match='已存在'):
        restore_module.restore()
    assert restored.read_text() == '本地修改'


@pytest.mark.parametrize('name,link', [('../escape.csv', False), ('网盘模拟数据/../../escape.csv', False), ('网盘模拟数据/link', True)])
def test_rejects_unsafe_archive_members(tmp_path, monkeypatch, name, link):
    bundle(tmp_path, name, link)
    monkeypatch.setattr(restore_module, 'ROOT', tmp_path)
    with pytest.raises(SystemExit):
        restore_module.restore()
    assert not (tmp_path / '网盘模拟数据').exists()
    assert not list(tmp_path.glob('.demo-restore-*'))


def test_rejects_checksum_mismatch(tmp_path, monkeypatch):
    archive = bundle(tmp_path)
    archive.write_bytes(archive.read_bytes() + b'changed')
    monkeypatch.setattr(restore_module, 'ROOT', tmp_path)
    with pytest.raises(SystemExit, match='校验失败'):
        restore_module.restore()
