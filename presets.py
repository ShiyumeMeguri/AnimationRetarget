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
"""

import json
import os
import re

import bpy

PRESET_SUBDIR = 'presets/AnimationRetarget'
_INVALID = re.compile(r'[\\/:*?"<>|]+')


def preset_dir(create=True):
    return bpy.utils.user_resource('SCRIPTS', path=PRESET_SUBDIR,
                                   create=create)


def sanitize(name):
    return _INVALID.sub('_', name).strip()


def preset_path(name):
    return os.path.join(preset_dir(), sanitize(name) + '.json')


def list_presets():
    d = preset_dir(create=False)
    if not d or not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(d)
                  if f.lower().endswith('.json'))


def save_preset(name, spec):
    """直接写盘覆盖 — 这就是旧版做不到的"更新保存"。"""
    path = preset_path(name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    return path


def load_preset(name):
    with open(preset_path(name), 'r', encoding='utf-8') as f:
        return json.load(f)


def load_preset_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def delete_preset(name):
    path = preset_path(name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
