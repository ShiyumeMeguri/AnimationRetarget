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

# 唯一的配置目录。重定向映射表和骨架改名表本质是同一种东西, 所以不分家 ——
# 分两个目录会导致"读了这边的、存到那边去", 同一份配置在浏览器里出现两条。
PRESET_SUBDIR = 'presets/AnimationRetarget'
_INVALID = re.compile(r'[\\/:*?"<>|]+')


def normalize_pairs(spec):
    """任何一种配置 → [(源骨名, 目标骨名, 该条的附加参数dict), …]

    重定向映射表和骨架改名表本质是同一种东西 —— 都是有序的"源骨名 → 目标骨名",
    只是语义不同 (改名 = 同一骨架内换名字, 重定向 = 跨骨架传动画)。
    写盘一律用 mappings (见 pairs_spec), renames 只是读侧兼容 AI 手写的简写。
    附加参数 (rot/loc/ik) 只有重定向配置才有, 改名表缺这些时按默认值走。
    """
    return [(row['source'], row['dest'], row) for row in normalize_pairs_rows(spec)]


def normalize_pairs_rows(spec):
    """同一份规整化的另一种取法: 直接给**行字典**, 每行都带 source/dest。

    normalize_pairs 的第三项对 mappings 来说就是原行、对 renames 来说是空 dict, 拿它
    当行用会把 renames 的两端弄丢。要整行 (组合、翻转、拼接都要) 就用这个, 两者共用
    下面这一段唯一的读取逻辑。
    """
    out = []
    for m in spec.get('mappings') or ():
        s, d = m.get('source', ''), m.get('dest', '')
        if s and d:
            out.append(dict(m, source=s, dest=d))
    for r in spec.get('renames') or ():
        s, d = r.get('from', ''), r.get('to', '')
        if s and d:
            out.append({'source': s, 'dest': d})
    return out


FORMAT = 'AnimationRetarget'
VERSION = 4


def make_spec(pairs, source=None, dest=None, settings=None, display_name=''):
    """唯一的落盘格式。一份配置只说两件事:

        它连接**哪两副骨架**   source / dest = {"family", "config"}
        以及**怎么连**         mappings 一条一条 {source, dest, rot/loc/ik ...}

    骨架身份是 家族 + 配置, 两个都只是名字 —— **代码里不出现任何一个具体家族**。
    家族连同它的别名就写在 source/dest 里, 所以目录里只有 XXToXX.json 一种文件,
    一份表拿走就能单独用 (见 compose)。

    这里不再记任何**场景里的东西**: 骨架对象叫什么是这一次打开这个 blend 的偶然事实,
    存进配置就等于让一份本该到处能用的表只对一个文件有效。配置里只有骨名与偏移这类
    骨架自己的固有信息。

    ``display_name`` 只服务骨架改名表 —— 那种表不连接两副骨架, 不上图, 只需要一个
    人读的标题。重定向表不写它: 它的标题就是"家族 配置 → 家族 配置", 现算即可。
    """
    spec = {
        'format': FORMAT,
        'version': VERSION,
        'source': _side_spec(source),
        'dest': _side_spec(dest),
        'settings': settings or {},
        'mappings': pairs,
    }
    if display_name:
        spec['display_name'] = display_name
    return spec


def _side_spec(side):
    """Side 对象 / dict / None → 落盘用的 {"family", "config"}。"""
    if side is None:
        return {}
    family = getattr(side, 'family', None)
    config = getattr(side, 'config', None)
    if family is None and isinstance(side, dict):
        family, config = side.get('family'), side.get('config')
    aliases = getattr(side, 'aliases', None)
    if aliases is None and isinstance(side, dict):
        aliases = side.get('aliases')
    family, config = str(family or '').strip(), str(config or '').strip()
    if not (family and config):
        return {}
    spec = {'family': family, 'config': config}
    names = [str(a).strip() for a in (aliases or ()) if str(a).strip()]
    if names:
        spec['aliases'] = names
    return spec


OWNED_KEYS = frozenset(make_spec([]).keys()) | {'renames', 'display_name',
                                                'source_armature', 'dest_armature'}


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
    """直接写盘覆盖 — 这就是旧版做不到的"更新保存"。

    **不归本模块管的顶层键 (OWNED_KEYS 之外的) 一律原样保留。** to_spec 是从面板
    状态重新拼一份 spec 出来的, make_spec 只产出它认识的那几个键 —— 所以本模块看不懂
    的段落 (手写/AI 写进去的 face 表情契约) 在"读进来看一眼再点保存"之后就会凭空消失,
    而且**不报错**: 文件还在, 只是从此不再带着那一对骨架的表情。

    反过来, OWNED_KEYS 里的键必须**被这次写入覆盖掉**, 保留才是错的 —— renames 是
    mappings 的别名, 留着盘上那份就等于同一批骨对存了两遍、读的时候翻一倍。
    要删掉某个外来段落 = 直接编辑文件, 那本来就是那些段落被写出来的地方。
    """
    path = preset_path(name, subdir)
    merged = dict(spec)
    if os.path.isfile(path):
        try:
            for key, value in load_preset_file(path).items():
                if key not in OWNED_KEYS:
                    merged.setdefault(key, value)
        except (ValueError, OSError):
            pass
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
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
    """唯一配置目录里的全部配置 → [(显示名, 预设名), …]。"""
    out = []
    for name in list_presets():
        label = name
        try:
            spec = load_preset(name)
            label = spec.get('display_name') or name
            n = len(normalize_pairs(spec))
        except Exception:
            n = -1
        if n >= 0:
            label = '%s (%d)' % (label, n)
        out.append((label, name))
    return out
