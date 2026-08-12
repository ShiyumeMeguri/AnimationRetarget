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
  4. 同一份对照表还能直接拿去转换动作: 把当前 blend 文件里的动作收进面板,
     按表改写动作内部的骨骼引用 (FCurve 路径 + 通道组名), 同样支持互转。

骨架改名只需要管好骨骼名本身的冲突: Blender 的 Bone.name setter 本来就会把
顶点组名 / FCurve 路径 / 同骨架内约束 subtarget 一并改掉 (逐条 RNA 属性
赋值触发, 不是只有菜单里的重命名操作符才有 —— 已实测确认), 蒙皮和已有
动画不会因改名而失效, 不需要 (也不应该) 在这之上再手搓一遍同步: 手搓版本
只会用改名前的旧表把引擎刚修好的引用改错, 互换名场景尤其致命。

引擎的联动只覆盖"挂在某个对象动画数据上"的动作。从游戏/FBX 导进来堆在文件里
的那一大批动作没有任何使用者, 引擎不会替它们改路径 —— 那批就是第 4 步的活。
"""

import json
import os
import re

import bpy

from . import core
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


def write_skeleton_export(obj, out_path=None):
    path = out_path or skeleton_export_path(obj.name)
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
# 动画转换: 同一份对照表改写动作内部的骨骼引用
# ---------------------------------------------------------------------------

_BONE_REF_RE = re.compile(r'pose\.bones\["((?:[^"\\]|\\.)*)"\]')


def _rewrite_path(data_path, name_map):
    """把路径里 pose.bones["旧名"] 的那一段换成新名 (转义交给引擎处理)。"""
    def one(m):
        old = bpy.utils.unescape_identifier(m.group(1))
        new = name_map.get(old)
        if new is None:
            return m.group(0)
        return 'pose.bones["%s"]' % bpy.utils.escape_identifier(new)
    return _BONE_REF_RE.sub(one, data_path)


def action_bone_names(action):
    """动作里引用过的全部骨骼名 (从 FCurve 路径解析, 含自定义属性/约束路径)。"""
    out = set()
    for fc in core.iter_action_fcurves(action):
        for m in _BONE_REF_RE.finditer(fc.data_path):
            out.add(bpy.utils.unescape_identifier(m.group(1)))
    return out


def rename_action(action, name_map):
    """按 name_map 改写一个动作里的骨骼引用。

    路径是纯字符串、单趟查表替换, 互换名天然安全 (不像骨骼名那样共用命名空间);
    通道组名共用命名空间, 所以走和骨骼一样的两阶段临时名。
    返回 (改写的曲线数, 改名的通道组数, 改完后重名的 (路径, 分量) 列表)。
    """
    finals = [(fc, _rewrite_path(fc.data_path, name_map))
              for fc in core.iter_action_fcurves(action)]

    # 撞车判据看的是改完之后的终态, 不受遍历顺序影响
    seen, collided = set(), []
    for fc, path in finals:
        key = (path, fc.array_index)
        if key in seen:
            collided.append(key)
        seen.add(key)

    curves = 0
    for fc, path in finals:
        if path != fc.data_path:
            fc.data_path = path
            curves += 1

    jobs = [(g, name_map[g.name]) for g in core.iter_action_groups(action)
            if g.name in name_map]
    for i, (g, _new) in enumerate(jobs):
        g.name = '.animret.tmp.%d' % i
    for g, new in jobs:
        g.name = new

    return curves, len(jobs), collided


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


class AnimRetActionEntry(bpy.types.PropertyGroup):
    """动作列表里的一行 (当前 blend 文件里的一个 Action)。"""
    name: bpy.props.StringProperty()
    selected: bpy.props.BoolProperty(
        default=True, description='勾选的动作才参与转换')
    bone_count: bpy.props.IntProperty(description='该动作引用的骨骼数')
    forward: bpy.props.IntProperty(description='正向可转换的骨名数')
    backward: bpy.props.IntProperty(description='反向可还原的骨名数')


def armature_bone_names(obj):
    return {b.name for b in obj.data.bones} if obj else set()


def count_directions(names, pairs):
    """(正向可改名数, 反向可改名数) —— 看现在用的是哪一侧的命名, 决定能往哪转。

    骨架和动作共用这一个判据: names 是骨架的骨名集或动作引用到的骨名集。
    """
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
    refresh_action_counts(self)   # 换了配置, 动作能往哪转也跟着变


class AnimRetSkeletonToolState(bpy.types.PropertyGroup):
    """骨架转换(重命名)工具的会话态, 挂在 Scene 上 (转换是一次性动作,
    不像映射配置那样需要常驻挂在某个目标骨架的数据块上)。"""

    entries: bpy.props.CollectionProperty(type=AnimRetPresetEntry)
    active_entry: bpy.props.IntProperty(default=-1, update=_on_browse_select)

    pair_filter: bpy.props.StringProperty(
        name='搜索', default='', options={'TEXTEDIT_UPDATE'},
        description='按骨骼名过滤对照表 (原名/新名任一命中即显示); '
                    '空格分隔多个词 = 全部命中才显示')

    actions: bpy.props.CollectionProperty(type=AnimRetActionEntry)
    active_action: bpy.props.IntProperty(default=-1)
    action_filter: bpy.props.StringProperty(
        name='搜索', default='', options={'TEXTEDIT_UPDATE'},
        description='按动作名过滤列表; 空格分隔多个词 = 全部命中才显示')

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


def action_matches(entry, query):
    if not query:
        return True
    hay = entry.name.lower()
    return all(w in hay for w in query.lower().split())


def refresh_actions(state):
    """把当前 blend 文件里的动作全部收进面板列表 (保留已有勾选)。"""
    chosen = {e.name: e.selected for e in state.actions}
    state.actions.clear()
    for act in sorted(bpy.data.actions, key=lambda a: a.name):
        e = state.actions.add()
        e.name = act.name
        e.selected = chosen.get(act.name, True)
    state.active_action = 0 if len(state.actions) else -1
    refresh_action_counts(state)
    return len(state.actions)


def refresh_action_counts(state):
    """按当前配置重算每条动作引用了哪些骨骼、能往哪个方向转。"""
    pairs = [(p.old_name, p.new_name) for p in state.pairs]
    for e in state.actions:
        names = action_bone_names(bpy.data.actions.get(e.name))
        e.bone_count = len(names)
        e.forward, e.backward = count_directions(names, pairs)


def selected_actions(state):
    """勾选中的动作条目 (面板上的"将转换 N 条"就是这个)。"""
    return [e for e in state.actions if e.selected]


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
    AnimRetActionEntry,
    AnimRetSkeletonToolState,
)
