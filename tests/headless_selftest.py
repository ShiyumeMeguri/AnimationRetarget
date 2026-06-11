# AnimationRetarget 无头自检 (养蛊场)
#
# 运行方式 (任选其一):
#   blender -b --factory-startup --python tests/headless_selftest.py
#   python tests/headless_selftest.py        (需 pip install bpy)
#
# 用 Blender depsgraph 求值结果作为 ground truth, 对抗性验证纯数学内核:
#   1. FK 忠实度 (connected / inherit_rotation / inherit_scale / euler / scale)
#   2. 与旧版约束 (COPY_ROTATION + COPY_LOCATION) 的数值等价
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
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0],
                 'align': None},
         'loc': {'enabled': True, 'axes': [True, True, True],
                 'scale_mode': loc_scale_mode, 'scale': 1.0}},
        {'source': 'spine', 'dest': 'Spine',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0],
                 'align': None}},
        {'source': 'upperarm', 'dest': 'UpArm',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0],
                 'align': None}},
        {'source': 'forearm', 'dest': 'LoArm',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0],
                 'align': None}},
        {'source': 'hand', 'dest': 'Hand',
         'rot': {'auto': auto, 'ortho': False, 'offset': [0, 0, 0],
                 'align': None}},
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
        world = core.src_world_mats_pure(src_skel, sampler, f, needed)
        dest_skel.update_world(dst.matrix_world)
        bases = rm.solve_frame(dest_skel, mappings, world)
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
         'rot': {'auto': True, 'ortho': False, 'offset': [0, 0, 0],
                 'align': None},
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
        world = core.src_world_mats_pure(skel_a, sampler, f, needed)
        bases = rm.solve_frame(skel_b, maps_id, world)
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
        world = core.src_world_mats_pure(src_skel, sampler, f, needed)
        dest_skel.update_world(dst.matrix_world)
        bases = rm.solve_frame(dest_skel, maps_ik, world)
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

def main():
    print('Blender %s | Python %s' % (bpy.app.version_string,
                                      sys.version.split()[0]))
    for phase in (phase_fk, phase_equivalence, phase_bake,
                  phase_empty_fcurves, phase_presets,
                  phase_operators, phase_cli):
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
