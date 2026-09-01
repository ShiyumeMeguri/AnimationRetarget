# AnimationRetarget 无头自检 (养蛊场)
#
# 运行方式 (任选其一):
#   blender -b --factory-startup --python tests/headless_selftest.py
#   python tests/headless_selftest.py        (需 pip install bpy)
#
# 用 Blender depsgraph 求值结果作为 ground truth, 对抗性验证纯数学内核:
#   1. FK 忠实度 (connected / inherit_rotation / inherit_scale / euler / scale)
#   2. 与旧版约束 (COPY_ROTATION + COPY_LOCATION) 的数值等价
#   2.5 手动偏移的层级性 (转父骨带动整条子链) 与对齐姿态捕捉的等价性
#   3. 自重定向恒等性 (相同骨架 → 输出 == 输入)
#   4. IK 可达性与朝向保持
#   5. 批量烘焙确定性 + 顺序无关性 (漂移在结构上不存在的证明)
#   6. 根骨骼位移 (含物体级根运动折叠) 绝不丢失
#   7. 隐藏骨骼照常烘焙 (旧版 nla.bake 的致命缺陷)
#   8. 状态零残留 (无约束 / 不动源动作指派 / 不动姿态)
#   9. JSON 预设覆盖保存 / 往返一致
#  10. 操作符集成 + CLI 全链路 (含 FBX 导出)

import math
import os
import sys
import tempfile
import traceback

import bpy
from mathutils import Matrix, Vector, Quaternion, Euler

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ADDON)
for p in (_PARENT,):
    if p not in sys.path:
        sys.path.insert(0, p)

PKG = os.path.basename(_ADDON)
addon = __import__(PKG)
core = __import__(PKG + '.core', fromlist=['x'])
rm = __import__(PKG + '.retarget_math', fromlist=['x'])
presets_mod = __import__(PKG + '.presets', fromlist=['x'])

FAILED = []
PASSED = []


def check(name, cond, detail=''):
    if cond:
        PASSED.append(name)
        print('  [PASS] %s' % name)
    else:
        FAILED.append((name, detail))
        print('  [FAIL] %s  %s' % (name, detail))


def quat_angle(a, b):
    d = abs(max(-1.0, min(1.0, a.normalized().dot(b.normalized()))))
    return 2.0 * math.acos(d)


def mat_rot_err(a, b):
    return quat_angle(a.decompose()[1], b.decompose()[1])


def mat_loc_err(a, b):
    return (a.to_translation() - b.to_translation()).length


# ---------------------------------------------------------------------------
# 场景搭建
# ---------------------------------------------------------------------------

def reset_blend():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def build_armature(name, bones, location=(0, 0, 0), rotation=(0, 0, 0),
                   scale=1.0):
    """bones: list of (name, head, tail, roll, parent, connect, inh_rot, inh_scale)"""
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = {}
    for (bname, head, tail, roll, parent, connect, inh_rot, inh_scale) in bones:
        eb = arm.edit_bones.new(bname)
        eb.head, eb.tail, eb.roll = Vector(head), Vector(tail), roll
        if parent:
            eb.parent = ebs[parent]
            eb.use_connect = connect
        eb.use_inherit_rotation = inh_rot
        eb.inherit_scale = inh_scale
        ebs[bname] = eb
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.location = location
    obj.rotation_euler = rotation
    obj.scale = (scale, scale, scale)
    bpy.context.view_layer.update()
    return obj


SRC_BONES = [
    # name        head             tail             roll  parent      con    inhR  inhS
    ('hips',     (0, 0, 1.00),    (0, 0.12, 1.05),  0.30, None,       False, True, 'FULL'),
    ('spine',    (0, 0.12, 1.05), (0, 0.10, 1.35), -0.20, 'hips',     True,  True, 'FULL'),
    ('twist',    (0.04, 0, 1.35), (0.04, 0, 1.50),  0.70, 'spine',    False, False, 'FULL'),
    ('upperarm', (0.10, 0, 1.45), (0.40, 0, 1.45),  0.10, 'twist',    False, True, 'NONE'),
    ('forearm',  (0.40, 0, 1.45), (0.65, 0, 1.45), -0.50, 'upperarm', True,  True, 'FULL'),
    ('hand',     (0.65, 0, 1.45), (0.75, 0, 1.45),  0.00, 'forearm',  True,  True, 'FULL'),
]

DST_BONES = [
    ('Root',  (0, 0, 0.00),     (0, 0.10, 0.00),  0.00, None,    False, True, 'FULL'),
    ('Hips',  (0, 0, 1.25),     (0, 0.15, 1.30),  0.00, 'Root',  False, True, 'FULL'),
    ('Spine', (0, 0.15, 1.30),  (0, 0.12, 1.70), -0.55, 'Hips',  True,  True, 'FULL'),
    ('UpArm', (0.12, 0, 1.65),  (0.50, 0, 1.62),  0.35, 'Spine', False, True, 'FULL'),
    ('LoArm', (0.50, 0, 1.62),  (0.82, 0, 1.60),  0.90, 'UpArm', True,  True, 'FULL'),
    ('Hand',  (0.82, 0, 1.60),  (0.95, 0, 1.60), -0.15, 'LoArm', True,  True, 'FULL'),
]


def key_bone(obj, bone, frame, quat=None, loc=None, euler=None, scale=None):
    pb = obj.pose.bones[bone]
    if quat is not None:
        pb.rotation_mode = 'QUATERNION'
        pb.rotation_quaternion = quat
        pb.keyframe_insert('rotation_quaternion', frame=frame)
    if euler is not None:
        pb.rotation_mode = 'XYZ'
        pb.rotation_euler = euler
        pb.keyframe_insert('rotation_euler', frame=frame)
    if loc is not None:
        pb.location = loc
        pb.keyframe_insert('location', frame=frame)
    if scale is not None:
        pb.scale = scale
        pb.keyframe_insert('scale', frame=frame)


def make_action(obj, name):
    ad = obj.animation_data or obj.animation_data_create()
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    ad.action = act
    if hasattr(ad, 'action_slot'):
        slots = getattr(act, 'slots', None)
        if slots is not None and len(slots) == 0:
            try:
                ad.action_slot = act.slots.new(id_type='OBJECT', name=obj.name)
            except Exception:
                pass
        elif slots:
            ad.action_slot = slots[0]
    return act


def reset_pose(obj):
    """键完动作后把姿态属性归零 — 未键控通道的 depsgraph 取值即默认值,
    与纯采样器的确定性默认值假设对齐。"""
    for pb in obj.pose.bones:
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
        pb.rotation_axis_angle = (0, 0, 1, 0)
        pb.scale = (1, 1, 1)


def build_src_actions(src):
    acts = {}
    acts['Walk'] = make_action(src, 'Walk')
    for f in range(1, 41):
        t = f / 40.0 * 2 * math.pi
        key_bone(src, 'hips', f,
                 quat=Quaternion((1, 0, 0), 0.25 * math.sin(t)),
                 loc=(0.1 * math.sin(2 * t), 0.05 * math.cos(t),
                      0.04 * abs(math.sin(t))))
        key_bone(src, 'spine', f,
                 quat=Quaternion((0, 0, 1), 0.35 * math.sin(t + 0.5)))
        key_bone(src, 'twist', f,
                 quat=Quaternion((0, 1, 0), 0.6 * math.sin(t)))
        key_bone(src, 'upperarm', f,
                 quat=Quaternion(Vector((0.2, 0.7, 0.4)).normalized(),
                                 0.9 * math.sin(t)))
        key_bone(src, 'forearm', f,
                 quat=Quaternion((0, 0, 1), 0.8 * abs(math.sin(t))))
        key_bone(src, 'hand', f,
                 quat=Quaternion((1, 0, 0), 0.4 * math.cos(t)))
        if f % 10 == 1:
            key_bone(src, 'spine', f, scale=(1.0, 1.0 + 0.1 * math.sin(t), 1.0))

    acts['Run'] = make_action(src, 'Run')
    for f in range(1, 31):
        t = f / 30.0 * 2 * math.pi
        key_bone(src, 'hips', f,
                 quat=Quaternion((0, 1, 0), 0.2 * math.cos(t)),
                 loc=(0, 0.02 * math.sin(3 * t), 0))
        key_bone(src, 'upperarm', f,
                 quat=Quaternion((1, 0, 0), 1.2 * math.sin(t)))
        key_bone(src, 'forearm', f,
                 quat=Quaternion((0, 0, 1), 0.5 + 0.5 * math.sin(t)))
        # 物体级根运动 (旧版 bake_types={'POSE'} 会彻底丢掉这个!)
        src.location = (0.5 * t / (2 * math.pi), 0.3 + 0.0, 0)
        src.keyframe_insert('location', frame=f)
    src.location = (0.3, -0.2, 0.1)

    acts['Idle'] = make_action(src, 'Idle')
    for f in range(1, 21):
        t = f / 20.0 * 2 * math.pi
        key_bone(src, 'upperarm', f, euler=(0.3 * math.sin(t), 0.1, 0))
        key_bone(src, 'hand', f, quat=Quaternion((0, 1, 0), 0.2 * math.sin(t)))

    junk = bpy.data.actions.new('Junk')
    junk.use_fake_user = True
    fc = junk.fcurves.new(data_path='pose.bones["nonexistent"].location',
                          index=0) if hasattr(junk, 'fcurves') else None
    if fc:
        fc.keyframe_points.insert(1, 0.5)
    acts['Junk'] = junk
    reset_pose(src)
    return acts


def base_spec(loc_scale_mode='NONE', ik_hand=False, auto=True):
    md = [
        {'source': 'hips', 'dest': 'Hips',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0]},
         'loc': {'enabled': True, 'axes': [True, True, True],
                 'scale_mode': loc_scale_mode, 'scale': 1.0}},
        {'source': 'spine', 'dest': 'Spine',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0]}},
        {'source': 'upperarm', 'dest': 'UpArm',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0]}},
        {'source': 'forearm', 'dest': 'LoArm',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0]}},
        {'source': 'hand', 'dest': 'Hand',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0]}},
    ]
    if ik_hand:
        md[-1]['ik'] = {'enabled': True, 'influence': 1.0, 'chain': 3}
    return {'format': 'AnimationRetarget', 'version': 2, 'settings': {},
            'mappings': md}


# ---------------------------------------------------------------------------
# Phase 1: FK 忠实度 — 纯数学 FK vs depsgraph
# ---------------------------------------------------------------------------

def phase_fk():
    print('\n== Phase 1: FK 忠实度 (vs depsgraph) ==')
    reset_blend()
    src = build_armature('SrcRig', SRC_BONES, location=(0.3, -0.2, 0.1),
                         rotation=(0, 0, math.radians(15)), scale=0.01)
    acts = build_src_actions(src)
    scene = bpy.context.scene

    for act_name in ('Walk', 'Idle'):
        act = acts[act_name]
        core.assign_action(src, act)
        reset_pose(src)   # 未键控通道 = 默认值, 与纯采样器的确定性语义对齐
        skel = core.snapshot_skeleton(src)
        sampler = core.ActionSampler(src, skel, act)
        max_rot = max_loc = 0.0
        per_bone = {}
        frames = core.action_frame_list(act)
        for f in frames:
            scene.frame_set(int(f))
            dg = bpy.context.evaluated_depsgraph_get()
            ev = src.evaluated_get(dg)
            obj_w = sampler.world_matrix(f)
            skel.update_world(obj_w)
            pose = rm.fk_pose(skel, sampler.basis_map(f))
            for i, b in enumerate(skel.bones):
                truth = ev.matrix_world @ ev.pose.bones[b.name].matrix
                mine = obj_w @ pose[i]
                re_, le_ = mat_rot_err(truth, mine), mat_loc_err(truth, mine)
                pb = per_bone.setdefault(b.name, [0.0, 0.0])
                pb[0] = max(pb[0], re_)
                pb[1] = max(pb[1], le_)
                max_rot = max(max_rot, re_)
                max_loc = max(max_loc, le_)
        for bn, (re_, le_) in per_bone.items():
            if re_ > 1e-3 or le_ > 1e-5:
                print('    bone %-9s rot %.3e loc %.3e' % (bn, re_, le_))
        check('FK[%s] 旋转误差 %.2e' % (act_name, max_rot), max_rot < 1e-3,
              'max %.3e rad' % max_rot)
        check('FK[%s] 位置误差 %.2e' % (act_name, max_loc), max_loc < 1e-5,
              'max %.3e' % max_loc)

    # 物体级动画折叠
    act = acts['Run']
    core.assign_action(src, act)
    skel = core.snapshot_skeleton(src)
    sampler = core.ActionSampler(src, skel, act)
    max_err = 0.0
    for f in core.action_frame_list(act):
        scene.frame_set(int(f))
        dg = bpy.context.evaluated_depsgraph_get()
        ev = src.evaluated_get(dg)
        max_err = max(max_err, (sampler.world_matrix(f).to_translation()
                                - ev.matrix_world.to_translation()).length)
    check('物体级根运动折叠误差 %.2e' % max_err, max_err < 1e-5)


# ---------------------------------------------------------------------------
# Phase 2: 与旧版约束的数值等价 + 恒等性 + IK
# ---------------------------------------------------------------------------

def phase_equivalence():
    print('\n== Phase 2: 旧式约束等价 / 自重定向恒等 / IK ==')
    reset_blend()
    src = build_armature('SrcRig', SRC_BONES, location=(0.3, -0.2, 0.1),
                         rotation=(0, 0, math.radians(15)), scale=0.01)
    acts = build_src_actions(src)
    dst = build_armature('DstRig', DST_BONES, location=(-0.5, 0.4, 0))
    dst.data.bones['Root'].hide = True  # 老插件的死穴: 隐藏骨

    pairs = [('hips', 'Hips'), ('spine', 'Spine'), ('upperarm', 'UpArm'),
             ('forearm', 'LoArm'), ('hand', 'Hand')]
    # 旧式约束 ground truth: 世界空间复制旋转 (+根骨复制位置)
    for s_name, d_name in pairs:
        pb = dst.pose.bones[d_name]
        cr = pb.constraints.new('COPY_ROTATION')
        cr.target, cr.subtarget = src, s_name
        if d_name == 'Hips':
            cp = pb.constraints.new('COPY_LOCATION')
            cp.target, cp.subtarget = src, s_name

    spec = base_spec(auto=False)   # Q=I ⇔ 纯世界复制
    core.assign_action(src, acts['Walk'])
    reset_pose(src)
    src_skel = core.snapshot_skeleton(src)
    dest_skel = core.snapshot_skeleton(dst)
    mappings, warns = rm.build_mappings(src_skel, dest_skel, spec['mappings'])
    check('build_mappings 无警告', not warns, str(warns))
    sampler = core.ActionSampler(src, src_skel, acts['Walk'])
    needed = [m.src_name for m in mappings]

    scene = bpy.context.scene
    max_rot = max_loc = 0.0
    for f in core.action_frame_list(acts['Walk']):
        scene.frame_set(int(f))
        dg = bpy.context.evaluated_depsgraph_get()
        ev = dst.evaluated_get(dg)
        world, sb = core.src_world_mats_pure(src_skel, sampler, f, needed)
        dest_skel.update_world(dst.matrix_world)
        bases = rm.solve_frame(dest_skel, mappings, world, sb)
        for s_name, d_name in pairs:
            truth = ev.matrix_world @ ev.pose.bones[d_name].matrix
            # 把 basis 套进快照 FK 得到我的世界矩阵
            basis_map = {dest_skel.index[n]: b for n, b in bases.items()}
            pose = rm.fk_pose(dest_skel, basis_map)
            mine = dst.matrix_world @ pose[dest_skel.index[d_name]]
            max_rot = max(max_rot, mat_rot_err(truth, mine))
            if d_name == 'Hips':
                max_loc = max(max_loc, mat_loc_err(truth, mine))
    check('约束等价: 旋转误差 %.2e' % max_rot, max_rot < 1e-3)
    check('约束等价: 根位移误差 %.2e' % max_loc, max_loc < 1e-5)

    for s_name, d_name in pairs:
        pb = dst.pose.bones[d_name]
        for con in list(pb.constraints):
            pb.constraints.remove(con)

    # 自重定向恒等性: src → src 复制体
    # 用 Run (无缩放键) 验证: 设计上不传递缩放, 缩放动画下恒等性本就不成立
    src2 = build_armature('SrcRig2', SRC_BONES, location=(0.3, -0.2, 0.1),
                          rotation=(0, 0, math.radians(15)), scale=0.01)
    spec_id = {'mappings': [
        {'source': n, 'dest': n,
         'rot': {'auto': True, 'ortho': False, 'offset': [0, 0, 0]},
         'loc': ({'enabled': True, 'axes': [True, True, True],
                  'scale_mode': 'NONE', 'scale': 1.0}
                 if n == 'hips' else {'enabled': False})}
        for n, *_ in [(b[0],) for b in SRC_BONES]]}
    skel_a = core.snapshot_skeleton(src)
    skel_b = core.snapshot_skeleton(src2)
    maps_id, _ = rm.build_mappings(skel_a, skel_b, spec_id['mappings'])
    sampler = core.ActionSampler(src, skel_a, acts['Run'])
    needed = [m.src_name for m in maps_id]
    max_rot = max_loc = 0.0
    for f in core.action_frame_list(acts['Run']):
        world, sb = core.src_world_mats_pure(skel_a, sampler, f, needed)
        bases = rm.solve_frame(skel_b, maps_id, world, sb)
        basis_map = {skel_b.index[n]: b for n, b in bases.items()}
        pose = rm.fk_pose(skel_b, basis_map)
        for n in needed:
            mine = skel_b.matrix_world @ pose[skel_b.index[n]]
            max_rot = max(max_rot, mat_rot_err(world[n], mine))
        max_loc = max(max_loc, mat_loc_err(
            world['hips'], skel_b.matrix_world @ pose[skel_b.index['hips']]))
    # mathutils 为 float32: 0.04°/亚微米级即该链深下的单精度量化极限
    check('自重定向恒等: 旋转 %.2e' % max_rot, max_rot < 1e-3)
    check('自重定向恒等: 位移 %.2e' % max_loc, max_loc < 5e-7)

    # IK 可达性: chain=3 双骨解析, 手腕头部应贴合目标 (可达时)
    spec_ik = base_spec(ik_hand=True)
    maps_ik, _ = rm.build_mappings(src_skel, dest_skel, spec_ik['mappings'])
    sampler = core.ActionSampler(src, src_skel, acts['Walk'])
    needed = [m.src_name for m in maps_ik]
    reach_fail = 0
    orient_err = 0.0
    up_len = (Vector(DST_BONES[4][1]) - Vector(DST_BONES[3][1])).length
    lo_len = (Vector(DST_BONES[5][1]) - Vector(DST_BONES[4][1])).length
    for f in core.action_frame_list(acts['Walk']):
        world, sb = core.src_world_mats_pure(src_skel, sampler, f, needed)
        dest_skel.update_world(dst.matrix_world)
        bases = rm.solve_frame(dest_skel, maps_ik, world, sb)
        basis_map = {dest_skel.index[n]: b for n, b in bases.items()}
        pose = rm.fk_pose(dest_skel, basis_map)
        goal = world['hand'].to_translation()
        shoulder = (dst.matrix_world @
                    pose[dest_skel.index['UpArm']]).to_translation()
        eff = (dst.matrix_world @
               pose[dest_skel.index['Hand']]).to_translation()
        reachable = (goal - shoulder).length <= (up_len + lo_len) * 0.999
        if reachable and (eff - goal).length > 1e-4:
            reach_fail += 1
        orient_err = max(orient_err, quat_angle(
            world['hand'].decompose()[1] @ maps_ik[-1].q_offset,
            (dst.matrix_world @ pose[dest_skel.index['Hand']]).decompose()[1]))
    check('IK 可达帧全部贴合 (失败帧=%d)' % reach_fail, reach_fail == 0)
    check('IK 后手部朝向保持 %.2e' % orient_err, orient_err < 1e-3)


# ---------------------------------------------------------------------------
# Phase 2.4: 自动对齐 — rest 约定不同 (T-pose 源 ↔ A-pose 目标) 时
#   肢体几何(肘弯角)必须逐帧守恒, 无需任何人工 align/offset。
#   KK(T) → EndField(A) 手臂差 34° 的真实回归见交接文档 §12。
# ---------------------------------------------------------------------------

T_ARM = [
    ('hips',     (0, 0, 1.00),    (0, 0.10, 1.05),  0.00, None,       False, True, 'FULL'),
    ('upperarm', (0.10, 0, 1.40), (0.40, 0, 1.40),  0.30, 'hips',     False, True, 'FULL'),
    ('forearm',  (0.40, 0, 1.40), (0.66, 0, 1.40), -0.40, 'upperarm', True,  True, 'FULL'),
    ('hand',     (0.66, 0, 1.40), (0.76, 0, 1.40),  0.10, 'forearm',  True,  True, 'FULL'),
]

_A = math.radians(45)
_AX, _AZ = math.cos(_A), math.sin(_A)
A_ARM = [
    ('Hips',  (0, 0, 1.05), (0, 0.10, 1.10), 0.00, None, False, True, 'FULL'),
    ('UpArm', (0.08, 0, 1.45),
     (0.08 + 0.30 * _AX, 0, 1.45 - 0.30 * _AZ), 0.80, 'Hips', False, True, 'FULL'),
    ('LoArm', (0.08 + 0.30 * _AX, 0, 1.45 - 0.30 * _AZ),
     (0.08 + 0.56 * _AX, 0, 1.45 - 0.56 * _AZ), -0.90, 'UpArm', True, True, 'FULL'),
    ('Hand',  (0.08 + 0.56 * _AX, 0, 1.45 - 0.56 * _AZ),
     (0.08 + 0.66 * _AX, 0, 1.45 - 0.66 * _AZ), 0.20, 'LoArm', True, True, 'FULL'),
]


def phase_auto_align():
    print('\n== Phase 2.4: 自动对齐 (T-pose 源 ↔ A-pose 目标, 肘弯守恒) ==')
    reset_blend()
    src = build_armature('TSrc', T_ARM)
    dst = build_armature('ADst', A_ARM, location=(1.0, 0.5, 0),
                         rotation=(0, 0, math.radians(30)))
    act = make_action(src, 'Swing')
    for f in range(1, 25):
        t = f / 24.0 * 2 * math.pi
        key_bone(src, 'upperarm', f,
                 quat=Quaternion(Vector((0.3, 0.8, 0.5)).normalized(),
                                 0.9 * math.sin(t)))
        key_bone(src, 'forearm', f,
                 quat=Quaternion((0, 0, 1), 0.7 * abs(math.sin(t))))
        key_bone(src, 'hand', f, quat=Quaternion((1, 0, 0), 0.3 * math.cos(t)))
    reset_pose(src)

    spec_maps = [{'source': s, 'dest': d,
                  'rot': {'auto': True, 'ortho': False, 'offset': [0, 0, 0]}}
                 for s, d in (('hips', 'Hips'), ('upperarm', 'UpArm'),
                              ('forearm', 'LoArm'), ('hand', 'Hand'))]
    skel_s = core.snapshot_skeleton(src)
    skel_d = core.snapshot_skeleton(dst)
    maps, warns = rm.build_mappings(skel_s, skel_d, spec_maps)
    check('T→A build_mappings 无警告', not warns, str(warns))

    rest_dirs_equal = 0
    for m in maps:
        if m.dest_name != 'UpArm':
            continue
        rest_dirs_equal = math.degrees(m.q_offset.angle)
    check('上臂 q_offset 非零 (A-pose 差被解出, %.1f°)' % rest_dirs_equal,
          20.0 < rest_dirs_equal)

    sampler = core.ActionSampler(src, skel_s, act)
    needed = [m.src_name for m in maps]

    def bend(heads):
        upper, lower = heads[1] - heads[0], heads[2] - heads[1]
        return math.degrees(upper.angle(lower))

    max_bend = 0.0
    for f in core.action_frame_list(act):
        world, sb = core.src_world_mats_pure(skel_s, sampler, f, needed)
        bases = rm.solve_frame(skel_d, maps, world, sb)
        basis_map = {skel_d.index[n]: b for n, b in bases.items()}
        pose = rm.fk_pose(skel_d, basis_map)
        src_heads = [world[n].to_translation()
                     for n in ('upperarm', 'forearm', 'hand')]
        dst_heads = [(skel_d.matrix_world @ pose[skel_d.index[n]]).to_translation()
                     for n in ('UpArm', 'LoArm', 'Hand')]
        max_bend = max(max_bend, abs(bend(src_heads) - bend(dst_heads)))
    check('T→A 肘弯角逐帧守恒 (max %.3e°)' % max_bend, max_bend < 0.05)

    # rest 约定一致时自动对齐必须严格退化为恒等 (参考朝向逐位 == rest)
    refs = rm.solve_reference_alignment(
        skel_s, skel_s, [(skel_s.index[n], skel_s.index[n])
                         for n, *_ in T_ARM])
    max_dev = max(math.degrees(
        (skel_s.rest_world_rot(i).inverted() @ q).angle)
        for i, q in refs.items())
    check('同构骨架自动对齐 = 恒等 (max %.2e°)' % max_dev, max_dev < 1e-6)


# ---------------------------------------------------------------------------
# Phase 2.5: 手动偏移的层级性
#   偏移是"参考姿态"的一部分, 转大腿必须带动小腿与扭曲骨整条子链 —
#   否则子骨各自把世界朝向钉死, 只有骨头被拖着平移 (腿在膝盖处断开,
#   扛大腿蒙皮权重的扭曲骨纹丝不动, 看着像大腿没权重)。
# ---------------------------------------------------------------------------

LEG_SRC = [
    # name         head              tail                roll  parent    con    inhR  inhS
    ('Pelvis',     (0, 0, 1.00),     (0, 0.10, 1.02),    0.00, None,     False, True, 'FULL'),
    ('Thigh',      (0.10, 0, 0.95),  (0.10, 0, 0.52),    0.20, 'Pelvis', False, True, 'FULL'),
    ('ThighTwist', (0.10, 0, 0.80),  (0.10, 0, 0.62),    0.20, 'Thigh',  False, True, 'FULL'),
    ('Calf',       (0.10, 0, 0.52),  (0.10, 0, 0.10),   -0.10, 'Thigh',  False, True, 'FULL'),
    ('AnkleS',     (0.10, 0, 0.10),  (0.10, 0.15, 0.05), 0.00, 'Calf',   False, True, 'FULL'),
]

# 目标侧照搬用户真实骨架的形状: 小腿 connected, 扭曲骨是大腿的形变子骨
LEG_DST = [
    ('Root',           (0, 0, 0.00),     (0, 0.10, 0.00),    0.00, None,       False, True, 'FULL'),
    ('UpperLeg',       (0.09, 0, 0.90),  (0.09, 0, 0.48),    0.35, 'Root',     False, True, 'FULL'),
    ('UpperLegTwists', (0.09, 0, 0.75),  (0.09, 0, 0.58),    0.35, 'UpperLeg', False, True, 'FULL'),
    ('LowerLeg',       (0.09, 0, 0.48),  (0.09, 0, 0.06),   -0.20, 'UpperLeg', True,  True, 'FULL'),
    ('Ankle',          (0.09, 0, 0.06),  (0.09, 0.14, 0.02), 0.00, 'LowerLeg', True,  True, 'FULL'),
]

LEG_PAIRS = [('Thigh', 'UpperLeg'), ('ThighTwist', 'UpperLegTwists'),
             ('Calf', 'LowerLeg'), ('AnkleS', 'Ankle')]


def leg_spec(offset=(0.0, 0.0, 0.0), self_only=True):
    """偏移只挂在 UpperLeg 上 — 子链跟不跟着转就是被测行为。"""
    return [{'source': s, 'dest': d,
             'rot': {'auto': True, 'ortho': False, 'self_only': self_only,
                     'offset': list(offset) if d == 'UpperLeg' else [0, 0, 0]}}
            for s, d in LEG_PAIRS]


def leg_solve(src, dst, spec_maps):
    """源骨架保持 rest → 解出的目标姿态就是参考姿态本身。"""
    src_skel = core.snapshot_skeleton(src)
    dest_skel = core.snapshot_skeleton(dst)
    mappings, warns = rm.build_mappings(src_skel, dest_skel, spec_maps)
    src_pose = rm.fk_pose(src_skel, {})
    world = {b.name: src_skel.matrix_world @ src_pose[i]
             for i, b in enumerate(src_skel.bones)}
    bases = rm.solve_frame(dest_skel, mappings, world, {})
    basis_map = {dest_skel.index[n]: b for n, b in bases.items()}
    pose = rm.fk_pose(dest_skel, basis_map)
    out = {}
    for i, b in enumerate(dest_skel.bones):
        loc, rot, _sca = (dest_skel.matrix_world @ pose[i]).decompose()
        out[b.name] = (rot, loc)
    return out, mappings, warns


def phase_manual_offset():
    print('\n== Phase 2.5: 手动偏移的层级性 ==')
    reset_blend()
    src = build_armature('LegSrc', LEG_SRC, location=(0.2, 0, 0))
    dst = build_armature('LegDst', LEG_DST, rotation=(0, 0, math.radians(20)))

    base, _maps, warns = leg_solve(src, dst, leg_spec())
    check('腿部映射无警告', not warns, str(warns))

    ang = math.radians(25)
    CHAIN = ('UpperLeg', 'UpperLegTwists', 'LowerLeg', 'Ankle')
    for label, off in (('摆 X', (ang, 0, 0)), ('扭 Y', (0, ang, 0)),
                       ('侧 Z', (0, 0, ang))):
        # self_only=False: 老语义, 整条子链跟着转
        cur, _m, _w = leg_solve(src, dst, leg_spec(off, self_only=False))
        err = max(abs(quat_angle(base[n][0], cur[n][0]) - ang) for n in CHAIN)
        check('偏移%s 关掉"仅本骨": 整条子链同步转 25° (最大偏差 %.2e rad)'
              % (label, err), err < 1e-4)

        # 缺省 self_only=True: 只有被偏移的那根骨动, 子骨一动不动
        cur, _m, _w = leg_solve(src, dst, leg_spec(off))
        own = abs(quat_angle(base['UpperLeg'][0], cur['UpperLeg'][0]) - ang)
        kids = max(quat_angle(base[n][0], cur[n][0]) for n in CHAIN[1:])
        check('偏移%s 仅本骨: 自己转 25° (偏差 %.2e rad)' % (label, own), own < 1e-4)
        check('偏移%s 仅本骨: 子骨纹丝不动 (最大 %.2e rad)' % (label, kids),
              kids < 1e-6)

    src_skel = core.snapshot_skeleton(src)
    dest_skel = core.snapshot_skeleton(dst)

    # 空态零成本: 没有偏移时参考姿态求值不介入, 旋转解逐位就是纯 rest 差
    plain, _ = rm.build_mappings(src_skel, dest_skel, leg_spec())
    check('零偏移时旋转解逐位等于纯 rest 差 (无参考姿态噪声)',
          all(m.q_offset == (src_skel.rest_world_rot(m.src_i).inverted()
                             @ dest_skel.rest_world_rot(m.dest_i)).normalized()
              for m in plain))


# ---------------------------------------------------------------------------
# Phase 2.6: 局部通道透传 (关节式面部绑定)
#   眨眼/张嘴是骨骼平移+缩放, 不是旋转; 只传旋转 = 表情一点都过不去。
#   两套脸可以关节位置完全一致而骨骼轴向完全不同, 所以位移必须过轴向换算。
# ---------------------------------------------------------------------------

FACE_SRC = [
    # name      head                tail                 roll  parent   con    inhR  inhS
    ('Head',    (0, 0, 1.60),      (0, 0, 1.75),         0.00, None,    False, True, 'FULL'),
    ('EyeLid',  (0.03, -0.06, 1.70), (0.03, -0.08, 1.70), 0.00, 'Head', False, True, 'FULL'),
    ('Jaw',     (0, -0.03, 1.64),  (0, -0.07, 1.62),     0.00, 'Head',  False, True, 'FULL'),
]

# 目标: 关节位置逐位相同, 但 roll 全不一样 (真实 rig 重建后就是这样)
FACE_DST = [
    ('HeadD',   (0, 0, 1.60),      (0, 0, 1.75),         1.10, None,    False, True, 'FULL'),
    ('LidD',    (0.03, -0.06, 1.70), (0.03, -0.08, 1.70), 2.40, 'HeadD', False, True, 'FULL'),
    ('JawD',    (0, -0.03, 1.64),  (0, -0.07, 1.62),    -1.80, 'HeadD', False, True, 'FULL'),
]

FACE_PAIRS = [('Head', 'HeadD'), ('EyeLid', 'LidD'), ('Jaw', 'JawD')]


def face_spec(local):
    return [{'source': s, 'dest': d,
             'rot': {'auto': True, 'ortho': False, 'offset': [0, 0, 0]},
             'local': {'enabled': local}}
            for s, d in FACE_PAIRS]


def face_solve(src, dst, spec_maps):
    src_skel = core.snapshot_skeleton(src)
    dest_skel = core.snapshot_skeleton(dst)
    mappings, _w = rm.build_mappings(src_skel, dest_skel, spec_maps)
    dg = bpy.context.evaluated_depsgraph_get()
    world, src_bases = core.src_world_mats_scene(
        src, dg, [m.src_name for m in mappings])
    bases = rm.solve_frame(dest_skel, mappings, world, src_bases)
    pose = rm.fk_pose(dest_skel, {dest_skel.index[n]: b
                                  for n, b in bases.items()})
    out = {}
    for i, b in enumerate(dest_skel.bones):
        loc, rot, sca = (dest_skel.matrix_world @ pose[i]).decompose()
        out[b.name] = (loc, rot, sca)
    return out, world, bases


def phase_local_channel():
    print('\n== Phase 2.6: 局部通道透传 (面部表情) ==')
    reset_blend()
    src = build_armature('FaceSrc', FACE_SRC)
    dst = build_armature('FaceDst', FACE_DST, location=(1.0, 0, 0))

    # 两侧 rest 朝向确实不同 — 否则这个测试是空转
    s_q = (src.matrix_world @ src.data.bones['EyeLid'].matrix_local).to_quaternion()
    d_q = (dst.matrix_world @ dst.data.bones['LidD'].matrix_local).to_quaternion()
    axis_gap = math.degrees(s_q.rotation_difference(d_q).angle)
    check('测试前提: 两侧骨骼轴向确实不同 (%.1f°)' % axis_gap, axis_gap > 30.0)

    for pb in src.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    base_off, w_off, _b = face_solve(src, dst, face_spec(False))
    base_on, _w, _b2 = face_solve(src, dst, face_spec(True))

    # 摆表情: 眼睑往局部下方挪 + 下巴挪 + 眼睑压扁
    src.pose.bones['EyeLid'].matrix_basis = Matrix.Translation((0, 0, -0.01))
    src.pose.bones['Jaw'].matrix_basis = Matrix.Translation((0, -0.012, 0))
    src.pose.bones['Head'].matrix_basis = Matrix.LocRotScale(
        None, Quaternion((0, 0, 1), 0.4), None)   # 顺带转头, 验证层级不受影响
    bpy.context.view_layer.update()

    off, w_pose, _b3 = face_solve(src, dst, face_spec(False))
    on, _w2, bases_on = face_solve(src, dst, face_spec(True))

    moved_src = 0.0
    worst_len = worst_dir = 0.0
    for s, d in FACE_PAIRS:
        vs = w_pose[s].to_translation() - w_off[s].to_translation()
        vd = on[d][0] - base_on[d][0]
        moved_src += vs.length
        if vs.length < 1e-5:
            continue
        worst_len = max(worst_len, abs(vs.length - vd.length))
        worst_dir = max(worst_dir, math.degrees(math.acos(max(-1.0, min(
            1.0, vs.normalized().dot(vd.normalized()))))))
    dropped = sum((off[d][0] - base_off[d][0]).length for _s, d in FACE_PAIRS)
    moved_dst = sum((on[d][0] - base_on[d][0]).length for _s, d in FACE_PAIRS)

    # 开/关的差 = 局部通道真正贡献的那部分 (头部旋转两边都有, 会抵消掉)
    contributed = sum((on[d][0] - off[d][0]).length for _s, d in FACE_PAIRS)
    check('局部通道确实改变结果 (开关差 %.4f, 源侧局部位移 %.4f)'
          % (contributed, moved_src), contributed > 1e-3)
    check('打开局部通道: 位移量对上 (最大差 %.2e)' % worst_len, worst_len < 1e-5)
    check('打开局部通道: 世界方向对上 (最大夹角 %.4f°)' % worst_dir,
          worst_dir < 0.05)
    check('目标跟着动的量与源一致 (%.4f vs %.4f)' % (moved_dst, moved_src),
          abs(moved_dst - moved_src) < 1e-4)

    # 缩放通道: 换帧后按主轴对应, 幅度必须守恒
    src.pose.bones['EyeLid'].matrix_basis = Matrix.LocRotScale(
        None, None, Vector((1.0, 1.0, 0.4)))
    bpy.context.view_layer.update()
    sca_on, _w3, bases_sca = face_solve(src, dst, face_spec(True))
    b = bases_sca.get('LidD')
    got = sorted(round(v, 4) for v in b.to_scale()) if b else []
    check('缩放通道透传 (源 (1,1,0.4) → 目标 %s)' % got,
          bool(b) and abs(min(got) - 0.4) < 1e-4 and abs(max(got) - 1.0) < 1e-4)

    # 空态零成本: 源没有局部偏移时, 开与不开这个开关的结果必须逐位一致
    for pb in src.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    _r1, _w4, b_off = face_solve(src, dst, face_spec(False))
    _r2, _w5, b_on = face_solve(src, dst, face_spec(True))
    check('源无局部偏移时: 开关局部通道结果逐位一致 (空态零成本)',
          set(b_off) == set(b_on) and all(b_off[k] == b_on[k] for k in b_off))


# ---------------------------------------------------------------------------
# Phase 3: 烘焙 — 确定性 / 顺序无关 / 根位移 / 零残留
# ---------------------------------------------------------------------------

def _curve_fingerprint(act):
    out = {}
    for fc in core.iter_action_fcurves(act):
        n = len(fc.keyframe_points)
        co = [0.0] * (n * 2)
        fc.keyframe_points.foreach_get('co', co)
        out[(fc.data_path, fc.array_index)] = tuple(co)
    return out


def phase_bake():
    print('\n== Phase 3: 烘焙确定性 / 顺序无关 / 根位移 / 零残留 ==')
    reset_blend()
    src = build_armature('SrcRig', SRC_BONES, location=(0.3, -0.2, 0.1),
                         rotation=(0, 0, math.radians(15)), scale=0.01)
    acts = build_src_actions(src)
    dst = build_armature('DstRig', DST_BONES)
    dst.data.bones['Root'].hide = True
    spec = base_spec(loc_scale_mode='AUTO', ik_hand=True)
    settings = {'frame_step': 1.0, 'interpolation': 'LINEAR',
                'suffix': '_baked', 'overwrite': True, 'bake_mode': 'PURE',
                'fake_user': True}

    bakeable = core.list_bakeable_actions(src)
    names = sorted(a.name for a in bakeable)
    check('可烘动作筛选 (排除 Junk): %s' % names,
          names == ['Idle', 'Run', 'Walk'])

    src_action_before = src.animation_data.action
    pose_before = {pb.name: pb.matrix_basis.copy() for pb in dst.pose.bones}

    results, errors = core.bake_all(src, dst, spec, settings)
    check('批量烘焙 3 条全部成功', len(results) == 3 and not errors,
          str(errors))

    fp_run_batch = _curve_fingerprint(bpy.data.actions['Run_baked'])

    # 确定性: 再烘一遍 (覆盖模式) → 指纹逐位相等
    results2, _ = core.bake_all(src, dst, spec, settings)
    fp_run_batch2 = _curve_fingerprint(bpy.data.actions['Run_baked'])
    check('重复批量烘焙逐位一致 (无漂移)', fp_run_batch == fp_run_batch2)

    # 顺序无关性: 单烘 Run (不同后缀) → 与批量结果一致
    solo, _info = core.bake_action(src, dst, spec, acts['Run'],
                                   dict(settings, suffix='_solo'))
    fp_run_solo = {k: v for k, v in _curve_fingerprint(solo).items()}
    check('单烘与批量结果逐位一致 (顺序无关)', fp_run_batch == fp_run_solo)

    # 根位移: Run 含物体级根运动, Hips 的 X 位移必须显著非常数
    hips_x = next((v for (p, i), v in fp_run_batch.items()
                   if p == 'pose.bones["Hips"].location' and i == 0), None)
    vals = hips_x[1::2] if hips_x else []
    check('根骨骼位移曲线存在且非常数 (span=%.4f)'
          % ((max(vals) - min(vals)) if vals else -1),
          bool(vals) and (max(vals) - min(vals)) > 0.05)

    # 隐藏骨 Root... Hips 父级隐藏不影响; 再验证隐藏的映射骨也能烘:
    hand_curves = [p for (p, i) in fp_run_batch
                   if p.startswith('pose.bones["Hand"]')]
    check('隐藏骨/未选中骨照常烘焙 (Hand 曲线 %d 条)' % len(hand_curves),
          len(hand_curves) >= 4)

    # 状态零残留
    n_con = sum(len(pb.constraints) for o in (src, dst)
                for pb in o.pose.bones)
    check('两骨架上约束数为 0', n_con == 0)
    check('源动作指派未被改动', src.animation_data.action is src_action_before)
    pose_after = {pb.name: pb.matrix_basis.copy() for pb in dst.pose.bones}
    same_pose = all((pose_before[k] - pose_after[k]).median_scale < 1e-12 or
                    pose_before[k] == pose_after[k] for k in pose_before)
    check('目标姿态未被烘焙触碰', same_pose)
    tagged = all(bpy.data.actions[n + '_baked'].get(core.GENERATED_TAG)
                 for n in ('Walk', 'Run', 'Idle'))
    check('产物已打 animret_generated 标签', tagged)

    # SCENE 模式应与 PURE 一致 (源无自定义约束时)
    scene_act, _ = core.bake_action(src, dst, spec, acts['Walk'],
                                    dict(settings, suffix='_scene',
                                         bake_mode='SCENE'))
    fp_walk_pure = _curve_fingerprint(bpy.data.actions['Walk_baked'])
    fp_walk_scene = _curve_fingerprint(scene_act)
    max_d = 0.0
    for k, v in fp_walk_pure.items():
        v2 = fp_walk_scene.get(k)
        if v2 is None or len(v) != len(v2):
            max_d = 1e9
            break
        max_d = max(max_d, max(abs(a - b) for a, b in zip(v, v2)))
    check('SCENE 与 PURE 模式误差 %.2e' % max_d, max_d < 2e-3)

    # 欧拉模式目标骨: 把 LoArm 切到 ZXY 再烘, 应输出欧拉曲线且连续
    dst.pose.bones['LoArm'].rotation_mode = 'ZXY'
    eul_act, _ = core.bake_action(src, dst, spec, acts['Walk'],
                                  dict(settings, suffix='_eul'))
    eul = _curve_fingerprint(eul_act)
    keys = [k for k in eul if k[0] == 'pose.bones["LoArm"].rotation_euler']
    ok_cont = bool(keys)
    for k in keys:
        vals = eul[k][1::2]
        for a, b in zip(vals, vals[1:]):
            if abs(a - b) > 1.0:
                ok_cont = False
    check('欧拉目标骨曲线存在且帧间连续', ok_cont)
    dst.pose.bones['LoArm'].rotation_mode = 'QUATERNION'


# ---------------------------------------------------------------------------
# Phase 3.5: 空 FCurve 毒针 (游戏提取动作: 有曲线槽位但 0 关键帧)
#   Blender 求值会跳过空曲线, 但 fc.evaluate() 返回 0.0 —
#   空 scale 曲线若被当真会把 basis 烧成零矩阵, 旋转全丢。
# ---------------------------------------------------------------------------

def phase_empty_fcurves():
    print('\n== Phase 3.5: 空 FCurve (游戏提取布局) ==')
    reset_blend()
    src = build_armature('SrcRig', SRC_BONES, location=(0.3, -0.2, 0.1),
                         rotation=(0, 0, math.radians(15)), scale=0.01)
    dst = build_armature('DstRig', DST_BONES)
    act = bpy.data.actions.new('Ripped')
    act.use_fake_user = True
    writer = core._ActionWriter(act, src.name)

    def add_curves(bone, prop, size, keys=None):
        for i in range(size):
            fc = writer.fcurves.new(
                data_path='pose.bones["%s"].%s' % (bone, prop), index=i)
            if keys:
                for f, vec in keys:
                    fc.keyframe_points.insert(f, vec[i])

    # hips: 真旋转动画 + 空 loc + 空 scale (毒针本体)
    hip_keys = [(f, Quaternion((1, 0, 0), 0.5 * math.sin(f / 6.0)))
                for f in range(1, 25)]
    add_curves('hips', 'rotation_quaternion', 4, hip_keys)
    add_curves('hips', 'location', 3)            # 空
    add_curves('hips', 'scale', 3)               # 空 — evaluate()=0 的陷阱
    # spine: 全部 10 条曲线皆空 (类似未动画的 Character_Root)
    add_curves('spine', 'rotation_quaternion', 4)
    add_curves('spine', 'location', 3)
    add_curves('spine', 'scale', 3)
    # forearm: 真旋转 + 空 scale
    fa_keys = [(f, Quaternion((0, 0, 1), 0.4 * math.sin(f / 4.0)))
               for f in range(1, 25)]
    add_curves('forearm', 'rotation_quaternion', 4, fa_keys)
    add_curves('forearm', 'scale', 3)

    core.assign_action(src, act)
    reset_pose(src)

    # 采样器输出 vs depsgraph 真值
    skel = core.snapshot_skeleton(src)
    sampler = core.ActionSampler(src, skel, act)
    check('空 scale 曲线被视同缺失',
          'scale' not in sampler.bone_channels.get('hips', {}))
    check('全空骨骼不进采样通道', 'spine' not in sampler.bone_channels)
    scene = bpy.context.scene
    max_rot = max_loc = 0.0
    for f in core.action_frame_list(act):
        scene.frame_set(int(f))
        dg = bpy.context.evaluated_depsgraph_get()
        ev = src.evaluated_get(dg)
        obj_w = sampler.world_matrix(f)
        skel.update_world(obj_w)
        pose = rm.fk_pose(skel, sampler.basis_map(f))
        for i, b in enumerate(skel.bones):
            truth = ev.matrix_world @ ev.pose.bones[b.name].matrix
            mine = obj_w @ pose[i]
            max_rot = max(max_rot, mat_rot_err(truth, mine))
            max_loc = max(max_loc, mat_loc_err(truth, mine))
    check('空曲线动作 FK 与 depsgraph 一致 %.2e' % max_rot, max_rot < 1e-3)
    check('空曲线动作 FK 位置一致 %.2e' % max_loc, max_loc < 1e-5)

    # 烘焙产物旋转必须真的在动
    baked, _ = core.bake_action(src, dst, base_spec(), act,
                                {'suffix': '_baked', 'overwrite': True,
                                 'bake_mode': 'PURE', 'fake_user': True})
    span = 0.0
    for fc in core.iter_action_fcurves(baked):
        if 'pose.bones["Hips"].rotation' in fc.data_path:
            vals = [kp.co[1] for kp in fc.keyframe_points]
            span = max(span, max(vals) - min(vals))
    check('烘焙产物 Hips 旋转非常数 (span=%.3f)' % span, span > 0.1)


# ---------------------------------------------------------------------------
# Phase 4: JSON 预设系统 (覆盖保存 / 读回 / 删除 / 往返)
# ---------------------------------------------------------------------------

def phase_presets():
    print('\n== Phase 4: JSON 预设覆盖保存 / 往返 ==')
    spec_a = base_spec(loc_scale_mode='AUTO')
    spec_b = base_spec(ik_hand=True)
    spec_b['mappings'][0]['rot']['offset'] = [0.0, 1.5707963705062866, 0.0]

    name = '_selftest_preset'
    p1 = presets_mod.save_preset(name, spec_a)
    check('预设保存落盘', os.path.isfile(p1))
    p2 = presets_mod.save_preset(name, spec_b)   # 直接覆盖, 不产生 .001
    check('同名覆盖保存为同一文件', p1 == p2
          and presets_mod.list_presets().count(name) == 1)

    loaded = presets_mod.load_preset(name)
    check('覆盖后读回的是新内容 (B 的 IK)',
          loaded['mappings'][-1].get('ik', {}).get('enabled') is True)
    check('预设往返逐字段一致', loaded == spec_b)

    presets_mod.delete_preset(name)
    check('预设删除', not os.path.isfile(p1))


# ---------------------------------------------------------------------------
# Phase 5: 插件注册 + 操作符集成 + 预览零残留
# ---------------------------------------------------------------------------

def phase_operators():
    print('\n== Phase 5: 操作符集成 / 预览零残留 ==')
    reset_blend()
    try:
        addon.register()
    except ValueError:
        pass    # 已注册
    src = build_armature('SrcRig', SRC_BONES, scale=0.01)
    acts = build_src_actions(src)
    dst = build_armature('DstRig', DST_BONES)
    scene = bpy.context.scene
    scene.animret_dest = dst
    s = dst.data.animret
    s.source = src

    for sn, dn in (('hips', 'Hips'), ('spine', 'Spine'),
                   ('upperarm', 'UpArm'), ('forearm', 'LoArm'),
                   ('hand', 'Hand')):
        m = s.mappings.add()
        m.dest, m.source = dn, sn
    s.mappings[0].loc_enabled = True
    s.mappings[0].loc_scale_mode = 'AUTO'

    core.assign_action(src, acts['Walk'])
    r = bpy.ops.animret.bake()
    check('操作符单烘 FINISHED', r == {'FINISHED'})
    check('单烘产物存在', 'Walk_baked' in bpy.data.actions)
    r = bpy.ops.animret.bake_all()
    check('操作符批量烘 FINISHED', r == {'FINISHED'})
    check('批量产物齐全', all(n + '_baked' in bpy.data.actions
                              for n in ('Walk', 'Run', 'Idle')))

    # 预览: 开 → 写姿态; 关 → 完全复位; .blend 中无任何新增物体/约束
    n_obj_before = len(bpy.data.objects)
    s.preview = True
    core._apply_preview_all()
    moved = any((pb.matrix_basis - Matrix.Identity(4)).median_scale > 1e-9
                or pb.matrix_basis != Matrix.Identity(4)
                for pb in dst.pose.bones)
    check('预览开启后目标骨架被驱动', moved)
    s.preview = False
    clean = all(pb.matrix_basis == Matrix.Identity(4)
                for pb in dst.pose.bones)
    check('预览关闭后姿态完全复位', clean)
    check('预览未创建任何对象/约束',
          len(bpy.data.objects) == n_obj_before and
          sum(len(pb.constraints) for pb in dst.pose.bones) == 0)

    # 根骨自动位移 + 分页恢复默认
    from AnimationRetarget.state import root_bone_names
    roots = root_bone_names(dst)
    check('根骨识别 (DstRig 的 Root 无父级)', roots == {'Root'})

    # 加载改名表(没有 loc 项)时, 根骨应当自动开位移
    s.from_spec({'renames': [{'from': 'hips', 'to': 'Root'},
                             {'from': 'spine', 'to': 'Spine'},
                             {'from': 'hand', 'to': 'Hand'}]})
    auto = [(m.dest, m.loc_scale_mode) for m in s.mappings if m.loc_enabled]
    check('加载改名表时根骨自动开位移 %s' % auto, auto == [('Root', 'AUTO')])
    s.mappings[0].dest = 'Root'          # 把首条映射指到根骨
    s.mappings[0]['loc_enabled'] = False
    r = bpy.ops.animret.setup_root_loc()
    check('操作符: 根骨自动位移 FINISHED', r == {'FINISHED'})
    check('根骨位移已开启且为 AUTO',
          s.mappings[0].loc_enabled and s.mappings[0].loc_scale_mode == 'AUTO')

    # 改一堆参数, 再按页恢复
    for m in s.mappings:
        m.rot_offset = (0.5, 0.5, 0.5)
        m.rot_auto = False
        m.ik_enabled = True
        m.ik_chain = 5
    s.editing_type = 1                   # 旋转页
    bpy.ops.animret.reset_tab()
    check('恢复旋转页: rot_offset 归零且 rot_auto 回默认',
          all(max(abs(v) for v in m.rot_offset) < 1e-9 and m.rot_auto
              for m in s.mappings))
    check('恢复旋转页: 不碰 IK 页的参数',
          all(m.ik_enabled and m.ik_chain == 5 for m in s.mappings))
    s.editing_type = 3                   # IK 页
    bpy.ops.animret.reset_tab()
    check('恢复 IK 页: ik 回默认',
          all(not m.ik_enabled and m.ik_chain == 2 for m in s.mappings))
    s.editing_type = 2                   # 位移页
    bpy.ops.animret.reset_tab()
    check('恢复位移页后根骨位移被自动补回',
          s.mappings[0].loc_enabled and s.mappings[0].loc_scale_mode == 'AUTO')
    check('恢复位移页: 非根骨的位移已关闭',
          all(not m.loc_enabled for m in s.mappings if m.dest not in roots))

    # 上面几项换过映射表, 恢复成本阶段约定的 5 条, 供后续测试继续用
    s.mappings.clear()
    for sn, dn in (('hips', 'Hips'), ('spine', 'Spine'),
                   ('upperarm', 'UpArm'), ('forearm', 'LoArm'),
                   ('hand', 'Hand')):
        m = s.mappings.add()
        m.dest, m.source = dn, sn
    s.mappings[0].loc_enabled = True
    s.mappings[0].loc_scale_mode = 'AUTO'
    s.editing_type = 0

    # 映射表的骨骼搜索过滤
    from AnimationRetarget.state import mapping_matches
    def mhit(q):
        return sum(1 for m in s.mappings if mapping_matches(m, q))
    check('映射搜索: 空 = 全部显示', mhit('') == len(s.mappings))
    check('映射搜索: 命中自身骨 (uparm -> 1)', mhit('uparm') == 1)
    check('映射搜索: 命中来源骨 (hips -> 1)', mhit('hips') == 1)
    check('映射搜索: 大小写不敏感', mhit('HIPS') == 1)
    check('映射搜索: 多词需全部命中', mhit('arm forearm') == 1)
    check('映射搜索: 无命中', mhit('zzzz') == 0)
    s.mapping_filter = 'hips'
    r = bpy.ops.animret.clear_mapping_filter()
    check('操作符: 清空映射搜索', r == {'FINISHED'} and s.mapping_filter == '')

    # 绑定不得阻挡"清理未使用数据": 删掉骨架后必须放开 ID 指针
    keep = build_armature('KeepRig', SRC_BONES)
    s.source = keep
    check('绑定会给对象记一个 user', keep.users == 2)
    for o in bpy.data.objects:
        o.select_set(False)
    keep.select_set(True)
    bpy.context.view_layer.objects.active = keep
    bpy.ops.object.delete()                 # 视图里 X 删除 = 从集合解链
    bpy.context.view_layer.update()
    left = bpy.data.objects.get('KeepRig')
    check('删除后绑定被自动放开', s.source is None)
    check('删除后对象 users 归零 (purge 才清得掉)',
          left is None or left.users == 0)
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True,
                                   do_recursive=True)
    check('清理未使用数据能清掉它',
          'KeepRig' not in bpy.data.objects
          and 'KeepRig' not in bpy.data.armatures)

    # 反例: 只是不在场景里(放在游离集合)不算删除, 绑定不能被误清
    det = build_armature('DetachedRig', SRC_BONES)
    s.source = det
    col = bpy.data.collections.new('Detached')
    col.objects.link(det)
    bpy.context.scene.collection.objects.unlink(det)
    bpy.context.view_layer.update()
    core.release_deleted_refs()
    check('游离集合里的对象不会被误清', s.source is det)
    s.source = src

    # 嵌入配置可被 CLI 的 IDProp 读取路径解析
    cli = __import__(PKG + '.cli', fromlist=['x'])
    embedded = cli.spec_from_idprops(dst.data)
    if embedded is None:
        print('    debug idprop keys:', list(dst.data.keys()))
        raw = dst.data.get('animret')
        if raw is not None:
            try:
                print('    debug animret:', raw.to_dict())
            except Exception as e:
                print('    debug to_dict failed:', e)
    check('CLI 可读取嵌入配置 (%d 条)' %
          (len(embedded['mappings']) if embedded else 0),
          embedded is not None and len(embedded['mappings']) == 5)

    addon.unregister()


# ---------------------------------------------------------------------------
# Phase 6: CLI 全链路 (blend → bake → FBX)
# ---------------------------------------------------------------------------

def phase_cli():
    print('\n== Phase 6: CLI 全链路 ==')
    reset_blend()
    src = build_armature('SrcRig', SRC_BONES, scale=0.01)
    build_src_actions(src)
    build_armature('DstRig', DST_BONES)
    tmp = tempfile.mkdtemp(prefix='animret_')
    blend_path = os.path.join(tmp, 'scene.blend')
    preset_path = os.path.join(tmp, 'map.json')
    import json
    with open(preset_path, 'w', encoding='utf-8') as f:
        json.dump(base_spec(loc_scale_mode='AUTO'), f)
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    cli = __import__(PKG + '.cli', fromlist=['x'])
    out_dir = os.path.join(tmp, 'fbx')
    argv = ['bake', '--blend', blend_path, '--preset', preset_path,
            '--source', 'SrcRig', '--dest', 'DstRig',
            '--actions', 'Walk,Run', '--export-fbx', out_dir, '--per-action',
            '--save-blend', os.path.join(tmp, 'baked.blend')]
    args = cli.parse_args(argv)
    try:
        cli.cmd_bake(args)
        cli_ok = True
    except SystemExit as e:
        cli_ok = (e.code in (0, None))
    except Exception:
        traceback.print_exc()
        cli_ok = False
    check('CLI bake 流程完成', cli_ok)
    fbx_files = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    check('FBX 逐动作导出: %s' % fbx_files,
          fbx_files == ['Run.fbx', 'Walk.fbx'])
    if fbx_files:
        sizes = [os.path.getsize(os.path.join(out_dir, f)) for f in fbx_files]
        check('FBX 文件非空 (%s bytes)' % sizes, all(sz > 10000 for sz in sizes))
    check('烘焙后 blend 已保存',
          os.path.isfile(os.path.join(tmp, 'baked.blend')))


# ---------------------------------------------------------------------------
# Phase 7: 骨架转换(重命名) — 与映射(mapping)完全独立的功能
# ---------------------------------------------------------------------------

SKEL_BONES = [
    # name    head              tail              roll  parent   con    inhR  inhS
    ('Root',  (0, 0, 0.00),    (0, 0.10, 0.00),   0.0,  None,    False, True, 'FULL'),
    ('Spine', (0, 0, 0.10),    (0, 0, 0.50),      0.0,  'Root',  True,  True, 'FULL'),
    ('ArmL',  (0.10, 0, 0.50), (0.40, 0, 0.50),   0.0,  'Spine', False, True, 'FULL'),
    ('ArmR',  (-0.10, 0, 0.50), (-0.40, 0, 0.50), 0.0,  'Spine', False, True, 'FULL'),
]


def phase_skeleton_rename():
    print('\n== Phase 7: 骨架转换(重命名) — 与映射完全独立 ==')
    import json
    skel = __import__(PKG + '.skeleton_rename', fromlist=['x'])

    # --- 导出结构: 层级 / depth / 世界坐标 / 蒙皮命中 ---
    reset_blend()
    arm = build_armature('SkelSrc', SKEL_BONES, location=(1, 2, 3))
    mesh = bpy.data.meshes.new('SkelMesh')
    mesh_obj = bpy.data.objects.new('SkelMeshObj', mesh)
    bpy.context.scene.collection.objects.link(mesh_obj)
    mesh.from_pydata([(0, 0, 0)], [], [])
    mesh.update()
    for bname in ('Root', 'Spine', 'ArmL'):
        mesh_obj.vertex_groups.new(name=bname)
    mod = mesh_obj.modifiers.new('Armature', 'ARMATURE')
    mod.object = arm

    snap = skel.export_skeleton(arm)
    check('导出结构: 骨骼数量', snap['bone_count'] == 4)
    by_name = {b['name']: b for b in snap['bones']}
    check('导出结构: 层级 parent/children 正确',
          by_name['Root']['parent'] is None
          and by_name['Root']['children'] == ['Spine']
          and set(by_name['Spine']['children']) == {'ArmL', 'ArmR'}
          and by_name['ArmL']['parent'] == 'Spine')
    check('导出结构: depth 正确',
          by_name['Root']['depth'] == 0 and by_name['Spine']['depth'] == 1
          and by_name['ArmL']['depth'] == 2)
    root_head_w = by_name['Root']['head_world']
    check('导出结构: 世界坐标叠加了物体变换 %s' % root_head_w,
          all(abs(root_head_w[i] - (1, 2, 3)[i]) < 1e-5 for i in range(3)))
    check('导出结构: 顶点组命中 (Root 有, ArmR 无)',
          by_name['Root']['deform_meshes'] == ['SkelMeshObj']
          and by_name['ArmR']['deform_meshes'] == [])

    path = skel.write_skeleton_export(arm)
    check('导出结构落盘', os.path.isfile(path))
    with open(path, 'r', encoding='utf-8') as f:
        reloaded = json.load(f)
    check('落盘内容与内存一致', reloaded == snap)

    # --- 重命名: 互换名(ArmL<->ArmR) + 普通改名 + 一个不存在的原名 ---
    act = make_action(arm, 'SkelAct')
    key_bone(arm, 'ArmL', 1, quat=Quaternion((0, 0, 1), 0.3))
    key_bone(arm, 'ArmL', 10, quat=Quaternion((0, 0, 1), -0.3))
    reset_pose(arm)
    con = arm.pose.bones['ArmR'].constraints.new('COPY_ROTATION')
    con.target, con.subtarget = arm, 'ArmL'

    pairs = [('ArmL', 'ArmR'), ('ArmR', 'ArmL'),
             ('Root', 'Pelvis'), ('Spine', 'Chest'),
             ('NoSuchBone', 'Whatever')]
    report = skel.apply_rename(arm, pairs)
    check('重命名: 4 根骨骼改名成功', len(report['renamed']) == 4)
    check('重命名: 缺失原名被报告', report['missing'] == ['NoSuchBone'])
    check('重命名: 无意外冲突', not report['collided'])
    names_now = {b.name for b in arm.data.bones}
    check('重命名: 互换名生效 %s' % sorted(names_now),
          names_now == {'Pelvis', 'Chest', 'ArmL', 'ArmR'})
    check('重命名: 层级结构未被打乱 (改名不影响父子指针)',
          arm.data.bones['Chest'].parent.name == 'Pelvis'
          and arm.data.bones['ArmR'].parent.name == 'Chest')

    # 顶点组由本模块接管 (寄存→改骨骼→落名), FCurve 路径 / 约束 subtarget 仍由
    # 引擎联动。这里验证互换名 (ArmL→ArmR, ArmR→ArmL) 下组名跟着骨骼走到位:
    # 若把组直接改到位再改骨骼, 引擎会在改 ArmR 那一步把刚落位的组又拖回 ArmL。
    vg_names = {v.name for v in mesh_obj.vertex_groups}
    check('顶点组随骨骼改名(互换名场景) %s' % sorted(vg_names),
          vg_names == {'Pelvis', 'Chest', 'ArmR'})

    fcurve_paths = {fc.data_path for fc in core.iter_action_fcurves(act)}
    check('引擎联动: FCurve 路径随骨骼改名 %s' % sorted(fcurve_paths),
          any('"ArmR"' in p for p in fcurve_paths)
          and not any('"ArmL"' in p for p in fcurve_paths))

    moved_con_bone = arm.pose.bones['ArmL']   # 原 ArmR 骨(挂了约束), 现名 ArmL
    check('引擎联动: 自引用约束 subtarget 随骨骼改名',
          moved_con_bone.constraints[0].subtarget == 'ArmR')

    # --- 顶点组撞名: 网格上已有一个叫目标名的外来组 (从别的骨架合并进来的
    # 那套命名) → 引擎联动在这种情况下会整条静默跳过, 必须由本模块接管并
    # 把权重合并, 否则原本正确的组变成孤儿、骨骼被外来组接管 ---
    reset_blend()
    arm3 = build_armature('SkelSrc3', SKEL_BONES)
    mesh3 = bpy.data.meshes.new('ClashMesh')
    mesh3.from_pydata([(0, 0, 0), (0, 0, 1), (1, 0, 0)], [], [(0, 1, 2)])
    mesh3.update()
    obj3 = bpy.data.objects.new('ClashMeshObj', mesh3)
    bpy.context.scene.collection.objects.link(obj3)
    mod3 = obj3.modifiers.new('Armature', 'ARMATURE')
    mod3.object = arm3

    real = obj3.vertex_groups.new(name='ArmL')          # 跟着骨骼走的那份
    real.add([0], 1.0, 'REPLACE')
    real.add([2], 0.25, 'REPLACE')
    foreign = obj3.vertex_groups.new(name='Bip001_L_Arm')  # 合并进来的外来那份
    foreign.add([1], 0.5, 'REPLACE')
    foreign.add([2], 0.25, 'REPLACE')                   # 与 real 在顶点2 上重叠

    report3 = skel.apply_rename(arm3, [('ArmL', 'Bip001_L_Arm')])
    names3 = {b.name for b in arm3.data.bones}
    check('顶点组撞名: 骨骼改名照常生效',
          'Bip001_L_Arm' in names3 and 'ArmL' not in names3)
    vg3 = [v.name for v in obj3.vertex_groups]
    check('顶点组撞名: 只剩一个目标组, 原组不残留 %s' % vg3,
          vg3.count('Bip001_L_Arm') == 1 and 'ArmL' not in vg3
          and not any(n.startswith(skel.VG_TMP_PREFIX) for n in vg3))
    gi = obj3.vertex_groups['Bip001_L_Arm'].index
    got = {v.index: round(e.weight, 4) for v in mesh3.vertices
           for e in v.groups if e.group == gi}
    check('顶点组撞名: 两份权重按顶点相加 %s' % got,
          got == {0: 1.0, 1: 0.5, 2: 0.5})
    check('顶点组撞名: 合并被报告出来, 不静默 %s' % (report3['merged'],),
          report3['merged'] == [('ClashMeshObj', 'ArmL', 'Bip001_L_Arm', 2)])
    check('顶点组撞名: 改完之后每个组都还能对上骨骼',
          {v.name for v in obj3.vertex_groups} <= {b.name for b in arm3.data.bones})

    # 反向再跑一次: 没有撞名时不该产生任何合并, 且组名跟着骨骼回去
    report3b = skel.apply_rename(arm3, [('Bip001_L_Arm', 'ArmL')])
    check('顶点组: 无撞名时不合并, 组名正常跟随',
          not report3b['merged']
          and 'ArmL' in {v.name for v in obj3.vertex_groups}
          and 'ArmL' in {b.name for b in arm3.data.bones})

    # --- 目标名冲突: 两个原名指向同一个新名 → 自动去重且被报告, 不静默覆盖 ---
    reset_blend()
    arm2 = build_armature('SkelSrc2', SKEL_BONES)
    report2 = skel.apply_rename(arm2, [('ArmL', 'X'), ('ArmR', 'X')])
    check('重命名: 目标名冲突被检测且未静默覆盖',
          len(report2['collided']) == 1
          and report2['collided'][0][0] == 'ArmR'
          and report2['collided'][0][2] != 'X')

    # --- 配置只有一个目录、一种格式 (禁止双重系统) ---
    spec = {'format': presets_mod.FORMAT, 'version': presets_mod.VERSION,
            'display_name': 'EndField_Si → Ruri', 'source_armature': 'EndField_Si',
            'renames': [{'from': 'J_Bip_C_Hips', 'to': 'Hips'}]}
    p1 = skel.save_rename_preset('_selftest_skel_rename', spec)
    check('配置保存落盘', os.path.isfile(p1))
    check('配置在唯一目录里可见',
          '_selftest_skel_rename' in presets_mod.list_presets())
    check('两个面板看到的是同一个目录',
          skel.list_rename_presets() == presets_mod.list_presets()
          and skel.rename_preset_dir() == presets_mod.preset_dir())
    loaded = skel.load_rename_preset('_selftest_skel_rename')
    check('display_name 往返', loaded['display_name'] == 'EndField_Si → Ruri')
    # 读改名表 → 存回去, 必须覆盖同一个文件而不是在别处再生成一份
    n_before = len(presets_mod.list_presets())
    pairs = [{'source': s_, 'dest': d_}
             for s_, d_, _e in presets_mod.normalize_pairs(loaded)]
    p2 = skel.save_rename_preset(
        '_selftest_skel_rename',
        presets_mod.make_spec(pairs, display_name=loaded['display_name']))
    check('读了再存 = 覆盖同一文件, 不产生第二份',
          p2 == p1 and len(presets_mod.list_presets()) == n_before)
    round2 = presets_mod.load_preset('_selftest_skel_rename')
    check('落盘格式只有 mappings 一种形状',
          'mappings' in round2 and 'renames' not in round2
          and round2['format'] == presets_mod.FORMAT)
    check('改写成 mappings 后内容不丢',
          [(m['source'], m['dest']) for m in round2['mappings']]
          == [('J_Bip_C_Hips', 'Hips')])
    skel.delete_rename_preset('_selftest_skel_rename')
    check('配置删除', not os.path.isfile(p1))

    # --- 操作符集成 ---
    reset_blend()
    try:
        addon.register()
    except ValueError:
        pass    # 已注册
    arm3 = build_armature('SkelSrc3', SKEL_BONES)
    arm4 = build_armature('SkelSrc4', SKEL_BONES, location=(2, 0, 0))
    for o in bpy.context.selected_objects:
        o.select_set(False)

    # 同时选中两个骨架 → 一次导出两份 (AI 对比命名的入口)
    arm3.select_set(True)
    arm4.select_set(True)
    bpy.context.view_layer.objects.active = arm3
    check('选中两个骨架时导出目标是两个',
          {o.name for o in skel.selected_armatures(bpy.context)}
          == {'SkelSrc3', 'SkelSrc4'})
    r = bpy.ops.animret.skeleton_export_structure()
    check('操作符: 导出骨架结构 FINISHED', r == {'FINISHED'})
    check('操作符: 两个骨架各落一份 json',
          os.path.isfile(skel.skeleton_export_path('SkelSrc3'))
          and os.path.isfile(skel.skeleton_export_path('SkelSrc4')))

    # 应用重命名只作用于活动骨架 (arm3), 另一个不受影响
    s = bpy.context.scene.animret_skel
    s.from_spec({'renames': [{'from': 'Root', 'to': 'Pelvis'},
                             {'from': 'Spine', 'to': 'Chest'}]})
    pairs = [(p.old_name, p.new_name) for p in s.pairs]

    def dirs(obj):
        return skel.count_directions(skel.armature_bone_names(obj), pairs)

    check('方向判定: 正向可用 反向不可用', dirs(arm3) == (2, 0))
    r = bpy.ops.animret.skeleton_rename_apply(reverse=False)
    check('操作符: 正向转换 FINISHED', r == {'FINISHED'})
    check('操作符: 骨骼确实改名',
          'Pelvis' in arm3.data.bones and 'Chest' in arm3.data.bones)
    check('操作符: 只改活动骨架, 另一个选中骨架不受影响',
          'Root' in arm4.data.bones and 'Pelvis' not in arm4.data.bones)

    # 双向: 反向把名字改回去
    check('方向判定: 转换后变成只能反向', dirs(arm3) == (0, 2))
    r = bpy.ops.animret.skeleton_rename_apply(reverse=True)
    check('操作符: 反向还原 FINISHED', r == {'FINISHED'})
    check('操作符: 还原回原名',
          'Root' in arm3.data.bones and 'Spine' in arm3.data.bones
          and 'Pelvis' not in arm3.data.bones)
    check('还原后方向判定回到正向', dirs(arm3) == (2, 0))

    # 配置浏览器: 磁盘上的配置直接列出来
    skel.save_rename_preset('_selftest_browse', {
        'format': presets_mod.FORMAT, 'version': presets_mod.VERSION,
        'display_name': '浏览器测试', 'source_armature': '',
        'renames': [{'from': 'ArmL', 'to': 'Arm_L'},
                    {'from': 'ArmR', 'to': 'Arm_R'}]})
    r = bpy.ops.animret.skeleton_refresh_entries()
    check('操作符: 刷新配置列表 FINISHED', r == {'FINISHED'})
    names = [e.name for e in s.entries]
    check('配置浏览器列出磁盘上的配置', '_selftest_browse' in names)
    s.active_entry = names.index('_selftest_browse')   # 点一行 = 加载
    check('点选配置即加载并展开',
          len(s.pairs) == 2 and s.pairs[0].new_name == 'Arm_L'
          and s.display_name == '浏览器测试')
    # 骨骼搜索过滤
    s.from_spec({'renames': [{'from': 'Bip001_L_Thigh', 'to': 'Thigh_L'},
                             {'from': 'Bip001_R_Thigh', 'to': 'Thigh_R'},
                             {'from': 'L_hairE_01_jnt', 'to': 'HairE_01_Jnt_L'},
                             {'from': 'eyeLf01Joint', 'to': 'Eye01Joint_L'}]})
    def nhit(q):
        return sum(1 for p in s.pairs if skel.pair_matches(p, q))
    check('搜索: 空 = 全部显示', nhit('') == 4)
    check('搜索: 命中新名一侧 (thigh -> 2)', nhit('thigh') == 2)
    check('搜索: 命中旧名一侧 (hairE -> 1)', nhit('hairE') == 1)
    check('搜索: 大小写不敏感', nhit('THIGH') == 2)
    check('搜索: 多词需全部命中 (thigh l -> 1)', nhit('thigh l') == 1)
    check('搜索: 无命中', nhit('zzzz') == 0)
    s.pair_filter = 'thigh'
    r = bpy.ops.animret.skeleton_clear_filter()
    check('操作符: 清空搜索', r == {'FINISHED'} and s.pair_filter == '')

    skel.delete_rename_preset('_selftest_browse')
    addon.unregister()


# ---------------------------------------------------------------------------
# Phase 7.5: 动画转换 — 同一份对照表改写动作内部的骨骼引用
#   引擎只替"挂在对象动画数据上"的动作改路径; 导进来堆在文件里没有使用者的
#   那一批它不管, 这一步管的就是那批。
# ---------------------------------------------------------------------------

def _paths_of(action):
    return {(fc.data_path, fc.array_index)
            for fc in core.iter_action_fcurves(action)}


def _count_under(action, bone):
    token = 'pose.bones["%s"]' % bone
    return sum(1 for fc in core.iter_action_fcurves(action)
               if fc.data_path.startswith(token))


def phase_action_rename():
    print('\n== Phase 7.5: 动画转换 (动作内部骨骼引用) ==')
    reset_blend()
    try:
        addon.register()
    except ValueError:
        pass
    skel = __import__(PKG + '.skeleton_rename', fromlist=['x'])

    arm = build_armature('ActSrc', SKEL_BONES)
    act = make_action(arm, 'GameAnim')
    for bone in ('Root', 'Spine', 'ArmL', 'ArmR'):
        key_bone(arm, bone, 1, quat=Quaternion((0, 0, 1), 0.3))
        key_bone(arm, bone, 10, quat=Quaternion((0, 0, 1), -0.3))
    arm.keyframe_insert('location', frame=1)     # 物体级通道: 不该被碰
    writer = core._ActionWriter(act, arm.name)
    writer.new_curve('pose.bones["ArmL"]["custom"]', 0, 'ArmL')
    writer.new_curve('pose.bones["ArmL"].constraints["c"].influence', 0, 'ArmL')

    # 摘掉指派 —— 模拟"导进来堆在文件里、没有使用者"的那一批动作
    act.use_fake_user = True
    arm.animation_data.action = None

    before = _paths_of(act)
    n_arm_l, n_arm_r = _count_under(act, 'ArmL'), _count_under(act, 'ArmR')
    check('自定义属性/约束路径也算骨骼引用 (ArmL %d 条)' % n_arm_l, n_arm_l == 6)

    pairs = [('Root', 'Pelvis'), ('Spine', 'Chest'),
             ('ArmL', 'ArmR'), ('ArmR', 'ArmL')]
    name_map = dict(pairs)
    names0 = skel.action_bone_names(act)
    check('解析出动作引用的骨骼名 %s' % sorted(names0),
          names0 == {'Root', 'Spine', 'ArmL', 'ArmR'})
    check('方向判定: 正向 4 / 反向 2',
          skel.count_directions(names0, pairs) == (4, 2))

    curves, groups, collided = skel.rename_action(act, name_map)
    paths = {p for p, _i in _paths_of(act)}
    check('骨骼引用已改写 (%d 条曲线)' % curves,
          'pose.bones["Pelvis"].rotation_quaternion' in paths
          and 'pose.bones["Chest"].rotation_quaternion' in paths
          and not any('"Root"' in p or '"Spine"' in p for p in paths))
    # 互换名若被改了两次就会原样退回去 —— 用两侧曲线条数是否对调来判
    check('互换名 ArmL↔ArmR 只改一次 (%d ↔ %d)' % (n_arm_l, n_arm_r),
          _count_under(act, 'ArmR') == n_arm_l
          and _count_under(act, 'ArmL') == n_arm_r)
    check('物体级通道未被波及', ('location', 0) in _paths_of(act))
    check('改完无重名曲线', not collided)
    gnames = {g.name for g in core.iter_action_groups(act)}
    check('骨骼通道组改名、非骨骼组不动 %s (%d 个)' % (sorted(gnames), groups),
          gnames == {'Pelvis', 'Chest', 'ArmL', 'ArmR', 'Object Transforms'})

    skel.rename_action(act, {n: o for o, n in name_map.items()})
    check('反向还原后与转换前逐条一致', _paths_of(act) == before)

    # 操作符 + 面板列表
    s = bpy.context.scene.animret_skel
    s.from_spec({'renames': [{'from': o, 'to': n} for o, n in pairs]})
    r = bpy.ops.animret.action_refresh()
    check('操作符: 添加当前文件的动作 FINISHED', r == {'FINISHED'})
    check('面板列出文件里的全部动作 (%d 条)' % len(s.actions),
          len(s.actions) == len(bpy.data.actions))
    e = next((x for x in s.actions if x.name == 'GameAnim'), None)
    check('列表项带骨骼数与双向计数',
          e is not None and e.bone_count == 4
          and e.forward == 4 and e.backward == 2)

    check('搜索: 命中动作名', skel.action_matches(e, 'game') is True)
    check('搜索: 无命中', skel.action_matches(e, 'zzzz') is False)
    s.action_filter = 'game'
    r = bpy.ops.animret.action_clear_filter()
    check('操作符: 清空动作搜索', r == {'FINISHED'} and s.action_filter == '')

    bpy.ops.animret.action_select(action='NONE')
    r = bpy.ops.animret.action_rename_apply(reverse=False)
    check('一条都没勾选时拒绝执行', r == {'CANCELLED'}
          and _paths_of(act) == before)

    bpy.ops.animret.action_select(action='ALL')
    r = bpy.ops.animret.action_rename_apply(reverse=False)
    check('操作符: 正向转换 FINISHED', r == {'FINISHED'})
    check('操作符转换后骨名已换掉',
          any('"Pelvis"' in p for p, _i in _paths_of(act)))
    e = next(x for x in s.actions if x.name == 'GameAnim')
    check('转换后方向计数自动翻面', e.forward == 2 and e.backward == 4)
    r = bpy.ops.animret.action_rename_apply(reverse=True)
    check('操作符: 反向还原 FINISHED 且逐条复原',
          r == {'FINISHED'} and _paths_of(act) == before)

    # 导入动画: 只要动作, 骨架/网格一概不留
    cli = __import__(PKG + '.cli', fromlist=['x'])
    tmp = tempfile.mkdtemp(prefix='animret_imp_')
    fbx_path = os.path.join(tmp, 'ImportedAnim.fbx')
    if not cli.ensure_fbx_addon():
        check('FBX 导入导出可用', False, '环境里没有 io_scene_fbx')
        addon.unregister()
        return
    core.assign_action(arm, act)
    for o in bpy.data.objects:
        o.select_set(o is arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.export_scene.fbx(filepath=fbx_path, use_selection=True,
                             bake_anim=True, object_types={'ARMATURE'})

    reset_blend()
    try:
        addon.register()
    except ValueError:
        pass
    actions, failures = core.import_actions([fbx_path])
    check('导入动画: 拿到动作 (%d 条, 失败 %d)' % (len(actions), len(failures)),
          len(actions) == 1 and not failures)
    check('导入动画: 一个对象都没留下 (%d)' % len(bpy.data.objects),
          len(bpy.data.objects) == 0)
    check('导入动画: 骨架数据块也没留下 (%d)' % len(bpy.data.armatures),
          len(bpy.data.armatures) == 0)
    check('导入动画: 按文件名命名并打了 fake user',
          actions and actions[0].name == 'ImportedAnim'
          and actions[0].use_fake_user)
    check('导入动画: 骨骼通道确实带进来了 (%d 根)'
          % len(skel.action_bone_names(actions[0]) if actions else ()),
          bool(actions) and len(skel.action_bone_names(actions[0])) >= 4)

    s = bpy.context.scene.animret_skel
    r = bpy.ops.animret.action_import(directory=tmp, whole_folder=True)
    check('操作符: 整个文件夹导入 FINISHED', r == {'FINISHED'})
    check('操作符: 导入后自动进列表 (%d 条)' % len(s.actions),
          len(s.actions) == len(bpy.data.actions) == 2)
    check('操作符: 导入后依然一个对象都没有', len(bpy.data.objects) == 0)
    r = bpy.ops.animret.action_import(directory=tmp, whole_folder=False)
    check('操作符: 没选文件时拒绝执行', r == {'CANCELLED'})

    # CLI 那条"要骨架"的导入路径与面板共用同一套底层, 一并验
    reset_blend()
    sources = cli.import_fbx_sources(fbx_path)
    check('CLI 导入: 拿到 (骨架, 动作) 且动作按文件名命名',
          len(sources) == 1 and sources[0][0].type == 'ARMATURE'
          and [a.name for a in sources[0][1]] == ['ImportedAnim'])

    addon.unregister()


def phase_api():
    print('\n== Phase 8: 稳定 API 门面 (供其他插件程序化调用) ==')
    import json
    api = __import__(PKG + '.api', fromlist=['x'])
    skel = __import__(PKG + '.skeleton_rename', fromlist=['x'])

    reset_blend()
    src = build_armature('ApiSrc', SRC_BONES, location=(0.2, -0.1, 0.05),
                         rotation=(0, 0, math.radians(10)), scale=0.01)
    build_src_actions(src)
    dst = build_armature('ApiDst', DST_BONES)
    settings = {'frame_step': 1.0, 'interpolation': 'LINEAR',
                'suffix': '_api', 'overwrite': True, 'bake_mode': 'PURE'}
    results, errors = api.retarget_actions(
        src, dst, base_spec()['mappings'], settings,
        [bpy.data.actions['Walk']])
    check('api.retarget_actions 无错误', not errors, str(errors))
    check('api.retarget_actions 产出 1 条动作', len(results) == 1)
    baked = bpy.data.actions.get('Walk_api')
    check('api.retarget_actions 落了目标动作 Walk_api', baked is not None)
    fp = _curve_fingerprint(baked) if baked else {}
    spans = [max(co[1::2]) - min(co[1::2])
             for (p, i), co in fp.items()
             if p.startswith('pose.bones["Hips"]') and len(co) >= 4]
    check('api 烘焙后目标骨 Hips 非常量 (max span=%.4f)'
          % (max(spans) if spans else -1.0),
          bool(spans) and max(spans) > 1e-4)

    default_path = api.export_skeleton_structure(src)
    check('api.export_skeleton_structure 默认落 SkeletonConfig 目录',
          os.path.dirname(default_path) == skel.skeleton_export_dir()
          and os.path.isfile(default_path))
    with open(default_path, 'r', encoding='utf-8') as f:
        doc = json.load(f)
    check('导出 JSON 读侧字段齐 (format/version/bones)',
          doc.get('format') == skel.SKELETON_FORMAT
          and doc.get('version') == skel.SKELETON_VERSION
          and isinstance(doc.get('bones'), list) and bool(doc['bones']))
    custom = os.path.join(tempfile.mkdtemp(prefix='animret_api_'), 'skel.json')
    got = api.export_skeleton_structure(src, custom)
    check('api.export_skeleton_structure 尊重自定义 out_path',
          got == custom and os.path.isfile(custom))

    spec = base_spec()
    name = '_selftest_api_mapping'
    saved = presets_mod.save_preset(name, spec)
    by_name = api.load_mapping(name)
    by_path = api.load_mapping(saved)
    check('load_mapping 预设名与绝对路径两种输入结果一致',
          by_name == by_path and bool(by_name))
    check('load_mapping 返回全部映射条数',
          len(by_name) == len(spec['mappings']))
    check('load_mapping 保留源/目标与每条附加参数 dict',
          by_name[0][0] == 'hips' and by_name[0][1] == 'Hips'
          and by_name[0][2].get('rot') is not None)
    presets_mod.delete_preset(name)

    fresh = __import__(PKG + '.api', fromlist=['x'])
    check('AnimationRetarget.api 可 import 且入口齐全 (消费方 ImportError 探测契约)',
          all(hasattr(fresh, entry) for entry in
              ('retarget_actions', 'load_mapping', 'mapping_dir',
               'export_skeleton_structure')))
    check('api.mapping_dir 指向预设目录',
          api.mapping_dir() == presets_mod.preset_dir())


# ---------------------------------------------------------------------------

def main():
    print('Blender %s | Python %s' % (bpy.app.version_string,
                                      sys.version.split()[0]))
    for phase in (phase_fk, phase_equivalence, phase_auto_align,
                  phase_manual_offset, phase_local_channel,
                  phase_bake, phase_empty_fcurves, phase_presets,
                  phase_operators, phase_skeleton_rename,
                  phase_action_rename, phase_api, phase_cli):
        try:
            phase()
        except Exception:
            FAILED.append((phase.__name__, 'EXCEPTION'))
            traceback.print_exc()

    print('\n================ 结果 ================')
    print('通过 %d 项, 失败 %d 项' % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print('  FAIL: %s  %s' % (name, detail))
    sys.exit(1 if FAILED else 0)


if __name__ == '__main__':
    main()
