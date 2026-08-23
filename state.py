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

"""数据状态层: 属性组 + 与 JSON 预设/CLI 共用的 spec 字典互转。

状态只存"参数", 不产生任何场景副作用 (无约束/无驱动) —
所有 update 回调最多只刷新实时预览。
"""

import bpy
from . import core
from . import presets


def _armature_from_mesh(mesh_obj):
    """从网格对象找它绑定的骨架: 优先 Armature 修改器, 再退到父级骨架。"""
    for mod in mesh_obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object is not None \
                and mod.object.type == 'ARMATURE':
            return mod.object
    par = mesh_obj.parent
    if par is not None and par.type == 'ARMATURE':
        return par
    return None


def resolve_dest_object(context=None):
    """解析目标骨架 (无需手动设置)。

    优先级: 显式 scene.animret_dest > 活动对象(骨架/或网格绑定的骨架)
            > 选中项里第一个骨架/绑定网格。
    """
    ctx = context or bpy.context
    explicit = ctx.scene.animret_dest
    if explicit is not None and explicit.type == 'ARMATURE':
        return explicit
    vl = getattr(ctx, 'view_layer', None)
    active = vl.objects.active if vl is not None \
        else getattr(ctx, 'active_object', None)
    if active is not None:
        if active.type == 'ARMATURE':
            return active
        if active.type == 'MESH':
            arm = _armature_from_mesh(active)
            if arm is not None:
                return arm
    for o in getattr(ctx, 'selected_objects', ()) or ():
        if o.type == 'ARMATURE':
            return o
        if o.type == 'MESH':
            arm = _armature_from_mesh(o)
            if arm is not None:
                return arm
    return None


def get_dest_object(context=None):
    return resolve_dest_object(context)


def get_state(context=None):
    obj = get_dest_object(context)
    return obj.data.animret if obj is not None else None


def _refresh_preview(state):
    if state is None:
        return
    core.preview_refresh(state, get_dest_object())


def _on_mapping_edit(self, context):
    _refresh_preview(get_state(context))


def root_bone_names(dest_obj):
    """目标骨架的根骨 (无父级)。根骨承载整体位移, 只传旋转的话角色会原地不动。"""
    return {b.name for b in dest_obj.data.bones
            if b.parent is None} if dest_obj else set()


def setup_root_loc(state, dest_obj):
    """把映射到根骨的那几条自动打开位移传递 (AUTO = 按髋高比缩放并重定基点,
    能吃掉两个骨架摆位不同带来的偏移)。返回被设置的条数。"""
    roots = root_bone_names(dest_obj)
    n = 0
    for m in state.mappings:
        if m.dest in roots and not m.loc_enabled:
            m['loc_enabled'] = True
            m.loc_scale_mode = 'AUTO'
            n += 1
    return n


def mapping_matches(mapping, query):
    """空格分隔的多个词全部命中(自身骨或来源骨任一侧)才算匹配。"""
    if not query:
        return True
    hay = (mapping.dest + ' ' + mapping.source).lower()
    return all(w in hay for w in query.lower().split())


class AnimRetMapping(bpy.types.PropertyGroup):
    """单条骨骼映射: dest(自身骨) ← source(来源骨) + 旋转/位移/IK 参数。"""

    dest: bpy.props.StringProperty(
        name='自身骨骼', description='接收动画的目标骨架骨骼',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    source: bpy.props.StringProperty(
        name='来源骨骼', description='动画来源骨架中的骨骼',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)

    # --- 旋转 ---
    rot_auto: bpy.props.BoolProperty(
        name='自动偏移', default=True,
        description='按两骨架 rest 朝向差自动计算旋转偏移 (精确解, 推荐开启)',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    rot_ortho: bpy.props.BoolProperty(
        name='正交', default=False,
        description='把自动偏移量吸附到 90° 的倍数 (rest 朝向不规范的骨架用)',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    rot_offset: bpy.props.FloatVectorProperty(
        name='手动偏移', subtype='EULER', size=3, default=(0.0, 0.0, 0.0),
        description='在自动偏移基础上追加的手动旋转修正\n'
                    '等同于在姿态模式里绕该骨自身轴向转动它: 整条子链跟着转\n'
                    '朝向本身每次从两侧活 rest 现算, 配置里只记这个相对偏移',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)

    # --- 位移 ---
    loc_enabled: bpy.props.BoolProperty(
        name='位置映射', default=False,
        description='让目标骨跟随来源骨的世界位置 (根骨骼/武器等)',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    loc_axes: bpy.props.BoolVectorProperty(
        name='位移轴向', size=3, default=(True, True, True), subtype='XYZ',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    loc_scale_mode: bpy.props.EnumProperty(
        name='位移缩放', default='NONE',
        items=(('NONE', '原值', '直接复制世界位置 (旧版行为)'),
               ('AUTO', '自动比例', '按两骨架髋高比缩放位移并重定基点 (体型不同时用)'),
               ('MANUAL', '手动比例', '手动指定位移缩放系数')),
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    loc_scale: bpy.props.FloatProperty(
        name='缩放系数', default=1.0, soft_min=0.01, soft_max=100.0,
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    local_enabled: bpy.props.BoolProperty(
        name='局部通道', default=False,
        description='把来源骨自身的局部位移与缩放也传过来 (换算到目标骨轴向, '
                    '按骨长比缩放)\n'
                    '关节式面部绑定的表情几乎全在这两个通道里: 眨眼/张嘴是骨骼'
                    '平移, 只传旋转会什么都传不到\n'
                    '身体骨一般不需要 — 两套 rig 比例不同时局部位移没有可比性',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)

    # --- IK ---
    ik_enabled: bpy.props.BoolProperty(
        name='IK', default=False,
        description='烘焙时做 IK 修正, 让该骨头部精确贴合来源骨世界位置 (手/脚)',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    ik_influence: bpy.props.FloatProperty(
        name='IK权重', default=1.0, min=0.0, max=1.0,
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)
    ik_chain: bpy.props.IntProperty(
        name='链长', default=2, min=2, max=8,
        description='与旧版 IK 约束 chain_count 同义: 实际生效关节数 = 链长 - 1\n'
                    '2 = 仅父骨瞄准; 3 = 解析双骨 IK (手臂/腿推荐)',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_mapping_edit)

    def _on_selected(self, context):
        s = get_state(context)
        if s is not None:
            s.selected_count += 1 if self.selected else -1
            s.update_select(context)

    selected: bpy.props.BoolProperty(
        override={'LIBRARY_OVERRIDABLE'}, update=_on_selected)

    def is_valid(self, state):
        if not self.source or not self.dest:
            return False
        src = state.source
        dest = get_dest_object()
        return (src is not None and self.source in src.data.bones
                and dest is not None and self.dest in dest.data.bones)

    def to_spec(self):
        d = {
            'source': self.source,
            'dest': self.dest,
            'rot': {
                'auto': self.rot_auto,
                'ortho': self.rot_ortho,
                'offset': list(self.rot_offset),
            },
        }
        if self.loc_enabled:
            d['loc'] = {
                'enabled': True,
                'axes': list(self.loc_axes),
                'scale_mode': self.loc_scale_mode,
                'scale': self.loc_scale,
            }
        if self.local_enabled:
            d['local'] = {'enabled': True}
        if self.ik_enabled:
            d['ik'] = {
                'enabled': True,
                'influence': self.ik_influence,
                'chain': self.ik_chain,
            }
        return d

    def from_spec(self, d):
        self['source'] = d.get('source', '')
        self['dest'] = d.get('dest', '')
        rot = d.get('rot', {})
        self['rot_auto'] = bool(rot.get('auto', True))
        self['rot_ortho'] = bool(rot.get('ortho', False))
        self['rot_offset'] = list(rot.get('offset', (0.0, 0.0, 0.0)))
        loc = d.get('loc', {})
        self['loc_enabled'] = bool(loc.get('enabled', False))
        self['loc_axes'] = list(loc.get('axes', (True, True, True)))
        mode = loc.get('scale_mode', 'NONE')
        self.loc_scale_mode = mode if mode in {'NONE', 'AUTO', 'MANUAL'} \
            else 'NONE'
        self['loc_scale'] = float(loc.get('scale', 1.0))
        local = d.get('local', {})
        self['local_enabled'] = bool(local.get('enabled', False)
                                     if isinstance(local, dict) else local)
        ik = d.get('ik', {})
        self['ik_enabled'] = bool(ik.get('enabled', False))
        self['ik_influence'] = float(ik.get('influence', 1.0))
        self['ik_chain'] = int(ik.get('chain', 2))


def _on_source_change(self, context):
    _refresh_preview(self)


def _on_preview_toggle(self, context):
    dest = get_dest_object(context)
    if self.preview:
        core.preview_refresh(self, dest)
    else:
        core.preview_refresh(self, dest)  # preview=False 路径内部即停止+复位


def _on_setting_edit(self, context):
    pass


class AnimRetState(bpy.types.PropertyGroup):
    """挂在目标骨架 Armature 数据上的全部重定向状态。"""

    source: bpy.props.PointerProperty(
        name='动画来源', type=bpy.types.Object,
        override={'LIBRARY_OVERRIDABLE'},
        poll=lambda self, obj: obj.type == 'ARMATURE'
        and obj != resolve_dest_object(),
        update=_on_source_change)

    mappings: bpy.props.CollectionProperty(
        type=AnimRetMapping, override={'LIBRARY_OVERRIDABLE', 'USE_INSERTION'})
    # 派生, 不是存量: 开着同步时, 列表高亮的那一行**就是**当前活动骨骼所在的行。
    #
    # 反过来 (视图选骨 → 列表跟着走) 没有可靠的推送机制可用: 实测把活动骨骼换掉
    # **不会**给依赖图打标记 (depsgraph_update_post 零次), 所以处理器接不到;
    # msgbus 在 --background 里根本不投递, 也就无法验收。而"读"是随时能读的 ——
    # 面板每次重绘都会问一遍这个 getter, 视图里选骨本来就会触发重绘, 于是同步是
    # 免费且必然发生的, 不需要任何回调, 也不需要从 draw 里往数据里写东西。
    stored_mapping: bpy.props.IntProperty(default=-1,
                                           override={'LIBRARY_OVERRIDABLE'})
    active_mapping: bpy.props.IntProperty(
        default=-1, override={'LIBRARY_OVERRIDABLE'},
        get=lambda self: self.get_active_mapping(),
        set=lambda self, value: self.set_active_mapping(value))
    selected_count: bpy.props.IntProperty(
        default=0, override={'LIBRARY_OVERRIDABLE'})

    editing_type: bpy.props.IntProperty(
        description='列表编辑页: 0映射 1旋转 2位移 3IK',
        override={'LIBRARY_OVERRIDABLE'})
    mapping_filter: bpy.props.StringProperty(
        name='搜索', default='', options={'TEXTEDIT_UPDATE'},
        description='按骨骼名过滤映射表 (自身骨/来源骨任一命中即显示); '
                    '空格分隔多个词 = 全部命中才显示',
        override={'LIBRARY_OVERRIDABLE'})
    preview: bpy.props.BoolProperty(
        name='预览', default=False,
        description='实时预览重定向效果 (纯计算, 不创建约束, 关闭即完全复原)',
        override={'LIBRARY_OVERRIDABLE'}, update=_on_preview_toggle)
    sync_select: bpy.props.BoolProperty(
        default=False, name='点列表时同时选骨',
        description='点击列表项时自动激活相应骨骼; 勾选列表项时自动选中相应骨骼\n'
                    '(反方向一直是开的: 在视图里选中某根骨, 列表自动跳到那一行)',
        override={'LIBRARY_OVERRIDABLE'})

    # --- 烘焙设置 ---
    frame_step: bpy.props.FloatProperty(
        name='帧步长', default=1.0, min=0.01, soft_max=10.0,
        override={'LIBRARY_OVERRIDABLE'})
    interpolation: bpy.props.EnumProperty(
        name='插值', default='LINEAR',
        items=(('LINEAR', '线性', '烘焙数据逐帧键控, 线性插值最忠实'),
               ('BEZIER', '贝塞尔', '自动钳制贝塞尔手柄 (便于手工二次编辑)')),
        override={'LIBRARY_OVERRIDABLE'})
    suffix: bpy.props.StringProperty(
        name='后缀', default='_baked',
        description='烘焙产物命名 = 源动作名 + 后缀; 同名直接覆盖更新',
        override={'LIBRARY_OVERRIDABLE'})
    overwrite: bpy.props.BoolProperty(
        name='覆盖同名', default=True,
        description='已存在同名产物时原地覆盖 (保持 NLA 引用), 而不是新建 .001',
        override={'LIBRARY_OVERRIDABLE'})
    bake_mode: bpy.props.EnumProperty(
        name='采样', default='PURE',
        items=(('PURE', '纯数学', '直接评估 FCurve, 不触碰场景。确定性最强、最快, '
                                '与时间轴/场景状态完全无关'),
               ('SCENE', '场景评估', '逐帧评估 depsgraph (来源骨架自带约束/驱动时使用)')),
        override={'LIBRARY_OVERRIDABLE'})
    push_nla: bpy.props.BoolProperty(
        name='推送NLA', default=False,
        description='批量烘焙完成后把产物推为静音 NLA 轨, 便于 FBX 多 take 导出',
        override={'LIBRARY_OVERRIDABLE'})
    assign_result: bpy.props.BoolProperty(
        name='烘焙后指派', default=True,
        description='单个烘焙完成后把产物指派到目标骨架以便回放检查',
        override={'LIBRARY_OVERRIDABLE'})

    active_preset: bpy.props.StringProperty(
        default='', description='当前加载的预设名 ("保存"按钮直接覆盖此预设)',
        override={'LIBRARY_OVERRIDABLE'})
    # 拼出来的组合没有对应文件, 所以"保存"无处可覆盖 —— 这里记的是它是哪一条通路,
    # 面板据此显示名字并把"保存"换成"另存为"。存下来之后它就有文件了, 这一项清空。
    active_route: bpy.props.StringProperty(
        default='', description='当前加载的组合通路 (源家族 → 目标家族)',
        override={'LIBRARY_OVERRIDABLE'})
    source_family: bpy.props.StringProperty(
        name='源骨架家族', default='',
        description='这份配置的来源骨架属于哪个家族 (逗号分隔可写多个别名)\n'
                    '声明了两侧家族的配置才是转换图上的一条边, 才能被别的配置当跳板',
        override={'LIBRARY_OVERRIDABLE'})
    dest_family: bpy.props.StringProperty(
        name='目标骨架家族', default='',
        description='这份配置的目标骨架属于哪个家族 (逗号分隔可写多个别名)',
        override={'LIBRARY_OVERRIDABLE'})

    # ------------------------------------------------------------------
    def row_of_bone(self, bone_name):
        """哪一行说的是这根骨。目标骨优先 —— offset 是加在目标骨上的, 用户在视图里
        点的也是那副骨架。找不到返回 -1。"""
        if not bone_name:
            return -1
        for index, mapping in enumerate(self.mappings):
            if mapping.dest == bone_name:
                return index
        for index, mapping in enumerate(self.mappings):
            if mapping.source == bone_name:
                return index
        return -1

    def get_active_mapping(self):
        """高亮的就是**当前活动骨骼那一行** —— 在视图里点哪根骨, 列表就跳到哪一行,
        offset 直接在下面调。

        **不受 sync_select 管**: 那个开关管的是反方向 (点列表去改视图的选择), 那是有
        副作用的, 该由用户决定; 这个方向只挪一个高亮, 没有副作用, 藏在一个默认关闭的
        下拉菜单里只会让人以为功能坏了。

        当前骨骼不在表里就谁都不高亮 (-1), 而不是退回上一次点过的行: 那会在选到一根
        没配对的骨时把列表甩到一个不相干的位置, 看起来像"它配对到那一行了"。空着才是
        实话 —— 这根骨这张表没说过。"""
        context = bpy.context
        active = getattr(context, 'active_pose_bone', None) or getattr(
            context, 'active_bone', None)
        row = self.row_of_bone(active.name if active is not None else '')
        return row if row >= 0 or active is not None else self.get('stored_mapping', -1)

    def set_active_mapping(self, value):
        self['stored_mapping'] = value
        # 传值进去, 不让它回头读 getter: 开着同步时 getter 答的是"活动骨骼那一行",
        # 而这一刻活动骨骼还是上一根 —— 读回来就会去激活上一行的骨, 于是列表点哪
        # 都跳不动。写入路径必须用刚写进去的那个值。
        self.update_active(bpy.context, index=value)

    def update_active(self, context, index=None):
        index = self.active_mapping if index is None else index
        if self.sync_select and 0 <= index < len(self.mappings):
            m = self.mappings[index]
            dest = get_dest_object(context)
            if dest is not None:
                b = dest.data.bones.get(m.dest)
                if b:
                    dest.data.bones.active = b
            if self.source is not None:
                b = self.source.data.bones.get(m.source)
                if b:
                    self.source.data.bones.active = b

    def update_select(self, context):
        if not self.sync_select:
            return
        dest = get_dest_object(context)
        dest_sel = {m.dest for m in self.mappings if m.selected}
        src_sel = {m.source for m in self.mappings if m.selected}
        if dest is not None:
            for bone in dest.data.bones:
                bone.select = bone.name in dest_sel
        if self.source is not None:
            for bone in self.source.data.bones:
                bone.select = bone.name in src_sel

    def get_selection(self):
        """返回操作对象索引集 (倒序): 有勾选用勾选, 否则用激活项。"""
        indices = []
        if self.selected_count <= 0:
            if 0 <= self.active_mapping < len(self.mappings):
                indices.append(self.active_mapping)
        else:
            for i in range(len(self.mappings) - 1, -1, -1):
                if self.mappings[i].selected:
                    indices.append(i)
        return indices

    def get_mapping_by_dest(self, name):
        if name:
            for i, m in enumerate(self.mappings):
                if m.dest == name:
                    return m, i
        return None, -1

    def add_mapping(self, dest, source, index=-1):
        """同一目标骨只允许一条映射 (重复则更新原条目)。"""
        if index == -1:
            index = self.active_mapping + 1
        m, i = self.get_mapping_by_dest(dest) if dest else (None, -1)
        if m is not None:
            m.source = source
            self.active_mapping = i
            return m, i
        m = self.mappings.add()
        m.dest = dest
        m.source = source
        index = max(0, min(index, len(self.mappings) - 1))
        self.mappings.move(len(self.mappings) - 1, index)
        self.active_mapping = index
        return self.mappings[index], index

    def remove_selected(self):
        for i in self.get_selection():
            self.mappings.remove(i)
        self.active_mapping = min(self.active_mapping,
                                  len(self.mappings) - 1)
        self.selected_count = 0

    # ------------------------------------------------------------------
    def mappings_spec(self):
        return [m.to_spec() for m in self.mappings if m.source and m.dest]

    def settings_spec(self):
        return {
            'frame_step': self.frame_step,
            'interpolation': self.interpolation,
            'suffix': self.suffix,
            'overwrite': self.overwrite,
            'bake_mode': self.bake_mode,
            'fake_user': True,
        }

    def families_spec(self):
        """面板上的两个家族字段 → 落盘的 skeletons 段 (逗号分隔 = 别名列表)。"""
        def split(text):
            return [part.strip() for part in str(text or '').split(',') if part.strip()]
        source, dest = split(self.source_family), split(self.dest_family)
        return {'source': source, 'dest': dest} if source and dest else {}

    def to_spec(self, dest_obj=None):
        # 与骨架转换面板同一种落盘格式 (presets.make_spec), 只有一种形状
        return presets.make_spec(
            self.mappings_spec(),
            source_armature=self.source.name if self.source else '',
            dest_armature=dest_obj.name if dest_obj else '',
            settings=self.settings_spec(),
            skeletons=self.families_spec())

    def from_spec(self, spec, context=None):
        self.mappings.clear()
        self.selected_count = 0
        declared = spec.get('skeletons') or {}
        self.source_family = ', '.join(str(n) for n in (declared.get('source') or []))
        self.dest_family = ', '.join(str(n) for n in (declared.get('dest') or []))
        # 映射表与骨架改名表通用: 改名表的 from/to 就是这里的 source/dest
        # 只记下标: 集合 .add() 扩容会让先前取到的元素引用失效, 存引用会静默写空
        no_loc = []
        for i, (src, dst, extra) in enumerate(presets.normalize_pairs(spec)):
            m = self.mappings.add()
            m.from_spec(dict(extra, source=src, dest=dst))
            if 'loc' not in extra:
                no_loc.append(i)
        # 配置没写 loc 的 (改名表就没这一项), 根骨自动开位移 —— 否则烘出来
        # 只有姿势没有整体移动
        roots = root_bone_names(get_dest_object(context))
        for i in no_loc:
            m = self.mappings[i]
            if m.dest in roots:
                m['loc_enabled'] = True
                m.loc_scale_mode = 'AUTO'
        self.active_mapping = 0 if len(self.mappings) else -1
        settings = spec.get('settings', {})
        if 'frame_step' in settings:
            self['frame_step'] = float(settings['frame_step'])
        if settings.get('interpolation') in {'LINEAR', 'BEZIER'}:
            self.interpolation = settings['interpolation']
        if 'suffix' in settings:
            self['suffix'] = str(settings['suffix'])
        if 'overwrite' in settings:
            self['overwrite'] = bool(settings['overwrite'])
        if settings.get('bake_mode') in {'PURE', 'SCENE'}:
            self.bake_mode = settings['bake_mode']
        src_name = spec.get('source_armature')
        if src_name and src_name in bpy.data.objects:
            obj = bpy.data.objects[src_name]
            if obj.type == 'ARMATURE':
                self.source = obj
        _refresh_preview(self)


classes = (
    AnimRetMapping,
    AnimRetState,
)
