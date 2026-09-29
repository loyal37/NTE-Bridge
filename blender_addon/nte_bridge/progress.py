"""Small replace-in-place progress messages shared by Blender and workers."""
import json
import os
from pathlib import Path
import re
import time


def report_progress(text, completed=None, total=None, *, log=None):
    path = os.environ.get('NTE_BRIDGE_PROGRESS')
    if not path:
        return
    data = {'token': os.environ.get('NTE_BRIDGE_PROGRESS_TOKEN', ''), 'text': text,
            'updated': time.time(), 'completed': completed, 'total': total, 'log': str(log) if log else ''}
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass  # Display failures must not invalidate imported/cooked assets.


def read_progress(path, token):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('token') != token:
            return None
        text = data['text']
        done, total = data.get('completed'), data.get('total')
        log = data.get('log')
        if log and Path(log).is_file():
            with Path(log).open('rb') as stream:
                stream.seek(max(0, stream.seek(0, 2) - 32768))
                tail = stream.read().decode('utf-8', errors='replace')
            matches = re.findall(r'Cooked packages\s+(\d+).*?Packages Remain(?:ing)?\s+(\d+)', tail, re.I)
            if matches:
                done, remaining = map(int, matches[-1])
                total = done + remaining
                text = 'UE 正在烘焙资产'
            else:
                shaders = re.findall(r'Shaders left to compile\s+(\d+)', tail, re.I)
                if shaders:
                    text += ' · 待编译着色器 ' + shaders[-1]
                # The existing external packager already supplies real progress.
                for line in reversed(tail.splitlines()):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get('percent'), (int, float)):
                        done, total = record['percent'], 100
                        text = {'Preparing': '准备打包', 'Patching': '修复形态键与贴图',
                                'Converting': '生成 Mod', 'Verifying': '校验 Mod',
                                'Publishing': '写入 Mod', 'Completed': '打包完成'}.get(record.get('stage'), '正在打包')
                        break
        fraction = -1.0
        if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
            fraction = max(0.0, min(1.0, done / total))
            text += ' · %d / %d' % (done, total)
        return text, fraction
    except (OSError, ValueError, KeyError, TypeError):
        return None
