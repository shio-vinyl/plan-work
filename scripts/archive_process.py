"""过程文件归档：渲染图到期删除，旧的改稿目录和历史版本里的大文件打成整包压缩，文字文件留在原地。

  python3 -I archive_process.py plan  <项目目录>…   列出会删、会打包的文件和能省多少，不动文件
  python3 -I archive_process.py apply <项目目录>…   照 plan 执行：先打包、核对包内文件齐全，再删原文件
  python3 -I archive_process.py list  <归档.tar.zst>
  python3 -I archive_process.py extract <归档.tar.zst> [--match 文件名里的字] [--to 目录]

规则（门槛可用参数改）：
- 渲染目录（renders/、*.renders/）里最新的文件超过 7 天：整个目录删除，需要时重新渲染。
- 改稿目录（名字以 _YYYYMMDD 结尾，如 改稿_20261104/）日期和最新文件都超过 14 天：
  里面的 pptx、图片、PDF 等打成 _归档_<今天>.tar.zst 放在该目录里，md、py、json 等文字文件留在原地。
- 历史目录（历史版本/、历史/、_归档/）：每条汇报线（文件名去掉 _v 版本号和 _交付、_清备注 等后缀）
  留版本号最大的两版不动，更早且超过 14 天的大文件打成 归档_<今天>.tar.zst；大纲等文字文件留在原地。
- 不碰：项目根目录的文件、资料和成果目录（资料/、原始资料/、规划成果/、同事来稿/、原文/、研究稿/、专题稿/）、*.gdb、有 ~$ 锁文件的稿子。

每个包旁边写一份 <包名>_清单.md：包里有哪些文件、原大小、怎么解压。包内路径相对项目目录，
extract 默认解回原位置（已存在的文件不覆盖）。压缩用 zstd --long=31（多版稿子里相同的图片只存一份）；
没装 zstd 时退回 Python 自带的 xz，压得慢一些、省得少一些。
"""
import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tarfile

TEXT_EXT = {'.md', '.py', '.json', '.txt', '.csv', '.js', '.sh', '.html', '.htm', '.yaml', '.yml', '.toml'}
SOURCE_DIRS = {'资料', '原始资料', '规划成果', '同事来稿', '原文', '研究稿', '专题稿', '__pycache__'}   # 原件和成果不碰
HISTORY_DIRS = {'历史版本', '历史', '_归档'}
ROUND_DIR = re.compile(r'_(\d{8})$')
VERSION = re.compile(r'_v(\d+)(?=$|_)')
LINE_SUFFIX = re.compile(r'(_交付|_清备注|_工作稿|_去AI味|_去AI味版)+$')
ARCHIVE_NAME = re.compile(r'(^|_)归档_\d{8}\.tar\.(zst|xz)$')


def zstd():
    """zstd 命令：PATH 里没有时再找 Homebrew 的目录（agent 的 shell 常常没带上）。"""
    hit = shutil.which('zstd')
    if hit:
        return hit
    for d in ('/opt/homebrew/bin', '/usr/local/bin'):
        if os.access(os.path.join(d, 'zstd'), os.X_OK):
            return os.path.join(d, 'zstd')
    return None


TAR_ENV = dict(os.environ, COPYFILE_DISABLE='1')   # macOS 的 tar 不带 ._ 附属文件


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024


def is_text(name):
    low = name.lower()
    return os.path.splitext(low)[1] in TEXT_EXT or low.endswith('.outline.md')


def age_days(path, now):
    return (now - dt.datetime.fromtimestamp(os.path.getmtime(path))).days


def newest(path):
    """目录里最新文件的时间；只看文件，目录本身的时间会因为删改文件而变。"""
    times = [os.path.getmtime(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs if f != '.DS_Store']
    return dt.datetime.fromtimestamp(max(times) if times else os.path.getmtime(path))


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)


def locked(path):
    d, n = os.path.split(path)
    return os.path.exists(os.path.join(d, '~$' + n)) or os.path.exists(os.path.join(d, '~$' + n[2:]))


def walk_dirs(project):
    """项目里的目录，跳过资料目录、gdb、隐藏目录。"""
    for root, dirs, _ in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SOURCE_DIRS and not d.endswith('.gdb') and not d.startswith('.')]
        for d in dirs:
            yield os.path.join(root, d)


def line_and_version(name):
    stem = name
    for ext in ('.pptx.log.json', '.outline.md'):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
            break
    else:
        stem = os.path.splitext(stem)[0]
    stem = LINE_SUFFIX.sub('', stem)
    m = VERSION.search(stem)
    if not m:
        return stem, None
    return VERSION.sub('', stem, count=1), int(m.group(1))


def plan(project, now, render_days, round_days, keep):
    project = os.path.abspath(project)
    out = {'project': project, 'delete': [], 'bundles': []}
    renders, rounds, histories = [], [], []
    for d in walk_dirs(project):
        name = os.path.basename(d)
        if name == 'renders' or name.endswith('.renders'):
            renders.append(d)
        elif name in HISTORY_DIRS:
            histories.append(d)
        elif ROUND_DIR.search(name) and os.path.dirname(d) != project:
            rounds.append(d)
    renders = [d for d in renders if not any(d.startswith(r + os.sep) for r in renders)]
    for d in renders:
        if (now - newest(d)).days > render_days:
            out['delete'].append({'path': d, 'size': size_of(d), 'why': f'渲染图，最新文件超过 {render_days} 天'})
    gone = {x['path'] for x in out['delete']}
    for d in rounds:
        date = dt.datetime.strptime(ROUND_DIR.search(os.path.basename(d)).group(1), '%Y%m%d')
        if (now - date).days <= round_days or (now - newest(d)).days <= round_days:
            continue
        files = []
        for root, dirs, fs in os.walk(d):
            if any(root == g or root.startswith(g + os.sep) for g in gone):
                continue
            dirs[:] = [x for x in dirs if x not in HISTORY_DIRS]
            for f in fs:
                p = os.path.join(root, f)
                if is_text(f) or f.startswith('~$') or f == '.DS_Store' or ARCHIVE_NAME.search(f) or locked(p):
                    continue
                files.append(p)
        if files:
            out['bundles'].append({'dir': d, 'kind': '改稿目录', 'files': files})
    for d in histories:
        lines = {}
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p) and not f.startswith('~$') and f != '.DS_Store' and not ARCHIVE_NAME.search(f):
                line, ver = line_and_version(f)
                lines.setdefault(line, []).append((ver, p))
        files = []
        for line, items in lines.items():
            vers = sorted({v for v, _ in items if v is not None})
            keep_vers = set(vers[-keep:]) if keep else set()
            for v, p in items:
                if v is None or v in keep_vers or is_text(os.path.basename(p)) or locked(p):
                    continue
                if age_days(p, now) > round_days:
                    files.append(p)
        if files:
            out['bundles'].append({'dir': d, 'kind': '历史目录', 'files': sorted(files)})
    return out


def report(plans):
    total = 0
    for pl in plans:
        print(f'## {pl["project"]}')
        if not pl['delete'] and not pl['bundles']:
            print('　没有到期的过程文件。')
        for x in pl['delete']:
            total += x['size']
            print(f'　删除　{os.path.relpath(x["path"], pl["project"])}　{human(x["size"])}（{x["why"]}）')
        for b in pl['bundles']:
            s = sum(os.path.getsize(f) for f in b['files'])
            total += s
            print(f'　打包　{os.path.relpath(b["dir"], pl["project"])}（{b["kind"]}）　{len(b["files"])} 个文件，{human(s)}')
            for f in b['files'][:8]:
                print(f'　　　　{os.path.relpath(f, b["dir"])}')
            if len(b['files']) > 8:
                print(f'　　　　……另有 {len(b["files"]) - 8} 个')
    print(f'\n合计处理 {human(total)}；打包后的大小要执行时才知道，多版稿子一般能压到一成以下。')


def write_archive(project, files, dest):
    rel = [os.path.relpath(f, project) for f in files]
    if zstd():
        tar = subprocess.Popen(['tar', '-cf', '-', '-C', project, '--', *rel], stdout=subprocess.PIPE, env=TAR_ENV)
        zst = subprocess.run([zstd(), '-q', '-15', '--long=31', '-T0', '-f', '-o', dest], stdin=tar.stdout)
        tar.stdout.close()
        if tar.wait() or zst.returncode:
            raise RuntimeError(f'打包失败：{dest}')
    else:
        with tarfile.open(dest, 'w:xz', preset=9) as t:
            for r in rel:
                t.add(os.path.join(project, r), arcname=r)


def read_members(path):
    """[(包内路径, 大小)]"""
    if path.endswith('.zst'):
        z = subprocess.Popen([zstd(), '-d', '-q', '--long=31', '-c', path], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=z.stdout, mode='r|') as t:
            out = [(m.name, m.size) for m in t if m.isfile()]
        z.stdout.close()
        if z.wait():
            raise RuntimeError(f'{path} 解压出错，包可能不完整')
        return out
    with tarfile.open(path, 'r:xz') as t:
        return [(m.name, m.size) for m in t.getmembers() if m.isfile()]


def apply(pl, now, script):
    project = pl['project']
    for x in pl['delete']:
        shutil.rmtree(x['path']) if os.path.isdir(x['path']) else os.remove(x['path'])
        print(f'已删除　{os.path.relpath(x["path"], project)}')
    stamp = now.strftime('%Y%m%d')
    ext = 'tar.zst' if zstd() else 'tar.xz'
    for b in pl['bundles']:
        name = ('_归档_' if b['kind'] == '改稿目录' else '归档_') + f'{stamp}.{ext}'
        dest = os.path.join(b['dir'], name)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(b['dir'], name.replace(f'.{ext}', f'_{n}.{ext}'))
            n += 1
        want = {os.path.relpath(f, project): os.path.getsize(f) for f in b['files']}
        write_archive(project, b['files'], dest)
        got = dict(read_members(dest))
        if got != want:
            os.remove(dest)
            raise RuntimeError(f'{dest}：包内文件和原文件对不上，已删掉这个包，原文件没动')
        for f in b['files']:
            os.remove(f)
        before, after = sum(want.values()), os.path.getsize(dest)
        write_list(dest, project, want, before, after, now, script)
        print(f'已打包　{os.path.relpath(dest, project)}　{len(want)} 个文件，{human(before)} → {human(after)}')
        for root, dirs, fs in os.walk(b['dir'], topdown=False):
            if root != b['dir'] and not os.listdir(root):
                os.rmdir(root)


def write_list(dest, project, members, before, after, now, script):
    rel_dest = os.path.relpath(dest, project)
    lines = [f'# {os.path.basename(dest)} 清单', '',
             f'{now:%Y-%m-%d} 打包，{len(members)} 个文件，原 {human(before)}，压缩后 {human(after)}。',
             '包内路径相对项目目录。', '',
             '## 解压', '',
             '只取一个文件（解回原位置，已存在的不覆盖）：', '',
             '```bash', f'python3 -I {script} extract "{rel_dest}" --match 文件名里的字', '```', '',
             '没有脚本时：', '',
             '```bash', f'cd "{project}"',
             (f'zstd -d --long=31 -c "{rel_dest}" | tar -xvkf - "<包内路径>"' if dest.endswith('.zst')
              else f'tar -xJvkf "{rel_dest}" "<包内路径>"'), '```', '',
             '## 文件', '', '| 包内路径 | 大小 |', '|---|---|']
    lines += [f'| `{k}` | {human(v)} |' for k, v in sorted(members.items())]
    with open(re.sub(r'\.tar\.(zst|xz)$', '_清单.md', dest), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def project_of(archive):
    """包内路径相对项目目录：从包所在目录往上找，直到包内第一个路径的开头目录出现。"""
    first = read_members(archive)[0][0]
    head = first.split('/')[0]
    d = os.path.dirname(os.path.abspath(archive))
    while d != os.path.dirname(d):
        if os.path.basename(d) == head or os.path.isdir(os.path.join(d, head)):
            return d if os.path.isdir(os.path.join(d, head)) else os.path.dirname(d)
        d = os.path.dirname(d)
    raise RuntimeError('找不到包对应的项目目录，用 --to 指定解压到哪里')


def extract(archive, match, to):
    names = [n for n, _ in read_members(archive) if not match or match in n]
    if not names:
        print('包里没有匹配的文件')
        return 1
    to = to or project_of(archive)
    if archive.endswith('.zst'):
        z = subprocess.Popen([zstd(), '-d', '-q', '--long=31', '-c', archive], stdout=subprocess.PIPE)
        t = subprocess.run(['tar', '-xf', '-', '-C', to, '-k', '--', *names], stdin=z.stdout, env=TAR_ENV)
        z.stdout.close()
        z.wait()
        if t.returncode:
            print('有文件没解出来（同名文件已存在时不覆盖），见上面的提示')
    else:
        with tarfile.open(archive, 'r:xz') as t:
            for n in names:
                if not os.path.exists(os.path.join(to, n)):
                    t.extract(n, to)
    for n in names:
        p = os.path.join(to, n)
        if os.path.exists(p):
            os.utime(p)          # 解出来的文件按刚用过算，免得下次又被打包
        print(p)
    return 0


def main():
    ap = argparse.ArgumentParser(description='过程文件归档')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for c in ('plan', 'apply'):
        p = sub.add_parser(c)
        p.add_argument('projects', nargs='+')
        p.add_argument('--render-days', type=int, default=7)
        p.add_argument('--round-days', type=int, default=14)
        p.add_argument('--keep', type=int, default=2, help='历史目录里每条汇报线留最新几版不打包')
    p = sub.add_parser('list')
    p.add_argument('archive')
    p = sub.add_parser('extract')
    p.add_argument('archive')
    p.add_argument('--match')
    p.add_argument('--to')
    a = ap.parse_args()
    now = dt.datetime.now()
    if a.cmd in ('plan', 'apply'):
        plans = [plan(p, now, a.render_days, a.round_days, a.keep) for p in a.projects]
        report(plans)
        if a.cmd == 'apply':
            print()
            for pl in plans:
                apply(pl, now, os.path.abspath(__file__))
        return 0
    if a.cmd == 'list':
        for n, s in read_members(a.archive):
            print(f'{human(s):>10}  {n}')
        return 0
    return extract(a.archive, a.match, a.to)


if __name__ == '__main__':
    sys.exit(main())
