"""Remove exactly the zero-byte unclassified fill-in folders, retaining a recovery copy."""
import argparse
import json
from pathlib import Path
import shutil

NAME = '_未列入清单_1014全量扫描补齐'


def plan(root):
    result = []
    for directory in sorted(root.rglob(NAME)):
        if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
            raise ValueError(f'不安全的目录：{directory}')
        files = list(directory.rglob('*'))
        if any(p.is_symlink() or (p.is_file() and p.stat().st_size != 0) for p in files):
            raise ValueError(f'发现非空文件或链接，停止清理：{directory}')
        result.append({'directory':str(directory.relative_to(root)),'files':sum(p.is_file() for p in files)})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--quarantine',required=True)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    root, quarantine = Path(args.root).resolve(),Path(args.quarantine).resolve()
    if not root.is_dir() or root == Path('/') or quarantine.is_relative_to(root):
        raise ValueError('请指定有效演示根目录及根目录之外的恢复目录。')
    entries = plan(root)
    print(json.dumps({'directories':entries,'total_files':sum(item['files'] for item in entries)},ensure_ascii=False))
    if not args.apply:
        return
    if any((quarantine/item['directory']).exists() for item in entries):
        raise ValueError('恢复目录已有同名文件，不覆盖已有备份。')
    quarantine.mkdir(parents=True,exist_ok=True)
    manifest = {'root':str(root),'entries':entries,'moved':[]}
    for item in entries:
        destination = quarantine/item['directory']
        destination.parent.mkdir(parents=True,exist_ok=True)
        (quarantine/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        shutil.move(str(root/item['directory']),str(destination))
        manifest['moved'].append(item['directory'])
    (quarantine/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
