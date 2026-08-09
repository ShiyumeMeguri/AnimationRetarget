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

"""bpy 适配层: 骨架快照 / 动作采样 / 直写 FCurve 烘焙 / 无约束预览

烘焙不经过 bpy.ops.nla.bake, 不依赖骨骼选择与可见性, 不创建任何约束 —
根骨骼位移烘焙失败、批量漂移、状态残留这三类问题在结构上不存在。
"""

import re
from contextlib import contextmanager

import bpy
from mathutils import Matrix, Vector, Quaternion, Euler

try:
    from . import retarget_math as rm
except ImportError:          # CLI 直接 import 时无包上下文
    import retarget_math as rm

GENERATED_TAG = 'animret_generated'
SOURCE_TAG = 'animret_source'
NLA_TAG = 'animret_nla'

_BONE_PATH_RE = re.compile(
    r'^pose\.bones\["(.*)"\]\.'
    r'(location|rotation_quaternion|rotation_euler|rotation_axis_angle|scale)$')
_OBJ_PROPS = {'location': 3, 'rotation_euler': 3, 'rotation_quaternion': 4,
              'rotation_axis_angle': 4, 'scale': 3}
_PROP_SIZE = {'location': 3, 'rotation_quaternion': 4, 'rotation_euler': 3,
              'rotation_axis_angle': 4, 'scale': 3}


# ---------------------------------------------------------------------------
# 骨架快照
# ---------------------------------------------------------------------------

def snapshot_skeleton(obj):
    """Object(ARMATURE) → retarget_math.Skeleton"""
    data_bones = list(obj.data.bones)
    name_to_i = {b.name: i for i, b in enumerate(data_bones)}
    specs = []
    for b in data_bones:
        pb = obj.pose.bones.get(b.name)
        specs.append(rm.BoneSpec(
            b.name,
            name_to_i[b.parent.name] if b.parent else -1,
            b.matrix_local.copy(),
            b.use_connect,
            b.use_inherit_rotation,
            getattr(b, 'inherit_scale', 'FULL'),
            b.length,
            pb.rotation_mode if pb else 'QUATERNION',
            getattr(b, 'use_local_location', True)))
    return rm.Skeleton(obj.name, specs, obj.matrix_world.copy())


# ---------------------------------------------------------------------------
# Action FCurve 兼容层 (Blender 4.4+ slotted actions / 旧版)
# ---------------------------------------------------------------------------

def iter_action_fcurves(action):
    """汇总 action 所有 FCurve, 兼容 slotted (4.4+) 与 legacy。"""
    if action is None:
        return []
    layers = getattr(action, 'layers', None)
    if layers:
        fcs = []
        try:
            for layer in layers:
                for strip in layer.strips:
                    if getattr(strip, 'type', 'KEYFRAME') != 'KEYFRAME':
                        continue
                    for slot in action.slots:
                        cb = strip.channelbag(slot)
                        if cb:
                            fcs.extend(cb.fcurves)
            if fcs:
                return fcs
        except Exception:
            pass
    return list(getattr(action, 'fcurves', []))


def iter_action_groups(action):
    """汇总 action 所有通道组, 兼容 slotted (4.4+) 与 legacy。

    通道组按骨骼分组、组名即骨骼名, 所以改写动作里的骨骼引用要连组名一起改。
    """
    if action is None:
        return []
    layers = getattr(action, 'layers', None)
    if layers:
        groups = []
        try:
            for layer in layers:
                for strip in layer.strips:
                    if getattr(strip, 'type', 'KEYFRAME') != 'KEYFRAME':
                        continue
                    for slot in action.slots:
                        cb = strip.channelbag(slot)
                        if cb is not None:
                            groups.extend(getattr(cb, 'groups', ()))
            if groups:
                return groups
        except Exception:
            pass
    return list(getattr(action, 'groups', []))


class _ActionWriter:
    """向 action 写 FCurve 的统一接口 (优先 slotted API, 失败回退 legacy)。"""

    def __init__(self, action, slot_name):
        self.action = action
        self.fcurves = None
        self.groups = None
        if hasattr(action, 'layers'):
            try:
                slot = action.slots[0] if len(action.slots) else \
                    action.slots.new(id_type='OBJECT', name=slot_name)
                layer = action.layers[0] if len(action.layers) else \
                    action.layers.new(name='Layer')
                strip = layer.strips[0] if len(layer.strips) else \
                    layer.strips.new(type='KEYFRAME')
                cb = strip.channelbag(slot, ensure=True)
                self.fcurves = cb.fcurves
                self.groups = getattr(cb, 'groups', None)
                self.slot = slot
                return
            except Exception:
                pass
        self.fcurves = action.fcurves
        self.groups = getattr(action, 'groups', None)
        self.slot = None

    def clear(self):
        try:
            self.fcurves.clear()
        except Exception:
            while len(self.fcurves):
                self.fcurves.remove(self.fcurves[0])

    def ensure_group(self, name):
        if self.groups is None:
            return None
        g = self.groups.get(name) if hasattr(self.groups, 'get') else None
        if g is None:
            try:
                g = self.groups.new(name)
            except Exception:
                g = None
        return g

    def new_curve(self, data_path, index, group_name):
        try:
            fc = self.fcurves.new(data_path=data_path, index=index,
                                  action_group=group_name)
            return fc
        except TypeError:
            fc = self.fcurves.new(data_path=data_path, index=index)
        g = self.ensure_group(group_name)
        if g is not None:
            try:
                fc.group = g
            except Exception:
                pass
        return fc


def assign_action(obj, action):
    ad = obj.animation_data or obj.animation_data_create()
    ad.action = action
    if action is not None and hasattr(ad, 'action_slot'):
        slots = getattr(action, 'slots', None)
        if slots and len(slots) and ad.action_slot is None:
            try:
                ad.action_slot = slots[0]
            except Exception:
                pass


# ---------------------------------------------------------------------------
# PURE 采样: 直接评估 FCurve, 不触碰场景 (确定性 / 无头 / 极快)
# ---------------------------------------------------------------------------

def _fcurve_is_empty(fc):
    """空 FCurve (无关键帧且无修改器) 在 Blender 动画求值里被跳过,
    但 fc.evaluate() 会返回 0.0 — 必须视同"通道不存在", 否则空 scale
    曲线会把 basis 烧成零矩阵 (游戏提取动作常见此毒)。"""
    return len(fc.keyframe_points) == 0 and len(fc.modifiers) == 0


class ActionSampler:
    """把一个 Action 解析为 (帧 → 源骨架 basis 矩阵 + 物体世界矩阵) 的纯函数。"""

    def __init__(self, src_obj, skel, action):
        self.skel = skel
        self.bone_channels = {}   # name -> {prop: [fc or None]*size}
        self.obj_channels = {}    # prop -> [fc or None]*size
        for fc in iter_action_fcurves(action):
            if _fcurve_is_empty(fc):
                continue
            m = _BONE_PATH_RE.match(fc.data_path)
            if m:
                name, prop = m.group(1), m.group(2)
                if name not in skel.index:
                    continue
                arr = self.bone_channels.setdefault(name, {}).setdefault(
                    prop, [None] * _PROP_SIZE[prop])
                if 0 <= fc.array_index < len(arr):
                    arr[fc.array_index] = fc
            elif fc.data_path in _OBJ_PROPS:
                arr = self.obj_channels.setdefault(
                    fc.data_path, [None] * _OBJ_PROPS[fc.data_path])
                if 0 <= fc.array_index < len(arr):
                    arr[fc.array_index] = fc
        # 清掉整组皆空的属性条目 (空 location 组无害但徒增计算)
        for name in list(self.bone_channels.keys()):
            props = self.bone_channels[name]
            for prop in list(props.keys()):
                if all(c is None for c in props[prop]):
                    del props[prop]
            if not props:
                del self.bone_channels[name]

        # 物体级动画的基底: 把当前 basis 从 matrix_world 中剥离, 留下父链部分
        try:
            self.obj_parent_part = src_obj.matrix_world @ \
                src_obj.matrix_basis.inverted()
        except Exception:
            self.obj_parent_part = Matrix.Identity(4)
        self.obj_static_world = src_obj.matrix_world.copy()
        # 物体未键控通道的回退值 = 快照时的静态值 (保留摆位)
        self.obj_fallback = {
            'location': list(src_obj.location),
            'rotation_euler': list(src_obj.rotation_euler),
            'rotation_quaternion': list(src_obj.rotation_quaternion),
            'rotation_axis_angle': list(src_obj.rotation_axis_angle),
            'scale': list(src_obj.scale),
        }
        self.obj_rotation_mode = src_obj.rotation_mode

    @staticmethod
    def _eval(arr, frame, fallback):
        return [arr[i].evaluate(frame) if arr[i] is not None else fallback[i]
                for i in range(len(arr))]

    def has_bone_curves(self):
        return bool(self.bone_channels)

    def world_matrix(self, frame):
        """该帧源骨架物体的世界矩阵 (物体级根运动折叠进来)。"""
        if not self.obj_channels:
            return self.obj_static_world
        loc = Vector(self._eval(self.obj_channels['location'], frame,
                                self.obj_fallback['location'])
                     if 'location' in self.obj_channels
                     else self.obj_fallback['location'])
        mode = self.obj_rotation_mode
        if 'rotation_quaternion' in self.obj_channels or mode == 'QUATERNION':
            q = self._eval(self.obj_channels.get('rotation_quaternion',
                                                 [None] * 4), frame,
                           self.obj_fallback['rotation_quaternion'])
            rot = Quaternion(q).normalized()
        elif 'rotation_axis_angle' in self.obj_channels or mode == 'AXIS_ANGLE':
            aa = self._eval(self.obj_channels.get('rotation_axis_angle',
                                                  [None] * 4), frame,
                            self.obj_fallback['rotation_axis_angle'])
            rot = Quaternion(Vector(aa[1:4]), aa[0])
        else:
            order = mode if rm.rotation_mode_is_euler(mode) else 'XYZ'
            e = self._eval(self.obj_channels.get('rotation_euler', [None] * 3),
                           frame, self.obj_fallback['rotation_euler'])
            rot = Euler(e, order).to_quaternion()
        sca = Vector(self._eval(self.obj_channels['scale'], frame,
                                self.obj_fallback['scale'])
                     if 'scale' in self.obj_channels
                     else self.obj_fallback['scale'])
        return self.obj_parent_part @ Matrix.LocRotScale(loc, rot, sca)

    def basis_map(self, frame):
        """{骨索引: basis 4x4}; 未键控骨骼不出现 (=rest)。

        未键控的分量使用属性默认值 (而非时间轴残留) — 烘焙结果只取决于
        Action 本身, 与场景当前状态完全无关。
        """
        out = {}
        for name, props in self.bone_channels.items():
            i = self.skel.index[name]
            mode = self.skel.bones[i].rotation_mode
            loc = Vector(self._eval(props['location'], frame, (0.0, 0.0, 0.0))
                         if 'location' in props else (0.0, 0.0, 0.0))
            # 严格按骨骼 rotation_mode 取通道 (与 Blender 评估语义一致:
            # 四元数曲线对欧拉模式骨骼不生效, 反之亦然)
            if mode == 'QUATERNION':
                if 'rotation_quaternion' in props:
                    q = self._eval(props['rotation_quaternion'], frame,
                                   (1.0, 0.0, 0.0, 0.0))
                    rot = Quaternion(q)
                    if rot.magnitude > 1e-9:
                        rot.normalize()
                    else:
                        rot = Quaternion()
                else:
                    rot = Quaternion()
            elif mode == 'AXIS_ANGLE':
                aa = self._eval(props.get('rotation_axis_angle',
                                          [None] * 4), frame,
                                (0.0, 0.0, 1.0, 0.0)) \
                    if 'rotation_axis_angle' in props else (0.0, 0.0, 1.0, 0.0)
                axis = Vector(aa[1:4])
                rot = Quaternion(axis, aa[0]) if axis.length_squared > 1e-12 \
                    else Quaternion()
            else:
                e = self._eval(props['rotation_euler'], frame,
                               (0.0, 0.0, 0.0)) \
                    if 'rotation_euler' in props else (0.0, 0.0, 0.0)
                rot = Euler(e, mode).to_quaternion()
            sca = Vector(self._eval(props['scale'], frame, (1.0, 1.0, 1.0))
                         if 'scale' in props else (1.0, 1.0, 1.0))
            out[i] = Matrix.LocRotScale(loc, rot, sca)
        return out


def action_frame_list(action, step=1.0, frame_start=None, frame_end=None):
    fr = action.frame_range
    start = float(fr[0] if frame_start is None else frame_start)
    end = float(fr[1] if frame_end is None else frame_end)
    if end < start:
        start, end = end, start
    step = max(0.01, float(step))
    frames, f = [], start
    while f < end - 1e-9:
        frames.append(f)
        f += step
    frames.append(end)
    return frames


# ---------------------------------------------------------------------------
# 单帧世界矩阵获取 (两种模式共用 solve_frame)
# ---------------------------------------------------------------------------

def src_world_mats_pure(skel, sampler, frame, needed_names):
    obj_w = sampler.world_matrix(frame)
    skel.update_world(obj_w)
    pose = rm.fk_pose(skel, sampler.basis_map(frame))
    return {n: obj_w @ pose[skel.index[n]] for n in needed_names
            if n in skel.index}


def src_world_mats_scene(src_obj, depsgraph, needed_names):
    """SCENE 模式: 读取 depsgraph 评估结果 (源骨架自带约束/驱动也能被烘下来)。"""
    ev = src_obj.evaluated_get(depsgraph)
    w = ev.matrix_world
    return {n: w @ ev.pose.bones[n].matrix for n in needed_names
            if n in ev.pose.bones}


# ---------------------------------------------------------------------------
# 烘焙
# ---------------------------------------------------------------------------

def _decompose_series(bases_per_frame, bone_name, rotation_mode):
    """帧序列 basis → 各通道数值数组 (四元数半球连续 / 欧拉就近兼容)。"""
    locs, quats = [], []
    has_loc = False
    for bases in bases_per_frame:
        b = bases.get(bone_name)
        if b is None:
            loc, rot = Vector(), Quaternion()
        else:
            loc, rot, _ = b.decompose()
        if loc.length_squared > 1e-14:
            has_loc = True
        locs.append(loc)
        quats.append(rot)
    rm.quaternion_make_consistent(quats)
    channels = {}
    if has_loc:
        channels['location'] = [[v[k] for v in locs] for k in range(3)]
    if rotation_mode == 'QUATERNION':
        channels['rotation_quaternion'] = [[q[k] for q in quats]
                                           for k in range(4)]
    elif rotation_mode == 'AXIS_ANGLE':
        vals = []
        for q in quats:
            axis, angle = q.to_axis_angle()
            if axis.length_squared < 1e-12:
                axis = Vector((0.0, 0.0, 1.0))
            vals.append((angle, axis.x, axis.y, axis.z))
        channels['rotation_axis_angle'] = [[v[k] for v in vals]
                                           for k in range(4)]
    else:
        order = rotation_mode if rm.rotation_mode_is_euler(rotation_mode) \
            else 'XYZ'
        eulers, prev = [], None
        for q in quats:
            e = q.to_euler(order, prev) if prev else q.to_euler(order)
            eulers.append(e)
            prev = e
        channels['rotation_euler'] = [[e[k] for e in eulers] for k in range(3)]
    return channels


def _find_or_create_action(name, overwrite):
    act = bpy.data.actions.get(name)
    if act is not None and overwrite:
        return act, True
    if act is not None:
        base, k = name, 1
        while bpy.data.actions.get('%s.%03d' % (base, k)):
            k += 1
        name = '%s.%03d' % (base, k)
    return bpy.data.actions.new(name), False


def bake_action(src_obj, dest_obj, spec, action, settings=None, depsgraph_step=None):
    """把源 Action 重定向烘焙为目标骨架的新 Action (纯数据操作)。

    spec: dict, 见 retarget_math.build_mappings; settings 键:
      frame_step / interpolation('LINEAR'|'BEZIER') / suffix / overwrite /
      bake_mode('PURE'|'SCENE') / frame_start / frame_end / fake_user
    返回 (dest_action, info dict)。
    """
    settings = settings or {}
    try:
        # 脚本里刚改过物体变换时 matrix_world 可能尚未刷新, 强制取最新
        bpy.context.view_layer.update()
    except Exception:
        pass
    src_skel = snapshot_skeleton(src_obj)
    dest_skel = snapshot_skeleton(dest_obj)
    mappings, warnings = rm.build_mappings(src_skel, dest_skel,
                                           spec.get('mappings', []))
    if not mappings:
        raise ValueError('没有有效映射, 无法烘焙')
    needed = [m.src_name for m in mappings]

    frames = action_frame_list(action, settings.get('frame_step', 1.0),
                               settings.get('frame_start'),
                               settings.get('frame_end'))
    bake_mode = settings.get('bake_mode', 'PURE')

    bases_per_frame = []
    if bake_mode == 'SCENE':
        # 场景评估模式: 临时指派动作逐帧评估, 完毕后恢复一切现场。
        # 评估前把源姿态归零 — 未键控通道一律取默认值, 杜绝上一条动作的
        # 姿态残留渗入本条烘焙 (确定性与 PURE 模式对齐)。
        scene = bpy.context.scene
        ad = src_obj.animation_data or src_obj.animation_data_create()
        saved = (ad.action,
                 getattr(ad, 'action_slot', None),
                 scene.frame_current,
                 scene.frame_subframe if hasattr(scene, 'frame_subframe') else 0.0)
        saved_pose = [(pb, pb.location.copy(),
                       pb.rotation_quaternion.copy(),
                       pb.rotation_euler.copy(),
                       tuple(pb.rotation_axis_angle),
                       pb.scale.copy()) for pb in src_obj.pose.bones]
        try:
            for pb in src_obj.pose.bones:
                pb.location = (0.0, 0.0, 0.0)
                pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
                pb.rotation_euler = (0.0, 0.0, 0.0)
                pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
                pb.scale = (1.0, 1.0, 1.0)
            assign_action(src_obj, action)
            for f in frames:
                scene.frame_set(int(f), subframe=max(0.0, min(0.999999, f - int(f))))
                dg = depsgraph_step() if depsgraph_step else \
                    bpy.context.evaluated_depsgraph_get()
                world = src_world_mats_scene(src_obj, dg, needed)
                bases_per_frame.append(rm.solve_frame(dest_skel, mappings, world))
        finally:
            ad.action = saved[0]
            if saved[1] is not None and hasattr(ad, 'action_slot'):
                try:
                    ad.action_slot = saved[1]
                except Exception:
                    pass
            for pb, loc, quat, eul, aa, sca in saved_pose:
                pb.location, pb.rotation_quaternion = loc, quat
                pb.rotation_euler, pb.rotation_axis_angle = eul, aa
                pb.scale = sca
            scene.frame_set(int(saved[2]), subframe=saved[3])
    else:
        sampler = ActionSampler(src_obj, src_skel, action)
        for f in frames:
            world = src_world_mats_pure(src_skel, sampler, f, needed)
            bases_per_frame.append(rm.solve_frame(dest_skel, mappings, world))

    # 收集所有被触及的目标骨骼 (含 IK 链上的非映射骨)
    touched = []
    seen = set()
    for bases in bases_per_frame:
        for n in bases:
            if n not in seen:
                seen.add(n)
                touched.append(n)
    touched.sort(key=lambda n: dest_skel.index[n])

    suffix = settings.get('suffix', '_baked')
    dest_name = action.name + suffix if suffix else action.name + '_retarget'
    dest_action, overwrote = _find_or_create_action(
        dest_name, settings.get('overwrite', True))

    writer = _ActionWriter(dest_action, dest_obj.name)
    writer.clear()
    interpolation = settings.get('interpolation', 'LINEAR')
    ipo_code = 1 if interpolation == 'LINEAR' else 2  # LINEAR / BEZIER
    n = len(frames)
    for bone_name in touched:
        mode = dest_skel.bones[dest_skel.index[bone_name]].rotation_mode
        channels = _decompose_series(bases_per_frame, bone_name, mode)
        for prop, arrays in channels.items():
            path = 'pose.bones["%s"].%s' % (bone_name, prop)
            for idx, values in enumerate(arrays):
                fc = writer.new_curve(path, idx, bone_name)
                fc.keyframe_points.add(n)
                co = [0.0] * (n * 2)
                co[0::2] = frames
                co[1::2] = values
                fc.keyframe_points.foreach_set('co', co)
                try:
                    fc.keyframe_points.foreach_set('interpolation',
                                                   [ipo_code] * n)
                except Exception:
                    for kp in fc.keyframe_points:
                        kp.interpolation = interpolation
                if interpolation == 'BEZIER':
                    for kp in fc.keyframe_points:
                        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
                fc.update()

    dest_action.use_fake_user = bool(settings.get('fake_user', True))
    dest_action[GENERATED_TAG] = 1
    dest_action[SOURCE_TAG] = action.name
    info = {'frames': n, 'bones': len(touched), 'warnings': warnings,
            'overwrote': overwrote, 'mode': bake_mode}
    return dest_action, info


def list_bakeable_actions(src_obj):
    """筛选能作用于源骨架的 Action: 含至少一条源骨骼曲线, 且不是本插件产物。"""
    skel_names = {b.name for b in src_obj.data.bones}
    out = []
    for act in bpy.data.actions:
        if act.get(GENERATED_TAG):
            continue
        for fc in iter_action_fcurves(act):
            if _fcurve_is_empty(fc):
                continue
            m = _BONE_PATH_RE.match(fc.data_path)
            if m and m.group(1) in skel_names:
                out.append(act)
                break
    return out


def bake_all(src_obj, dest_obj, spec, settings=None, actions=None,
             on_progress=None):
    """批量烘焙: 逐 Action 纯函数处理, 互不影响, 不触碰场景状态。"""
    actions = actions if actions is not None else list_bakeable_actions(src_obj)
    results, errors = [], []
    for i, act in enumerate(actions):
        if on_progress:
            on_progress(i, len(actions), act.name)
        try:
            dest_action, info = bake_action(src_obj, dest_obj, spec, act,
                                            settings)
            results.append((act, dest_action, info))
        except Exception as e:
            errors.append((act.name, str(e)))
    return results, errors


def push_to_nla(dest_obj, dest_actions):
    """把烘焙产物推送为静音 NLA 轨 (FBX 多 take 导出用)。可重复执行 (先清旧)。"""
    ad = dest_obj.animation_data or dest_obj.animation_data_create()
    for track in [t for t in ad.nla_tracks if t.get(NLA_TAG)]:
        ad.nla_tracks.remove(track)
    for act in dest_actions:
        track = ad.nla_tracks.new()
        track[NLA_TAG] = 1
        track.name = act.name
        track.mute = True
        start = int(round(act.frame_range[0]))
        strip = track.strips.new(act.name, start, act)
        strip.name = act.name


# ---------------------------------------------------------------------------
# 无约束实时预览 (frame_change / depsgraph 处理器, 纯会话态, 不写入 .blend)
# ---------------------------------------------------------------------------

_preview_jobs = {}     # dest_armature_data_name -> dict(精编预览所需)
_in_preview = False


def _build_preview_job(state, dest_obj):
    src_obj = state.source
    if src_obj is None or dest_obj is None:
        return None
    src_skel = snapshot_skeleton(src_obj)
    dest_skel = snapshot_skeleton(dest_obj)
    mappings, _ = rm.build_mappings(src_skel, dest_skel, state.mappings_spec())
    if not mappings:
        return None
    return {
        'src': src_obj.name, 'dest': dest_obj.name,
        'dest_skel': dest_skel,
        'mappings': mappings,
        'needed': [m.src_name for m in mappings],
        'touched': set(),
    }


def preview_refresh(state, dest_obj):
    """预览参数变化 (映射/偏移被编辑) 时重建作业。"""
    key = dest_obj.data.name if dest_obj else None
    if state.preview and key:
        job = _build_preview_job(state, dest_obj)
        if job:
            _preview_jobs[key] = job
            _apply_preview_all()
            return
    if key in _preview_jobs:
        _preview_stop(key)


def _preview_stop(key):
    job = _preview_jobs.pop(key, None)
    if job is None:
        return
    dest_obj = bpy.data.objects.get(job['dest'])
    if dest_obj is None or dest_obj.type != 'ARMATURE':
        return
    ident = Matrix.Identity(4)
    for name in job['touched']:
        pb = dest_obj.pose.bones.get(name)
        if pb is not None:
            pb.matrix_basis = ident.copy()


def preview_stop_all():
    for key in list(_preview_jobs.keys()):
        _preview_stop(key)


def _apply_preview_all(depsgraph=None):
    global _in_preview
    if _in_preview or not _preview_jobs:
        return
    _in_preview = True
    try:
        if depsgraph is None:
            depsgraph = bpy.context.evaluated_depsgraph_get()
        for key in list(_preview_jobs.keys()):
            job = _preview_jobs[key]
            src_obj = bpy.data.objects.get(job['src'])
            dest_obj = bpy.data.objects.get(job['dest'])
            if src_obj is None or dest_obj is None:
                _preview_jobs.pop(key, None)
                continue
            job['dest_skel'].update_world(dest_obj.matrix_world)
            world = src_world_mats_scene(src_obj, depsgraph, job['needed'])
            bases = rm.solve_frame(job['dest_skel'], job['mappings'], world)
            changed = False
            for name, basis in bases.items():
                pb = dest_obj.pose.bones.get(name)
                if pb is None:
                    continue
                job['touched'].add(name)
                cur = pb.matrix_basis
                diff = False
                for r in range(4):
                    for c in range(4):
                        if abs(cur[r][c] - basis[r][c]) > 1e-6:
                            diff = True
                            break
                    if diff:
                        break
                if diff:
                    pb.matrix_basis = basis
                    changed = True
            if changed:
                dest_obj.update_tag()
    finally:
        _in_preview = False


@contextmanager
def preview_suspended():
    """烘焙期间挂起预览作业 (SCENE 模式逐帧评估时避免预览写姿态干扰)。"""
    global _preview_jobs
    saved = _preview_jobs
    _preview_jobs = {}
    try:
        yield
    finally:
        _preview_jobs = saved


_releasing = False


def release_deleted_refs():
    """放开指向"已被删除对象"的绑定, 返回清掉的条数。

    目标骨架/动画来源是 ID 指针, 会给对象记一个 user。用户在视图里删骨架只是
    从集合解链, 对象带着我们这个 user 赖在 bpy.data 里, "清理未使用数据" 就
    永远清不掉它 (连它的骨架数据一起留下)。
    判据用 users_collection: 任何集合都不在 = 真的被删了; 只是没链接到场景
    (放在游离集合里) 仍然算数, 不能误清。
    """
    global _releasing
    if _releasing:
        return 0
    n = 0
    _releasing = True
    try:
        for scene in bpy.data.scenes:
            obj = getattr(scene, 'animret_dest', None)
            if obj is not None and not obj.users_collection:
                scene.animret_dest = None
                n += 1
        for arm in bpy.data.armatures:
            state = getattr(arm, 'animret', None)
            if state is None:
                continue
            obj = state.source
            if obj is not None and not obj.users_collection:
                state.source = None
                n += 1
    finally:
        _releasing = False
    return n


def _on_frame_change(scene, depsgraph=None):
    _apply_preview_all(depsgraph)


def _on_depsgraph_update(scene, depsgraph=None):
    release_deleted_refs()
    _apply_preview_all(depsgraph)


def _on_load_post(_dummy=None):
    """打开文件时强制关闭一切预览 — 预览是会话态, 绝不常驻。"""
    _preview_jobs.clear()
    release_deleted_refs()
    for arm in bpy.data.armatures:
        st = getattr(arm, 'animret', None)
        if st is not None and st.preview:
            st['preview'] = False   # 绕过 update 回调直接落值


_handlers = (
    (bpy.app.handlers.frame_change_post, _on_frame_change),
    (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
    (bpy.app.handlers.load_post, _on_load_post),
)


def register_handlers():
    unregister_handlers()
    for collection, fn in _handlers:
        handler = bpy.app.handlers.persistent(fn)
        collection.append(handler)


def unregister_handlers():
    for collection, fn in _handlers:
        for h in list(collection):
            if getattr(h, '__name__', '') == fn.__name__:
                collection.remove(h)
