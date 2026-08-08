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

"""操作符层: 烘焙 / 映射工具 / 对齐捕捉 / 预设 / 清理。"""

import difflib
import os

import bpy
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import core
from . import presets
from .state import get_state, get_dest_object


def _alert(title, message):
    def draw(self, context):
        for line in message.split('\n'):
            self.layout.label(text=line)
    bpy.context.window_manager.popup_menu(draw, title=title, icon='ERROR')


class _StateOperator:
    @classmethod
    def poll(cls, context):
        return get_state(context) is not None


# ---------------------------------------------------------------------------
# 烘焙
# ---------------------------------------------------------------------------

class ANIMRET_OT_bake(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.bake'
    bl_label = '烘焙动画'
    bl_description = ('把动画来源骨架当前指派的动作重定向烘焙为目标骨架的新动作\n'
                      '纯数学直写关键帧: 不经过 nla.bake, 不依赖骨骼选择/可见性, '
                      '不创建任何约束')
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        src = s.source
        if src is None:
            _alert('未设置动画来源', '请先选择动画来源骨架')
            return {'CANCELLED'}
        act = src.animation_data.action if src.animation_data else None
        if act is None:
            _alert('来源骨架上没有动作', '请先给来源骨架指派一个动作, '
                   '或使用"批量烘焙"处理全部动作')
            return {'CANCELLED'}
        spec = {'mappings': s.mappings_spec()}
        try:
            with core.preview_suspended():
                dest_action, info = core.bake_action(src, dest, spec, act,
                                                     s.settings_spec())
        except Exception as e:
            _alert('烘焙失败', str(e))
            return {'CANCELLED'}
        if s.assign_result:
            core.assign_action(dest, dest_action)
        for w in info['warnings']:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, '烘焙完成: %s (%d 帧 / %d 骨)' %
                    (dest_action.name, info['frames'], info['bones']))
        return {'FINISHED'}


class ANIMRET_OT_bake_all(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.bake_all'
    bl_label = '批量烘焙所有动画'
    bl_description = ('把所有作用于来源骨架的动作逐一重定向烘焙\n'
                      '每条动作都是独立的纯函数计算, 不触碰场景状态 — '
                      '不存在顺序漂移与状态残留')
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        src = s.source
        if src is None:
            _alert('未设置动画来源', '请先选择动画来源骨架')
            return {'CANCELLED'}
        actions = core.list_bakeable_actions(src)
        if not actions:
            self.report({'WARNING'}, '没有找到作用于来源骨架的动作')
            return {'CANCELLED'}
        spec = {'mappings': s.mappings_spec()}
        wm = context.window_manager
        wm.progress_begin(0, len(actions))
        try:
            with core.preview_suspended():
                results, errors = core.bake_all(
                    src, dest, spec, s.settings_spec(), actions,
                    on_progress=lambda i, n, name: wm.progress_update(i))
        finally:
            wm.progress_end()
        if s.push_nla and results:
            core.push_to_nla(dest, [r[1] for r in results])
        for name, err in errors:
            self.report({'ERROR'}, '烘焙 %s 失败: %s' % (name, err))
        warn_count = sum(len(r[2]['warnings']) for r in results)
        msg = '批量烘焙完成: 成功 %d 条' % len(results)
        if errors:
            msg += ', 失败 %d 条' % len(errors)
        if warn_count:
            msg += ', 警告 %d 条 (见控制台)' % warn_count
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class ANIMRET_OT_push_nla(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.push_nla'
    bl_label = '产物推送NLA'
    bl_description = ('把本插件烘焙出的所有动作推送为静音 NLA 轨\n'
                      '配合 FBX 导出 (Baked Animation: NLA Strips) 得到多 take 文件')
    bl_options = {'UNDO'}

    def execute(self, context):
        dest = get_dest_object(context)
        acts = [a for a in bpy.data.actions if a.get(core.GENERATED_TAG)]
        if not acts:
            self.report({'WARNING'}, '当前没有本插件生成的动作')
            return {'CANCELLED'}
        core.push_to_nla(dest, acts)
        self.report({'INFO'}, '已推送 %d 条动作到 NLA' % len(acts))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 列表与映射工具
# ---------------------------------------------------------------------------

class ANIMRET_OT_clear_mapping_filter(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.clear_mapping_filter'
    bl_label = '清空搜索'

    def execute(self, context):
        get_state(context).mapping_filter = ''
        return {'FINISHED'}


class ANIMRET_OT_select_edit_type(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.select_edit_type'
    bl_label = ''
    bl_description = '切换列表编辑页'
    bl_options = {'UNDO'}

    selected_type: bpy.props.IntProperty(override={'LIBRARY_OVERRIDABLE'})

    def execute(self, context):
        get_state(context).editing_type = self.selected_type
        return {'FINISHED'}


class ANIMRET_OT_select_action(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.select_action'
    bl_label = '列表选择操作'
    bl_description = '全选/弃选/反选'
    bl_options = {'UNDO'}

    action: bpy.props.StringProperty(override={'LIBRARY_OVERRIDABLE'})

    def execute(self, context):
        s = get_state(context)
        if self.action == 'ALL':
            for m in s.mappings:
                m.selected = True
        elif self.action == 'NONE':
            for m in s.mappings:
                m.selected = False
        elif self.action == 'INVERSE':
            for m in s.mappings:
                m.selected = not m.selected
        return {'FINISHED'}


class ANIMRET_OT_list_action(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.list_action'
    bl_label = '列表基本操作'
    bl_description = ('依次为新建(批量)、新建(激活骨)、新建、删除、上移、下移\n'
                      '姿态模式下选中骨骼后点击批量新建可自动填入')
    bl_options = {'UNDO'}

    action: bpy.props.StringProperty(override={'LIBRARY_OVERRIDABLE'})

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)

        def add():
            s.add_mapping('', '')

        def add_select():
            for bone in dest.data.bones:
                if bone.select:
                    s.add_mapping(bone.name, '')
            if s.source is not None:
                for bone in s.source.data.bones:
                    if bone.select:
                        s.add_mapping('', bone.name)

        def add_active():
            d = dest.data.bones.active
            t = s.source.data.bones.active if s.source else None
            s.add_mapping(d.name if d else '', t.name if t else '')

        def remove():
            if len(s.mappings):
                s.remove_selected()

        def move(delta):
            if s.selected_count == 0:
                i = s.active_mapping
                j = i + delta
                if 0 <= i < len(s.mappings) and 0 <= j < len(s.mappings):
                    s.mappings.move(i, j)
                    s.active_mapping = j
            else:
                rng = range(1, len(s.mappings)) if delta < 0 else \
                    range(len(s.mappings) - 2, -1, -1)
                for i in rng:
                    if s.mappings[i].selected and \
                            not s.mappings[i + delta].selected:
                        s.mappings.move(i, i + delta)

        {'ADD': add, 'ADD_SELECT': add_select, 'ADD_ACTIVE': add_active,
         'REMOVE': remove,
         'UP': lambda: move(-1), 'DOWN': lambda: move(1)}[self.action]()
        return {'FINISHED'}


class ANIMRET_OT_child_mapping(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.child_mapping'
    bl_label = '子级映射'
    bl_description = ('若所选映射两端的骨骼都有且仅有唯一子级, '
                      '则在那两个子级间建立新映射')
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        did = False
        for i in s.get_selection():
            m = s.mappings[i]
            if not (m.dest in dest.data.bones and s.source
                    and m.source in s.source.data.bones):
                continue
            if m.selected:
                m.selected = False
            d_children = dest.data.bones[m.dest].children
            s_children = s.source.data.bones[m.source].children
            if len(d_children) == len(s_children) == 1:
                s.add_mapping(d_children[0].name, s_children[0].name,
                              i + 1)[0].selected = True
                did = True
            else:
                for k, child in enumerate(d_children):
                    s.add_mapping(child.name, '', i + k + 1)[0].selected = True
                    did = True
        if not did:
            self.report({'ERROR'}, '所选项中没有可建立子级映射的映射')
        return {'FINISHED'}


def _most_similar(name, bones):
    best, ratio = '', 0.0
    for b in bones:
        r = difflib.SequenceMatcher(None, name, b.name).quick_ratio()
        if r > ratio:
            ratio, best = r, b.name
    return best


class ANIMRET_OT_name_mapping(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.name_mapping'
    bl_label = '名称映射(向右)'
    bl_description = '按名称相似度给自身骨骼自动寻找最接近的来源骨骼'
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        if s.source is None:
            return {'CANCELLED'}
        for i in s.get_selection():
            m = s.mappings[i]
            if m.dest:
                m.source = _most_similar(m.dest, s.source.data.bones)
        return {'FINISHED'}


class ANIMRET_OT_name_mapping_reverse(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.name_mapping_reverse'
    bl_label = '名称映射(向左)'
    bl_description = '按名称相似度给来源骨骼自动寻找最接近的自身骨骼'
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        for i in s.get_selection():
            m = s.mappings[i]
            if m.source:
                m.dest = _most_similar(m.source, dest.data.bones)
        return {'FINISHED'}


class ANIMRET_OT_mirror_mapping(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.mirror_mapping'
    bl_label = '镜像映射'
    bl_description = '若所选映射两端都存在对称骨骼, 则在对称骨骼间建立新映射'
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        did = False
        for i in s.get_selection():
            m = s.mappings[i]
            if m.selected:
                m.selected = False
            d_mir = dest.data.bones.get(bpy.utils.flip_name(m.dest)) \
                if m.dest else None
            s_mir = s.source.data.bones.get(bpy.utils.flip_name(m.source)) \
                if (s.source and m.source) else None
            if d_mir is not None and s_mir is not None \
                    and d_mir.name != m.dest:
                nm, _ = s.add_mapping(d_mir.name, s_mir.name, i + 1)
                nm.selected = True
                # 镜像参数: 欧拉偏移按 YZ 翻转, 其余照搬
                nm.rot_auto = m.rot_auto
                nm.rot_ortho = m.rot_ortho
                nm.rot_offset = (m.rot_offset[0], -m.rot_offset[1],
                                 -m.rot_offset[2])
                nm.loc_enabled = m.loc_enabled
                nm.loc_axes = m.loc_axes
                nm.loc_scale_mode = m.loc_scale_mode
                nm.loc_scale = m.loc_scale
                nm.ik_enabled = m.ik_enabled
                nm.ik_influence = m.ik_influence
                nm.ik_chain = m.ik_chain
                did = True
        if not did:
            self.report({'ERROR'}, '所选项中没有可镜像的映射')
        return {'FINISHED'}


# --- Mwni 风格智能猜测 (名称+同义词+左右分组) ---

_BONE_SYNONYMS = (
    ('clavicle', 'shoulder', 'collar'),
    ('upperarm', 'uparm', 'shoulder'),
    ('lowerarm', 'loarm', 'forearm', 'elbow'),
    ('pinky', 'little'),
    ('pelvis', 'hips', 'hip', 'abdomen'),
    ('upperleg', 'thigh', 'upleg'),
    ('lowerleg', 'shin', 'calf', 'knee'),
    ('foot', 'ankle'),
    ('ball', 'toe'),
)


def _apply_synonym(a, b):
    for synonyms in _BONE_SYNONYMS:
        for key in synonyms:
            if key not in a:
                continue
            for other in (s for s in synonyms if s != key):
                if other in b:
                    return a, b.replace(other, key)
    return a, b


def _group_by_side(names):
    groups = {'l': [], 'r': [], 'x': []}
    for n in names:
        if n in groups['l'] or n in groups['r']:
            continue
        flip = bpy.utils.flip_name(n)
        if flip != n and flip in names:
            low = n.lower()
            if ('r' in low.replace(flip.lower(), '')
                    or 'right' in low or low.endswith('_r')
                    or low.endswith('.r')):
                groups['r'].append(n)
                groups['l'].append(flip)
            else:
                groups['l'].append(n)
                groups['r'].append(flip)
        else:
            groups['x'].append(n)
    return groups


def _guess_pairs(src_names, dest_names):
    matches = []
    for d in dest_names:
        for t in src_names:
            a, b = _apply_synonym(t.lower(), d.lower())
            sm = difflib.SequenceMatcher(None, a, b)
            if sm.find_longest_match(0, len(a), 0, len(b)).size <= 1:
                continue
            matches.append((t, d, sm.ratio()))
    matches.sort(key=lambda m: m[2], reverse=True)
    used_s, used_d, out = set(), set(), []
    for t, d, _r in matches:
        if t in used_s or d in used_d:
            continue
        used_s.add(t)
        used_d.add(d)
        out.append((t, d))
    return out


class ANIMRET_OT_guess_mapping(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.guess_mapping'
    bl_label = '智能猜测映射'
    bl_description = ('按名称相似度+常用同义词+左右侧分组, 给所有尚未映射的目标骨骼'
                      '自动匹配来源骨骼\n姿态模式下有选中骨骼时只处理选中的骨骼')
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        if s.source is None:
            return {'CANCELLED'}
        mapped_dest = {m.dest for m in s.mappings if m.dest}
        mapped_src = {m.source for m in s.mappings if m.source}
        sel = [b.name for b in dest.data.bones if b.select]
        candidates = sel if (context.mode == 'POSE' and sel) else \
            [b.name for b in dest.data.bones]
        dest_names = [n for n in candidates if n not in mapped_dest]
        src_names = [b.name for b in s.source.data.bones
                     if b.name not in mapped_src]
        d_sides = _group_by_side(dest_names)
        s_sides = _group_by_side(src_names)
        added = 0
        for side in ('l', 'r', 'x'):
            for t, d in _guess_pairs(s_sides[side], d_sides[side]):
                s.add_mapping(d, t)
                added += 1
        self.report({'INFO'}, '智能匹配了 %d 对骨骼' % added)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 对齐姿态
# ---------------------------------------------------------------------------

class ANIMRET_OT_capture_align(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.capture_align'
    bl_label = '捕捉对齐姿态'
    bl_description = ('先把目标骨架摆成与来源骨架 rest 相同的姿势 (如 A-pose 对 T-pose),'
                      '\n然后对所选映射捕捉该姿态作为旋转参考 — 之后传递的是动作增量')
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        dest = get_dest_object(context)
        n = 0
        for i in s.get_selection():
            m = s.mappings[i]
            pb = dest.pose.bones.get(m.dest)
            if pb is None:
                continue
            q = (dest.matrix_world @ pb.matrix).decompose()[1]
            m.align_rot = (q.w, q.x, q.y, q.z)
            m.align_set = True
            n += 1
        self.report({'INFO'}, '已为 %d 条映射捕捉对齐姿态' % n)
        return {'FINISHED'}


class ANIMRET_OT_clear_align(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.clear_align'
    bl_label = '清除对齐姿态'
    bl_description = '清除所选映射的对齐姿态, 回到以 rest 为参考'
    bl_options = {'UNDO'}

    def execute(self, context):
        s = get_state(context)
        for i in s.get_selection():
            s.mappings[i].align_set = False
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------

class ANIMRET_OT_preset_save(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.preset_save'
    bl_label = '保存'
    bl_description = '直接覆盖更新当前激活的预设文件 (无需重新命名)'

    def execute(self, context):
        s = get_state(context)
        if not s.active_preset:
            bpy.ops.animret.preset_save_as('INVOKE_DEFAULT')
            return {'FINISHED'}
        path = presets.save_preset(s.active_preset,
                                   s.to_spec(get_dest_object(context)))
        self.report({'INFO'}, '已覆盖更新预设: %s' % path)
        return {'FINISHED'}


class ANIMRET_OT_preset_save_as(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.preset_save_as'
    bl_label = '另存为预设'
    bl_description = '把当前映射表与设置保存为新预设'

    name: bpy.props.StringProperty(name='预设名', default='')

    def invoke(self, context, event):
        s = get_state(context)
        if not self.name:
            self.name = s.active_preset or ''
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        if not self.name.strip():
            self.report({'ERROR'}, '预设名不能为空')
            return {'CANCELLED'}
        s = get_state(context)
        name = presets.sanitize(self.name)
        path = presets.save_preset(name, s.to_spec(get_dest_object(context)))
        s.active_preset = name
        self.report({'INFO'}, '已保存预设: %s' % path)
        return {'FINISHED'}


class ANIMRET_OT_preset_load(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.preset_load'
    bl_label = '加载预设'
    bl_description = ('加载预设到当前目标骨架\n'
                      '骨架改名表也能直接当映射用 (两种配置通用)')
    bl_options = {'UNDO'}

    name: bpy.props.StringProperty()
    subdir: bpy.props.StringProperty(default=presets.PRESET_SUBDIR)

    def execute(self, context):
        s = get_state(context)
        try:
            spec = presets.load_preset(self.name,
                                       self.subdir or presets.PRESET_SUBDIR)
        except Exception as e:
            self.report({'ERROR'}, '加载失败: %s' % e)
            return {'CANCELLED'}
        s.from_spec(spec, context)
        s.active_preset = self.name
        self.report({'INFO'}, '已加载预设 %s (%d 条映射)' %
                    (self.name, len(s.mappings)))
        return {'FINISHED'}


class ANIMRET_OT_preset_delete(_StateOperator, bpy.types.Operator):
    bl_idname = 'animret.preset_delete'
    bl_label = '删除当前预设'
    bl_description = '删除当前激活的预设文件'

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = get_state(context)
        if not s.active_preset:
            self.report({'WARNING'}, '当前没有激活的预设')
            return {'CANCELLED'}
        if presets.delete_preset(s.active_preset):
            self.report({'INFO'}, '已删除预设 %s' % s.active_preset)
        s.active_preset = ''
        return {'FINISHED'}


class ANIMRET_OT_preset_open_folder(bpy.types.Operator):
    bl_idname = 'animret.preset_open_folder'
    bl_label = '打开预设文件夹'

    def execute(self, context):
        d = presets.preset_dir()
        if os.name == 'nt':
            os.startfile(d)
        else:
            import subprocess
            subprocess.Popen(['xdg-open', d])
        return {'FINISHED'}


class ANIMRET_OT_preset_export_file(_StateOperator, bpy.types.Operator,
                                    ExportHelper):
    bl_idname = 'animret.preset_export_file'
    bl_label = '导出配置(JSON)'
    bl_description = '把当前配置导出为 JSON 文件 (可直接交给 CLI 无头烘焙使用)'

    filename_ext = '.json'
    filter_glob: bpy.props.StringProperty(default='*.json', options={'HIDDEN'})

    def execute(self, context):
        s = get_state(context)
        import json
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(s.to_spec(get_dest_object(context)), f,
                      ensure_ascii=False, indent=2)
        self.report({'INFO'}, '已导出: %s' % self.filepath)
        return {'FINISHED'}


class ANIMRET_OT_preset_import_file(_StateOperator, bpy.types.Operator,
                                    ImportHelper):
    bl_idname = 'animret.preset_import_file'
    bl_label = '导入配置(JSON)'
    bl_description = '从任意 JSON 配置文件加载映射与设置'
    bl_options = {'UNDO'}

    filename_ext = '.json'
    filter_glob: bpy.props.StringProperty(default='*.json', options={'HIDDEN'})

    def execute(self, context):
        s = get_state(context)
        try:
            spec = presets.load_preset_file(self.filepath)
        except Exception as e:
            self.report({'ERROR'}, '加载失败: %s' % e)
            return {'CANCELLED'}
        s.from_spec(spec, context)
        s.active_preset = ''
        self.report({'INFO'}, '已导入 %d 条映射' % len(s.mappings))
        return {'FINISHED'}


classes = (
    ANIMRET_OT_bake,
    ANIMRET_OT_bake_all,
    ANIMRET_OT_push_nla,
    ANIMRET_OT_clear_mapping_filter,
    ANIMRET_OT_select_edit_type,
    ANIMRET_OT_select_action,
    ANIMRET_OT_list_action,
    ANIMRET_OT_child_mapping,
    ANIMRET_OT_name_mapping,
    ANIMRET_OT_name_mapping_reverse,
    ANIMRET_OT_mirror_mapping,
    ANIMRET_OT_guess_mapping,
    ANIMRET_OT_capture_align,
    ANIMRET_OT_clear_align,
    ANIMRET_OT_preset_save,
    ANIMRET_OT_preset_save_as,
    ANIMRET_OT_preset_load,
    ANIMRET_OT_preset_delete,
    ANIMRET_OT_preset_open_folder,
    ANIMRET_OT_preset_export_file,
    ANIMRET_OT_preset_import_file,
)
