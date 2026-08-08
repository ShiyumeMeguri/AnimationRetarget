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

"""骨架转换(重命名)操作符层 — 与 ops.py(映射操作符)完全独立。"""

import os

import bpy

from . import presets
from . import skeleton_rename
from .state import resolve_dest_object


class _ArmatureOperator:
    @classmethod
    def poll(cls, context):
        return resolve_dest_object(context) is not None


def _open_folder(path):
    if os.name == 'nt':
        os.startfile(path)
    else:
        import subprocess
        subprocess.Popen(['xdg-open', path])


# ---------------------------------------------------------------------------
# 骨架结构导出
# ---------------------------------------------------------------------------

class ANIMRETSK_OT_export_structure(bpy.types.Operator):
    bl_idname = 'animret.skeleton_export_structure'
    bl_label = '导出骨架结构'
    bl_description = ('把选中的每个骨架各导出一份结构 JSON (层级 + 世界空间头尾 + '
                      '蒙皮命中)\n文件名=骨架名, 固定写入 SkeletonConfig 目录并原地覆盖\n'
                      '同时选中来源与目标两个骨架即可一次导出两份, 交给 AI 对比命名')

    @classmethod
    def poll(cls, context):
        return bool(skeleton_rename.selected_armatures(context))

    def execute(self, context):
        objs = skeleton_rename.selected_armatures(context)
        for obj in objs:
            path = skeleton_rename.write_skeleton_export(obj)
            print('[AnimationRetarget] 已导出骨架结构 %s (%d 根骨骼): %s'
                  % (obj.name, len(obj.data.bones), path))
        self.report({'INFO'}, '已导出 %d 个骨架结构: %s (目录: %s)'
                    % (len(objs), ', '.join(o.name for o in objs),
                       skeleton_rename.skeleton_export_dir()))
        return {'FINISHED'}


class ANIMRETSK_OT_open_export_folder(bpy.types.Operator):
    bl_idname = 'animret.skeleton_open_export_folder'
    bl_label = '打开骨架导出目录'

    def execute(self, context):
        _open_folder(skeleton_rename.skeleton_export_dir())
        return {'FINISHED'}


class ANIMRETSK_OT_open_rename_folder(bpy.types.Operator):
    bl_idname = 'animret.skeleton_open_rename_folder'
    bl_label = '打开重命名预设目录'

    def execute(self, context):
        _open_folder(skeleton_rename.rename_preset_dir())
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 重命名列表编辑
# ---------------------------------------------------------------------------

class ANIMRETSK_OT_refresh_entries(bpy.types.Operator):
    bl_idname = 'animret.skeleton_refresh_entries'
    bl_label = '刷新配置列表'
    bl_description = '重扫配置目录 (AI 刚写完新文件后点一下)'

    def execute(self, context):
        n = skeleton_rename.refresh_entries(context.scene.animret_skel)
        self.report({'INFO'}, '找到 %d 份配置' % n)
        return {'FINISHED'}


class ANIMRETSK_OT_clear_filter(bpy.types.Operator):
    bl_idname = 'animret.skeleton_clear_filter'
    bl_label = '清空搜索'

    def execute(self, context):
        context.scene.animret_skel.pair_filter = ''
        return {'FINISHED'}


class ANIMRETSK_OT_apply_rename(_ArmatureOperator, bpy.types.Operator):
    bl_idname = 'animret.skeleton_rename_apply'
    bl_label = '应用重命名'
    bl_description = ('把当前配置应用到目标骨架 (两阶段防冲突, 支持互换名)\n'
                      '正向 = 表里的 原名→新名; 反向 = 新名→原名, 用来还原\n'
                      '顶点组名 / FCurve 路径 / 同骨架约束 subtarget 由 Blender '
                      '自身联动维护, 蒙皮与已有动画不会因改名而失效')
    bl_options = {'UNDO'}

    reverse: bpy.props.BoolProperty(
        name='反向', default=False,
        description='反着改回去 (新名 → 原名), 即还原')

    def execute(self, context):
        obj = resolve_dest_object(context)
        s = context.scene.animret_skel
        pairs = [(p.new_name, p.old_name) if self.reverse
                 else (p.old_name, p.new_name) for p in s.pairs]
        if not pairs:
            self.report({'WARNING'}, '当前没有加载任何配置')
            return {'CANCELLED'}
        report = skeleton_rename.apply_rename(obj, pairs)
        for old, new, got in report['collided']:
            self.report({'WARNING'}, '目标名冲突: %s → %s 实际改成了 %s'
                        % (old, new, got))
        if report['missing']:
            self.report({'WARNING'}, '骨架上找不到 %d 个源名 (该方向可能已经应用过)'
                        % len(report['missing']))
        self.report({'INFO'}, '%s: 已重命名 %d 根骨骼'
                    % ('还原' if self.reverse else '转换', len(report['renamed'])))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------

class ANIMRETSK_OT_preset_save(bpy.types.Operator):
    bl_idname = 'animret.skeleton_preset_save'
    bl_label = '保存'
    bl_description = '直接覆盖更新当前激活的骨架转换预设 (无需重新命名)'

    def execute(self, context):
        s = context.scene.animret_skel
        if not s.active_preset:
            bpy.ops.animret.skeleton_preset_save_as('INVOKE_DEFAULT')
            return {'FINISHED'}
        obj = resolve_dest_object(context)
        path = skeleton_rename.save_rename_preset(
            s.active_preset, s.to_spec(obj.name if obj else ''))
        self.report({'INFO'}, '已覆盖更新预设: %s' % path)
        return {'FINISHED'}


class ANIMRETSK_OT_preset_save_as(bpy.types.Operator):
    bl_idname = 'animret.skeleton_preset_save_as'
    bl_label = '另存为骨架转换预设'
    bl_description = '把当前重命名列表保存为新预设 (可自定义菜单显示名)'

    name: bpy.props.StringProperty(name='预设名(文件名)', default='')
    display_name: bpy.props.StringProperty(name='显示名称', default='',
        description='菜单里显示的名字, 可用箭头/中文等任意字符, 与文件名分开')

    def invoke(self, context, event):
        s = context.scene.animret_skel
        self.name = s.active_preset or ''
        self.display_name = s.display_name or self.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        if not self.name.strip():
            self.report({'ERROR'}, '预设名不能为空')
            return {'CANCELLED'}
        s = context.scene.animret_skel
        obj = resolve_dest_object(context)
        s.display_name = self.display_name.strip() or self.name.strip()
        name = presets.sanitize(self.name)
        path = skeleton_rename.save_rename_preset(
            name, s.to_spec(obj.name if obj else ''))
        s.active_preset = name
        self.report({'INFO'}, '已保存预设: %s' % path)
        return {'FINISHED'}


class ANIMRETSK_OT_preset_delete(bpy.types.Operator):
    bl_idname = 'animret.skeleton_preset_delete'
    bl_label = '删除当前配置'

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = context.scene.animret_skel
        if not s.active_preset:
            self.report({'WARNING'}, '当前没有选中配置')
            return {'CANCELLED'}
        if presets.delete_preset(s.active_preset, s.active_subdir
                                 or skeleton_rename.SKELETON_RENAME_SUBDIR):
            self.report({'INFO'}, '已删除配置 %s' % s.active_preset)
        s.active_preset = ''
        s.pairs.clear()
        skeleton_rename.refresh_entries(s)
        return {'FINISHED'}


classes = (
    ANIMRETSK_OT_export_structure,
    ANIMRETSK_OT_open_export_folder,
    ANIMRETSK_OT_open_rename_folder,
    ANIMRETSK_OT_refresh_entries,
    ANIMRETSK_OT_clear_filter,
    ANIMRETSK_OT_apply_rename,
    ANIMRETSK_OT_preset_save,
    ANIMRETSK_OT_preset_save_as,
    ANIMRETSK_OT_preset_delete,
)
