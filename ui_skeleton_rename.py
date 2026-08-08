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

"""界面层: 骨架转换(重命名) — 独立 Panel, 与映射面板(ui.py)完全分开。

配置浏览器直接摊开列出磁盘上的配置, 点一行即加载并展开显示内容;
骨骼名对照表只读展示 (配置由 AI 写文件, 不在面板里手工加骨骼);
转换是双向的 —— 正向按表改名, 反向把名字改回去。
"""

import bpy

from . import presets
from . import skeleton_rename
from .state import resolve_dest_object


class ANIMRETSK_UL_entries(bpy.types.UIList):
    """配置浏览器: 磁盘上的每份配置一行。"""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        row = layout.row(align=True)
        row.label(text=item.label, translate=False, icon='PRESET')

    def draw_filter(self, context, layout):
        pass


class ANIMRETSK_UL_pairs(bpy.types.UIList):
    """当前配置的骨名对照表 (只读)。"""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        dest = resolve_dest_object(context)
        names = {b.name for b in dest.data.bones} if dest else set()
        row = layout.row(align=True)
        # 骨架上现在叫哪个名字, 就高亮哪一侧 —— 一眼看出当前处在哪个方向
        left = row.row()
        left.active = item.old_name in names
        left.label(text=item.old_name, translate=False, icon='BONE_DATA')
        row.label(icon='ARROW_LEFTRIGHT')
        right = row.row()
        right.active = item.new_name in names
        right.label(text=item.new_name, translate=False, icon='BONE_DATA')

    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        pairs = getattr(data, propname)
        query = context.scene.animret_skel.pair_filter
        flags = [self.bitflag_filter_item] * len(pairs)
        if query:
            for i, p in enumerate(pairs):
                if not skeleton_rename.pair_matches(p, query):
                    flags[i] &= ~self.bitflag_filter_item
        return flags, []


class ANIMRETSK_PT_panel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AnimRetarget'
    bl_label = '骨架转换 (重命名)'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.animret_skel

        # ① 导出结构: 作用于"选中的全部骨架"(可多选两个用于对比)
        box = layout.box()
        box.label(text='① 导出骨架结构给 AI 看', icon='EXPORT')
        arms = skeleton_rename.selected_armatures(context)
        box.label(
            text=('将导出: ' + ', '.join(a.name for a in arms)) if arms
            else '先选中骨架 (可同时选中两个用于对比)',
            translate=False, icon='ARMATURE_DATA' if arms else 'INFO')
        row = box.row(align=True)
        row.operator('animret.skeleton_export_structure', icon='EXPORT')
        row.operator('animret.skeleton_open_export_folder', text='',
                     icon='FILE_FOLDER')

        # ② 配置浏览器 + 双向转换
        box = layout.box()
        box.label(text='② 应用 AI 写好的配置', icon='SORTALPHA')

        row = box.row()
        row.template_list('ANIMRETSK_UL_entries', '', s, 'entries', s,
                          'active_entry', rows=4)
        side = row.column(align=True)
        side.operator('animret.skeleton_refresh_entries', text='',
                      icon='FILE_REFRESH')
        side.operator('animret.skeleton_open_rename_folder', text='',
                      icon='FILE_FOLDER')
        side.separator()
        side.operator('animret.skeleton_preset_save', text='', icon='FILE_TICK')
        side.operator('animret.skeleton_preset_save_as', text='', icon='ADD')
        side.operator('animret.skeleton_preset_delete', text='', icon='TRASH')

        if not len(s.entries):
            box.label(text='点右侧刷新按钮扫描配置目录', icon='INFO')

        # 当前配置内容直接摊开
        if len(s.pairs):
            hit = sum(1 for p in s.pairs
                      if skeleton_rename.pair_matches(p, s.pair_filter))
            box.label(text='%s — %s'
                      % (s.display_name or s.active_preset or '当前配置',
                         ('%d / %d 条骨名对照' % (hit, len(s.pairs)))
                         if s.pair_filter else ('%d 条骨名对照' % len(s.pairs))),
                      translate=False)
            row = box.row(align=True)
            row.prop(s, 'pair_filter', text='', icon='VIEWZOOM')
            if s.pair_filter:
                row.operator('animret.skeleton_clear_filter', text='', icon='X')
            box.template_list('ANIMRETSK_UL_pairs', '', s, 'pairs', s,
                              'active_pair', rows=8)

        dest = resolve_dest_object(context)
        box.label(
            text=('改名对象: ' + dest.name) if dest else '没有可改名的活动骨架',
            translate=False, icon='ARMATURE_DATA' if dest else 'INFO')

        # 双向转换: 按骨架当前的命名判断该往哪边走
        fwd = rev = 0
        if dest and len(s.pairs):
            fwd, rev = skeleton_rename.count_directions(
                dest, [(p.old_name, p.new_name) for p in s.pairs])
        row = box.row(align=True)
        col = row.column(align=True)
        col.enabled = fwd > 0
        op = col.operator('animret.skeleton_rename_apply',
                          text='转换 → (%d)' % fwd, icon='FORWARD')
        op.reverse = False
        col = row.column(align=True)
        col.enabled = rev > 0
        op = col.operator('animret.skeleton_rename_apply',
                          text='← 还原 (%d)' % rev, icon='BACK')
        op.reverse = True


classes = (
    ANIMRETSK_UL_entries,
    ANIMRETSK_UL_pairs,
    ANIMRETSK_PT_panel,
)
