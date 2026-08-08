# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""JSON 预设系统: 直接覆盖保存 / 另存为 / 删除 / 加载。

预设即 spec 字典 (与 CLI 配置同格式), 人类可读、可进版本管理、可被 AI 编辑。
"保存"按钮永远直接更新当前激活预设文件 (无需重新命名即可覆盖)。

subdir 参数让骨架重命名等其他功能复用同一套存取逻辑而不必各自实现一遍
(单一真源); 默认值维持映射预设原本的子目录, 现有调用点无需改动。
"""

import json
import os
import re

import bpy

PRESET_SUBDIR = 'presets/AnimationRetarget'
SKELETON_RENAME_SUBDIR = 'presets/AnimationRetarget/SkeletonRename'
_INVALID = re.compile(r'[\\/:*?"<>|]+')


def normalize_pairs(spec):
    """任何一种配置 → [(源骨名, 目标骨名, 该条的附加参数dict), …]

    重定向映射表和骨架改名表本质是同一种东西 —— 都是有序的"源骨名 → 目标骨名",
    只是语义不同 (改名 = 同一骨架内换名字, 重定向 = 跨骨架传动画)。所以两边
    互相通用: 改名表能直接当重定向映射用, 反之亦然。附加参数 (rot/loc/ik) 只有
    重定向配置才有, 改名表缺这些时按默认值走。
    """
    out = []
    for m in spec.get('mappings') or ():
        s, d = m.get('source', ''), m.get('dest', '')
        if s and d:
            out.append((s, d, m))
    for r in spec.get('renames') or ():
        s, d = r.get('from', ''), r.get('to', '')
        if s and d:
            out.append((s, d, {}))
    return out


def preset_dir(subdir=PRESET_SUBDIR, create=True):
    return bpy.utils.user_resource('SCRIPTS', path=subdir, create=create)


def sanitize(name):
    return _INVALID.sub('_', name).strip()


def preset_path(name, subdir=PRESET_SUBDIR):
    return os.path.join(preset_dir(subdir), sanitize(name) + '.json')


def list_presets(subdir=PRESET_SUBDIR):
    d = preset_dir(subdir, create=False)
    if not d or not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(d)
                  if f.lower().endswith('.json'))


def save_preset(name, spec, subdir=PRESET_SUBDIR):
    """直接写盘覆盖 — 这就是旧版做不到的"更新保存"。"""
    path = preset_path(name, subdir)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    return path


def load_preset(name, subdir=PRESET_SUBDIR):
    with open(preset_path(name, subdir), 'r', encoding='utf-8') as f:
        return json.load(f)


def load_preset_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def delete_preset(name, subdir=PRESET_SUBDIR):
    path = preset_path(name, subdir)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def list_all_presets():
    """两个目录的配置合并列出 → [(显示名, 预设名, subdir), …]。

    映射预设与骨架改名表通用 (见 normalize_pairs), 所以两个面板的菜单都把
    两边一起列出来, 省得为了"配置不通用"而没法选。
    """
    out = []
    for subdir, tag in ((PRESET_SUBDIR, ''),
                        (SKELETON_RENAME_SUBDIR, '改名表')):
        for name in list_presets(subdir):
            label = name
            try:
                spec = load_preset(name, subdir)
                label = spec.get('display_name') or name
                n = len(normalize_pairs(spec))
            except Exception:
                n = -1
            if tag:
                label = '%s  [%s]' % (label, tag)
            if n >= 0:
                label = '%s (%d)' % (label, n)
            out.append((label, name, subdir))
    return out
