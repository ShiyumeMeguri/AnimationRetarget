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

"""界面层: 保留原版 AnimationRetarget 的面板工作流, 但底下零约束零驱动。"""

import bpy

from . import compose
from . import presets
from .state import (get_state, get_dest_object, resolve_dest_object,
                    mapping_matches)


# Bone names are the column the eye actually scans, and they are long
# ('Bip001_R_Finger22') while their settings are a toggle and three short angles.
# Two plain rows would split the width evenly and truncate exactly the half that
# carries the identity, so the name side is given the larger share explicitly.
NAME_COLUMN_FACTOR = 0.45


class ANIMRET_UL_mappings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        s = get_state(context)
        dest = get_dest_object(context)
        layout.alert = not item.is_valid(s)
        layout.active = item.selected or s.selected_count == 0
        if s.editing_type == 0:
            row = layout.row(align=True)
        else:
            split = layout.split(factor=NAME_COLUMN_FACTOR)
            row = split.row(align=True)
            layout = split

        if s.editing_type == 0:
            row.prop(item, 'selected', text='', emboss=False,
                     icon='CHECKBOX_HLT' if item.selected
                     else 'CHECKBOX_DEHLT')
            if dest is not None:
                row.prop_search(item, 'dest', dest.data, 'bones', text='',
                                translate=False, icon='BONE_DATA')
            row.label(icon='BACK')
            if s.source is not None:
                row.prop_search(item, 'source', s.source.data, 'bones',
                                text='', translate=False, icon='BONE_DATA')
        elif s.editing_type == 1:
            row.prop(item, 'rot_auto', icon='CON_ROTLIKE', icon_only=True)
            row.label(text=item.dest, translate=False)
            sub = layout.row(align=True)
            if item.rot_auto:
                sub.prop(item, 'rot_ortho', text='正交', toggle=True)
            sub.prop(item, 'rot_self_only', text='仅本骨', toggle=True)
            sub.prop(item, 'rot_offset', text='')
        elif s.editing_type == 2:
            row.prop(item, 'loc_enabled', icon='CON_LOCLIKE', icon_only=True)
            row.label(text=item.dest, translate=False)
            sub = layout.row(align=True)
            if item.loc_enabled:
                sub.prop(item, 'loc_axes', text='', toggle=True)
                sub.prop(item, 'loc_scale_mode', text='')
                if item.loc_scale_mode == 'MANUAL':
                    sub.prop(item, 'loc_scale', text='')
            sub.prop(item, 'local_enabled', text='局部', toggle=True,
                     icon='CON_TRANSLIKE')
        elif s.editing_type == 3:
            row.prop(item, 'ik_enabled', icon='CON_KINEMATIC', icon_only=True)
            row.label(text=item.dest, translate=False)
            if item.ik_enabled:
                sub = layout.row(align=True)
                sub.prop(item, 'ik_influence', text='权重', slider=True)
                sub.prop(item, 'ik_chain', text='链长')

    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        mappings = getattr(data, propname)
        s = get_state(context)
        query = s.mapping_filter if s else ''
        flags = [self.bitflag_filter_item] * len(mappings)
        if query:
            for i, m in enumerate(mappings):
                if not mapping_matches(m, query):
                    flags[i] &= ~self.bitflag_filter_item
        return flags, []


class ANIMRET_MT_presets(bpy.types.Menu):
    bl_label = '映射配置'

    def draw(self, context):
        layout = self.layout
        edges = compose.load_edges()
        routed = compose.routes(edges)
        counted = {}
        for route in routed:
            spec = compose.compose(route, edges)
            counted[route.key] = len(presets.normalize_pairs(spec))
        alive = [route for route in routed if counted[route.key]]

        if alive:
            layout.label(text='转换 (%d 副骨架张成 %d 对)'
                              % (len(compose.sides(edges)), len(alive)),
                         icon='DECORATE_LINKED')
            # 盘上真有那份文件的打勾, 当场算出来的 (拼的, 或反着读的) 打加号 ——
            # 加号意味着"点了能用, 但还没有文件, 另存为才会固化"。
            for route in alive:
                op = layout.operator('animret.preset_load',
                                     text=route.label(counted[route.key]),
                                     translate=False,
                                     icon='FILE_TICK' if route.direct else 'PLUS')
                op.route = route.key
                op.name = ''
            dropped = [route for route in routed if not counted[route.key]]
            if dropped:
                # 拼得通、但中间骨架把行 join 光了。说出来, 别让它看起来不存在。
                layout.label(text='(%d 对拼得通但没剩下映射: %s)'
                                  % (len(dropped),
                                     ', '.join(r.key for r in dropped[:3])),
                             icon='INFO')

        # 没声明两侧家族的配置上不了图, 但仍然是能直接加载的文件。
        edge_names = {name for name, _spec, _s, _d in edges}
        loose = [(label, name) for label, name in presets.list_all_presets()
                 if name not in edge_names]
        if loose:
            layout.separator()
            layout.label(text='未声明家族/配置 (不参与组合)', icon='FILE_BLANK')
            for label, name in loose:
                op = layout.operator('animret.preset_load', text=label,
                                     translate=False)
                op.name = name
                op.route = ''
        if not alive and not loose:
            layout.label(text='(没有配置, 用右侧按钮保存)')


class ANIMRET_MT_settings(bpy.types.Menu):
    bl_label = '设置'

    def draw(self, context):
        s = get_state(context)
        layout = self.layout
        layout.prop(s, 'sync_select')
        layout.separator()
        layout.prop(s, 'bake_mode')
        layout.prop(s, 'interpolation')
        layout.prop(s, 'frame_step')
        layout.separator()
        layout.prop(s, 'suffix')
        layout.prop(s, 'overwrite')
        layout.prop(s, 'assign_result')
        layout.prop(s, 'push_nla')
        layout.separator()
        layout.operator('animret.push_nla', icon='NLA')
        layout.separator()
        layout.operator('animret.preset_import_file', icon='IMPORT')
        layout.operator('animret.preset_export_file', icon='EXPORT')


class ANIMRET_MT_align(bpy.types.Menu):
    bl_label = '对齐姿态'

    def draw(self, context):
        layout = self.layout


def _draw_mapping_block(layout):
    s = get_state()

    row = layout.row()
    left = row.column_flow(columns=1, align=True)
    box = left.box().row()
    if s.editing_type == 0:
        box_left = box.row(align=True)
        if s.selected_count == len(s.mappings) and len(s.mappings):
            box_left.operator('animret.select_action', text='', emboss=False,
                              icon='CHECKBOX_HLT').action = 'NONE'
        else:
            box_left.operator('animret.select_action', text='', emboss=False,
                              icon='CHECKBOX_DEHLT').action = 'ALL'
            if s.selected_count != 0:
                box_left.operator('animret.select_action', text='',
                                  emboss=False,
                                  icon='UV_SYNC_SELECT').action = 'INVERSE'
    box_right = box.row(align=False)
    box_right.alignment = 'RIGHT'
    box_right.operator('animret.reset_tab', text='', icon='LOOP_BACK')
    box_right.separator()
    for t, (label, icon) in enumerate((
            ('映射', 'PRESET'), ('旋转', 'CON_ROTLIKE'),
            ('位移', 'CON_LOCLIKE'), ('ＩＫ', 'CON_KINEMATIC'))):
        box_right.operator('animret.select_edit_type',
                           text='' if s.editing_type != t else label,
                           icon=icon, emboss=True,
                           depress=s.editing_type == t).selected_type = t

    srow = left.row(align=True)
    srow.prop(s, 'mapping_filter', text='', icon='VIEWZOOM')
    if s.mapping_filter:
        hit = sum(1 for m in s.mappings if mapping_matches(m, s.mapping_filter))
        srow.label(text='%d/%d' % (hit, len(s.mappings)))
        srow.operator('animret.clear_mapping_filter', text='', icon='X')

    left.template_list('ANIMRET_UL_mappings', '', s, 'mappings', s,
                       'active_mapping', rows=7)

    holder = left.box()
    box = holder.row(align=True)
    box.menu(ANIMRET_MT_presets.__name__,
             text=s.active_preset or s.active_route or '映射配置', translate=False,
             icon='PRESET')
    saveable = box.row(align=True)
    # 拼出来的通路没有对应文件, "保存"无处可覆盖 —— 只能另存为。
    saveable.enabled = bool(s.active_preset)
    saveable.operator('animret.preset_save', text='', icon='FILE_TICK')
    box.operator('animret.preset_save_as', text='', icon='ADD')
    removable = box.row(align=True)
    removable.enabled = bool(s.active_preset)
    removable.operator('animret.preset_delete', text='', icon='REMOVE')
    box.separator()
    box.operator('animret.preset_open_folder', text='', icon='FILE_FOLDER')
    if s.active_route and not s.active_preset:
        holder.label(text='组合出来的, 还没有文件 —— 另存为即可固化', icon='PLUS')
    identity = holder.row(align=True)
    identity.prop(s, 'source_family', text='')
    identity.prop(s, 'source_config', text='')
    identity.label(icon='FORWARD')
    identity.prop(s, 'dest_family', text='')
    identity.prop(s, 'dest_config', text='')
    alias_row = holder.row(align=True)
    alias_row.prop(s, 'source_aliases', text='', icon='OUTLINER_COLLECTION')
    alias_row.label(icon='BLANK1')
    alias_row.prop(s, 'dest_aliases', text='', icon='OUTLINER_COLLECTION')

    right = row.column(align=True)
    right.separator()
    right.menu(ANIMRET_MT_settings.__name__, text='', icon='DOWNARROW_HLT')
    right.separator()
    right.operator('animret.list_action', icon='PRESET_NEW',
                   text='').action = 'ADD_SELECT'
    right.operator('animret.list_action', icon='CON_TRACKTO',
                   text='').action = 'ADD_ACTIVE'
    right.operator('animret.list_action', icon='ADD', text='').action = 'ADD'
    right.operator('animret.list_action', icon='REMOVE',
                   text='').action = 'REMOVE'
    right.operator('animret.list_action', icon='TRIA_UP',
                   text='').action = 'UP'
    right.operator('animret.list_action', icon='TRIA_DOWN',
                   text='').action = 'DOWN'
    right.separator()
    right.operator('animret.setup_root_loc', icon='CON_LOCLIKE', text='')
    right.operator('animret.guess_mapping', icon='SHADERFX', text='')
    right.operator('animret.child_mapping', icon='CON_CHILDOF', text='')
    right.operator('animret.name_mapping', icon='FORWARD', text='')
    right.operator('animret.name_mapping_reverse', icon='BACK', text='')
    right.operator('animret.mirror_mapping', icon='MOD_MIRROR', text='')
    right.separator()
    right.menu(ANIMRET_MT_align.__name__, text='', icon='ORIENTATION_GIMBAL')


class ANIMRET_PT_panel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AnimRetarget'
    bl_label = 'Animation Retarget'

    def draw(self, context):
        layout = self.layout
        split = layout.row().split(factor=0.25)
        left = split.column()
        right = split.column()
        left.label(text='目标骨架:')
        left.label(text='动画来源:')

        # 目标骨架: 默认自动识别 (活动骨架 / 选中网格绑定的骨架), 留空即自动
        dest = resolve_dest_object(context)
        explicit = context.scene.animret_dest
        trow = right.row(align=True)
        if explicit is None and dest is not None:
            trow.label(text=dest.name + '  (自动)', icon='ARMATURE_DATA')
            trow.prop(context.scene, 'animret_dest', text='',
                      icon='DOWNARROW_HLT')
        elif explicit is None:
            trow.label(text='选中骨架或绑定网格', icon='INFO')
            trow.prop(context.scene, 'animret_dest', text='',
                      icon='DOWNARROW_HLT')
        else:
            trow.prop(context.scene, 'animret_dest', text='',
                      icon='ARMATURE_DATA', translate=False)

        if dest is None:
            return
        s = dest.data.animret
        right.prop(s, 'source', text='', icon='ARMATURE_DATA',
                   translate=False)
        if s.source is None:
            layout.label(text='选择动画来源骨架以继续操作', icon='INFO')
            return

        _draw_mapping_block(layout)

        row = layout.row()
        row.prop(s, 'preview', text='预览效果',
                 icon='HIDE_OFF' if s.preview else 'HIDE_ON')
        row.operator('animret.bake', text='烘焙动画', icon='NLA')
        layout.row().operator('animret.bake_all', text='批量烘焙所有动画',
                              icon='RENDER_ANIMATION')


classes = (
    ANIMRET_UL_mappings,
    ANIMRET_MT_presets,
    ANIMRET_MT_settings,
    ANIMRET_MT_align,
    ANIMRET_PT_panel,
)
