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

"""骨架转换(重命名)功能: 与映射(mapping/retarget_math)完全独立的数据与逻辑。

工作流:
  1. 选中任意骨架, "导出骨架结构" 一键写出 <SkeletonConfig>/<骨架名>.json
     (骨骼层级 + 世界空间头尾 + 蒙皮命中, 供人/AI 阅读识别用)。
  2. AI (或人) 读取该 json, 按目标命名规范手写一份"重命名预设" json
     (old→new 表 + 自定义 display_name), 存进 <SkeletonRename>/。
  3. Blender 里从预设菜单按 display_name 选中该预设, "应用重命名" 一键
     把骨骼改名。

重命名只需要管好骨骼名本身的冲突: Blender 的 Bone.name setter 本来就会把
顶点组名 / FCurve 路径 / 同骨架内约束 subtarget 一并改掉 (逐条 RNA 属性
赋值触发, 不是只有菜单里的重命名操作符才有 —— 已实测确认), 蒙皮和已有
动画不会因改名而失效, 不需要 (也不应该) 在这之上再手搓一遍同步: 手搓版本
只会用改名前的旧表把引擎刚修好的引用改错, 互换名场景尤其致命。
"""

import json
import os

import bpy

from . import presets
from .state import resolve_dest_object

# 骨架结构导出是另一种数据(骨骼层级快照, 不是映射), 所以单独一个目录;
# 改名表 = 映射表, 共用 presets.PRESET_SUBDIR 那一个配置目录。
SKELETON_EXPORT_SUBDIR = 'presets/AnimationRetarget/SkeletonConfig'
SKELETON_FORMAT = 'AnimationRetargetSkeleton'
SKELETON_VERSION = 1


# ---------------------------------------------------------------------------
# 骨架结构导出 (供 AI 识别用, 与 retarget_math.Skeleton 无关)
# ---------------------------------------------------------------------------

def selected_armatures(context):
    """要导出结构的骨架 = 当前选中的全部骨架 (同时选中来源与目标两个骨架,
    一次就导出两份, 供 AI 对比两边命名); 一个都没选中时退回自动识别的那个。
    """
    out = []
    for o in getattr(context, 'selected_objects', ()) or ():
        if o.type == 'ARMATURE' and o not in out:
            out.append(o)
    if not out:
        one = resolve_dest_object(context)
        if one is not None:
            out.append(one)
    return out


def deforming_meshes(arm_obj):
    """有 ARMATURE 修改器指向 arm_obj 的网格对象 (导出结构时取蒙皮命中用)。"""
    out = []
    for o in bpy.data.objects:
        if o.type != 'MESH':
            continue
        for mod in o.modifiers:
            if mod.type == 'ARMATURE' and mod.object == arm_obj:
                out.append(o)
                break
    return out


def _vertex_group_index(arm_obj):
    idx = {}
    for mesh_obj in deforming_meshes(arm_obj):
        for vg in mesh_obj.vertex_groups:
            idx.setdefault(vg.name, []).append(mesh_obj.name)
    return idx


def _bone_order(data_bones):
    """拓扑序: 根优先, 同级子骨深度优先展开 — 顺着 depth 字段就是层级缩进。"""
    order = []

    def visit(b, depth):
        order.append((b, depth))
        for c in b.children:
            visit(c, depth + 1)

    for b in data_bones:
        if b.parent is None:
            visit(b, 0)
    return order


def export_skeleton(obj):
    """Object(ARMATURE) → JSON 可序列化的骨架结构快照。"""
    mw = obj.matrix_world
    deform_index = _vertex_group_index(obj)
    bones = []
    for b, depth in _bone_order(obj.data.bones):
        head_w = mw @ b.head_local
        tail_w = mw @ b.tail_local
        bones.append({
            'name': b.name,
            'parent': b.parent.name if b.parent else None,
            'children': [c.name for c in b.children],
            'depth': depth,
            'head_world': [round(v, 6) for v in head_w],
            'tail_world': [round(v, 6) for v in tail_w],
            'length_world': round((tail_w - head_w).length, 6),
            'use_connect': b.use_connect,
            'use_deform': b.use_deform,
            'deform_meshes': deform_index.get(b.name, []),
        })
    return {
        'format': SKELETON_FORMAT,
        'version': SKELETON_VERSION,
        'armature': obj.name,
        'bone_count': len(bones),
        'bones': bones,
    }


def skeleton_export_dir(create=True):
    return presets.preset_dir(SKELETON_EXPORT_SUBDIR, create=create)


def skeleton_export_path(armature_name):
    return os.path.join(skeleton_export_dir(),
                        presets.sanitize(armature_name) + '.json')


def write_skeleton_export(obj):
    """导出永远原地覆盖同名文件 — 只向前看, 不留旧档 (与映射预设同一哲学)。"""
    path = skeleton_export_path(obj.name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(export_skeleton(obj), f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# 重命名预设 JSON I/O (复用 presets.py, 独立子目录)
# ---------------------------------------------------------------------------

def rename_preset_dir(create=True):
    return presets.preset_dir(create=create)


def list_rename_presets():
    return presets.list_presets()


def save_rename_preset(name, spec):
    return presets.save_preset(name, spec)


def load_rename_preset(name):
    return presets.load_preset(name)


def delete_rename_preset(name):
    return presets.delete_preset(name)


# ---------------------------------------------------------------------------
# 重命名应用: 两阶段防冲突改名 (顶点组/FCurve/约束引用由 Blender 引擎自身
# 的 Bone.name setter 联动维护, 见文件头说明, 这里不重复实现)
# ---------------------------------------------------------------------------

def apply_rename(obj, pairs):
    """pairs: [(old, new), ...]。

    两阶段临时改名防瞬时冲突 (含 A↔B 互换名场景): 先把全部涉及骨骼改成
    互不相干的临时名, 再统一改成最终名 — 否则例如 A→B、B→A 这种互换,
    若按输入顺序直接改, 第二步会撞上第一步刚占用的名字, 被 Blender
    自动加 .001 后缀写歪。
    返回统计报告 dict: renamed(实际生效的 (old, new) 列表, new 可能因目标名
    冲突被 Blender 加了后缀) / missing(骨架上不存在的原名) / collided
    (目标名被占用、实际改名结果与请求不符的三元组)。
    """
    data = obj.data
    seen, jobs, missing = set(), [], []
    for old, new in pairs:
        old, new = (old or '').strip(), (new or '').strip()
        if not old or not new or old in seen:
            continue
        seen.add(old)
        if old not in data.bones:
            missing.append(old)
            continue
        jobs.append([old, new, None])

    for i, job in enumerate(jobs):
        tmp = '.animret.tmp.%d' % i
        data.bones[job[0]].name = tmp
        job[2] = tmp

    rename_map, collided = {}, []
    for old, new, tmp in jobs:
        b = data.bones.get(tmp)
        if b is None:
            continue
        b.name = new
        rename_map[old] = b.name
        if b.name != new:
            collided.append((old, new, b.name))

    return {
        'renamed': sorted(rename_map.items()),
        'missing': missing,
        'collided': collided,
    }


# ---------------------------------------------------------------------------
# 会话态: 与映射状态 (state.AnimRetState) 完全独立
# ---------------------------------------------------------------------------

class AnimRetRenamePair(bpy.types.PropertyGroup):
    old_name: bpy.props.StringProperty(
        name='原名', description='待改名骨架上现有的骨骼名',
        override={'LIBRARY_OVERRIDABLE'})
    new_name: bpy.props.StringProperty(
        name='新名', description='改成的目标命名规范骨骼名',
        override={'LIBRARY_OVERRIDABLE'})


class AnimRetPresetEntry(bpy.types.PropertyGroup):
    """配置浏览器里的一行 (磁盘上的一个配置文件)。"""
    name: bpy.props.StringProperty()      # 文件名(不含扩展名)
    label: bpy.props.StringProperty()     # display_name, 没有就用文件名


def count_directions(obj, pairs):
    """(正向可改名数, 反向可改名数) —— 看骨架现在是哪一侧的命名, 决定能往哪转。"""
    names = {b.name for b in obj.data.bones} if obj else set()
    fwd = sum(1 for o, n in pairs if o in names and o != n)
    rev = sum(1 for o, n in pairs if n in names and o != n)
    return fwd, rev


def _on_browse_select(self, context):
    """在配置浏览器里点一行 = 立刻把那份配置读进来展开显示。"""
    if not (0 <= self.active_entry < len(self.entries)):
        return
    e = self.entries[self.active_entry]
    try:
        spec = presets.load_preset(e.name)
    except Exception:
        return
    self.from_spec(spec)
    self.active_preset = e.name


class AnimRetSkeletonToolState(bpy.types.PropertyGroup):
    """骨架转换(重命名)工具的会话态, 挂在 Scene 上 (转换是一次性动作,
    不像映射配置那样需要常驻挂在某个目标骨架的数据块上)。"""

    entries: bpy.props.CollectionProperty(type=AnimRetPresetEntry)
    active_entry: bpy.props.IntProperty(default=-1, update=_on_browse_select)

    pair_filter: bpy.props.StringProperty(
        name='搜索', default='', options={'TEXTEDIT_UPDATE'},
        description='按骨骼名过滤对照表 (原名/新名任一命中即显示); '
                    '空格分隔多个词 = 全部命中才显示')

    pairs: bpy.props.CollectionProperty(
        type=AnimRetRenamePair,
        override={'LIBRARY_OVERRIDABLE', 'USE_INSERTION'})
    active_pair: bpy.props.IntProperty(
        default=-1, override={'LIBRARY_OVERRIDABLE'})
    active_preset: bpy.props.StringProperty(
        default='', description='当前加载的骨架转换预设名 ("保存"直接覆盖此预设)',
        override={'LIBRARY_OVERRIDABLE'})
    display_name: bpy.props.StringProperty(
        default='', name='显示名称',
        description='该预设在菜单中显示的自定义名字, 与磁盘文件名分开',
        override={'LIBRARY_OVERRIDABLE'})

    def to_spec(self, source_armature=''):
        # 与映射面板同一种落盘格式, 不再另出一种 renames 形状
        return presets.make_spec(
            [{'source': p.old_name, 'dest': p.new_name}
             for p in self.pairs if p.old_name and p.new_name],
            display_name=self.display_name, source_armature=source_armature)

    def from_spec(self, spec):
        self.pairs.clear()
        # 改名表与重定向映射表通用: 映射表的 source/dest 就是这里的 old/new
        for src, dst, _extra in presets.normalize_pairs(spec):
            p = self.pairs.add()
            p.old_name = src
            p.new_name = dst
        self.active_pair = 0 if len(self.pairs) else -1
        self.display_name = spec.get('display_name', '')


def pair_matches(pair, query):
    """空格分隔的多个词全部命中(原名或新名任一侧)才算匹配。"""
    if not query:
        return True
    hay = (pair.old_name + ' ' + pair.new_name).lower()
    return all(w in hay for w in query.lower().split())


def refresh_entries(state):
    """重扫两个配置目录, 填进浏览器列表 (改名表与映射表通用, 一起列)。"""
    keep = state.active_preset
    state.entries.clear()
    for label, name in presets.list_all_presets():
        e = state.entries.add()
        e.name = name
        e.label = label
    idx = next((i for i, e in enumerate(state.entries) if e.name == keep), -1)
    state['active_entry'] = idx          # 绕过 update, 不重复加载
    return len(state.entries)


classes = (
    AnimRetRenamePair,
    AnimRetPresetEntry,
    AnimRetSkeletonToolState,
)
