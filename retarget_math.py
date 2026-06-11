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

"""纯数学重定向内核 (bpy-free)

本模块不 import bpy, 只依赖 mathutils, 因此可以:
  - 在 blender --background 中运行 (CLI 无头烘焙)
  - 在装有 pip bpy / mathutils 的普通 Python 中做单元测试

骨骼父链评估逐行移植自 Blender 内核
BKE_bone_parent_transform_calc_from_matrices / _apply (armature.cc),
含 hinge (不继承旋转) 与全部 6 种 inherit_scale 模式 —
与 depsgraph 求值的偏差在 1e-5 量级 (见 tests/headless_selftest.py)。

核心重定向公式 (统一了 kumopult 世界旋转复制 与 Mwni rest-delta 相似变换,
二者数学上等价):

    R_dest_world(t) = R_src_world(t) @ Q
    Q = rot(src_rest_world)⁻¹ @ rot(dest_ref_world) @ R_manual

其中 dest_ref_world 默认取目标骨骼自身 rest 朝向; 若用户捕捉了"对齐姿态"
(让目标骨架摆出与源骨架 rest 相同的姿势), 则取对齐姿态下的世界朝向,
以此支持 A-pose ↔ T-pose 等不同 rest 约定之间的迁移。

位移传递 (根骨骼/武器):
    p' = origin_dest + (p_src_world - origin_src) * scale   (按世界轴向掩码混合)
    scale 为 1 / 自动髋高比 / 手动值。

IK 修正: 解析法双骨 IK (保持当前弯曲平面) + 单关节瞄准 + 多关节阻尼 CCD,
等价替代旧版 IK 约束 (chain_count 语义一致: 生效关节数 = 链长 - 1)。

求解器为纯函数: 同样输入永远得到同样输出, 不依赖场景状态 →
批量烘焙在结构上不可能产生漂移或残留。
"""

import math
from mathutils import Matrix, Vector, Quaternion, Euler
from math import pi

_EULER_ORDERS = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}
_HALF_PI = pi * 0.5
_FLT_EPSILON = 1.1920929e-07


def rotation_mode_is_euler(mode):
    return mode in _EULER_ORDERS


def rot_of(matrix):
    """取 4x4 矩阵的纯旋转四元数 (decompose 可耐受缩放/非正交)。"""
    return matrix.decompose()[1]


# ---------------------------------------------------------------------------
# BLI 矩阵工具 (逐行移植 math_matrix_c.cc; C 的 M[i] 是列 → mathutils 的 col)
# ---------------------------------------------------------------------------

def _get_cols(m):
    return [Vector((m[0][i], m[1][i], m[2][i])) for i in range(3)]


def _set_cols(m, cols):
    for i in range(3):
        m[0][i], m[1][i], m[2][i] = cols[i].x, cols[i].y, cols[i].z


def _orthogonalize_stable(v1, v2, v3, normalize):
    """BLI orthogonalize_stable: 保持 v1 方向不变的稳定正交化。"""
    len_sq_v1 = v1.length_squared
    if len_sq_v1 > 0.0:
        v2 = v2 - v1 * (v2.dot(v1) / len_sq_v1)
        v3 = v3 - v1 * (v3.dot(v1) / len_sq_v1)
        if normalize:
            v1 = v1 * (1.0 / math.sqrt(len_sq_v1))
    length_v2 = v2.length
    length_v3 = v3.length
    norm_v2 = v2 / length_v2 if length_v2 > 0.0 else v2.copy()
    norm_v3 = v3 / length_v3 if length_v3 > 0.0 else v3.copy()
    cos_angle = norm_v2.dot(norm_v3)
    abs_cos = abs(cos_angle)
    if 1e-4 < abs_cos < 1.0 - _FLT_EPSILON:
        angle = math.acos(max(-1.0, min(1.0, cos_angle)))
        target_angle = angle + (_HALF_PI - angle) / 2.0
        norm_v2 = norm_v2 - norm_v3 * cos_angle
        norm_v2 = norm_v2 * (math.sin(target_angle) / norm_v2.length)
        norm_v2 = norm_v2 + norm_v3 * math.cos(target_angle)
        tmp = norm_v2.cross(norm_v3)
        norm_v3 = tmp.cross(norm_v2)
        norm_v3.normalize()
        if not normalize:
            scale_fac = math.sqrt(math.sin(angle))
            v2 = norm_v2 * (length_v2 * scale_fac)
            v3 = norm_v3 * (length_v3 * scale_fac)
    if normalize:
        v2 = norm_v2
        v3 = norm_v3
    return v1, v2, v3


def _orthogonalize_m4_stable(m, axis, normalize):
    cols = _get_cols(m)
    if axis == 0:
        cols[0], cols[1], cols[2] = _orthogonalize_stable(
            cols[0], cols[1], cols[2], normalize)
    elif axis == 1:
        cols[1], cols[0], cols[2] = _orthogonalize_stable(
            cols[1], cols[0], cols[2], normalize)
    else:
        cols[2], cols[0], cols[1] = _orthogonalize_stable(
            cols[2], cols[0], cols[1], normalize)
    _set_cols(m, cols)


def _normalize_m4(m):
    cols = _get_cols(m)
    for i in range(3):
        ln = cols[i].length
        if ln != 0.0:
            cols[i] = cols[i] / ln
    _set_cols(m, cols)


def _normalize_m4_ex(m):
    cols = _get_cols(m)
    scale = Vector((0.0, 0.0, 0.0))
    for i in range(3):
        scale[i] = cols[i].length
        if scale[i] != 0.0:
            cols[i] = cols[i] / scale[i]
    _set_cols(m, cols)
    return scale


def _mat4_to_size(m):
    cols = _get_cols(m)
    return Vector((cols[0].length, cols[1].length, cols[2].length))


def _mat4_to_volume_scale(m):
    return m.to_3x3().determinant


def _mat4_to_size_fix_shear(m):
    size = _mat4_to_size(m)
    volume = size.x * size.y * size.z
    if volume != 0.0:
        size = size * (abs(_mat4_to_volume_scale(m) / volume) ** (1.0 / 3.0))
    return size


def _rescale_m4(m, scale):
    cols = _get_cols(m)
    for i in range(3):
        cols[i] = cols[i] * scale[i]
    _set_cols(m, cols)


def _mul_mat3_m4_fl(m, f):
    cols = _get_cols(m)
    for i in range(3):
        cols[i] = cols[i] * f
    _set_cols(m, cols)


# ---------------------------------------------------------------------------
# 骨架快照
# ---------------------------------------------------------------------------

class BoneSpec:
    """单根骨骼的静态快照 (与 bpy 解耦)。"""
    __slots__ = ('name', 'parent', 'rest', 'rest_inv', 'use_connect',
                 'inherit_rotation', 'inherit_scale', 'use_local_location',
                 'length', 'rotation_mode')

    def __init__(self, name, parent=-1, rest=None, use_connect=False,
                 inherit_rotation=True, inherit_scale='FULL',
                 length=1.0, rotation_mode='QUATERNION',
                 use_local_location=True):
        self.name = name
        self.parent = parent                  # 父骨索引, 无父 = -1
        self.rest = rest or Matrix.Identity(4)  # bone.matrix_local (骨架空间 rest)
        self.rest_inv = self.rest.inverted()
        self.use_connect = use_connect
        self.inherit_rotation = inherit_rotation
        self.inherit_scale = inherit_scale    # Blender 6 种模式原样支持
        self.use_local_location = use_local_location
        self.length = length
        self.rotation_mode = rotation_mode    # pose bone 的旋转模式


class Skeleton:
    """骨架快照: 拓扑有序骨骼数组 + 物体世界矩阵。"""

    def __init__(self, name, bones, matrix_world=None):
        self.name = name
        self.matrix_world = matrix_world or Matrix.Identity(4)
        # 拓扑排序: 父先于子 (传入顺序若已满足则保持稳定)
        order, placed = [], set()
        pending = list(range(len(bones)))
        while pending:
            advanced = False
            rest_pending = []
            for i in pending:
                p = bones[i].parent
                if p < 0 or p in placed:
                    order.append(i)
                    placed.add(i)
                    advanced = True
                else:
                    rest_pending.append(i)
            if not advanced:
                raise ValueError('骨架 %s 存在父子环' % name)
            pending = rest_pending
        remap = {old: new for new, old in enumerate(order)}
        self.bones = []
        for old in order:
            b = bones[old]
            nb = BoneSpec(b.name, remap[b.parent] if b.parent >= 0 else -1,
                          b.rest.copy(), b.use_connect, b.inherit_rotation,
                          b.inherit_scale, b.length, b.rotation_mode,
                          b.use_local_location)
            self.bones.append(nb)
        self.index = {b.name: i for i, b in enumerate(self.bones)}
        self.update_world(self.matrix_world)

    def update_world(self, matrix_world):
        """物体世界矩阵变更时刷新缓存 (含物体级动画的逐帧覆盖)。"""
        self.matrix_world = matrix_world.copy()
        self.matrix_world_inv = matrix_world.inverted()
        loc, rot, _sca = matrix_world.decompose()
        self.world_rot = rot
        self.world_rot_inv = rot.inverted()
        self.origin = loc

    def rest_world_rot(self, i):
        return self.world_rot @ rot_of(self.bones[i].rest)

    def rest_world_head(self, i):
        return self.matrix_world @ self.bones[i].rest.to_translation()


# ---------------------------------------------------------------------------
# 父链变换 — 逐行移植 BKE_bone_parent_transform_calc_from_matrices / _apply
# ---------------------------------------------------------------------------

class BoneParentTransform:
    __slots__ = ('rotscale_mat', 'loc_mat', 'post_scale')


def bone_parent_transform(skel, i, pose):
    """计算骨骼 i 的 (rotscale_mat, loc_mat, post_scale)。

    与 Blender 一致: hinge / 缩放继承模式只影响旋转缩放矩阵,
    骨骼头部位置永远由完整父变换 (loc_mat) 决定。
    """
    b = skel.bones[i]
    bpt = BoneParentTransform()
    bpt.post_scale = Vector((1.0, 1.0, 1.0))

    if b.parent < 0:
        offs_bone = b.rest
        bpt.rotscale_mat = offs_bone.copy()
        if not b.use_local_location:
            m = Matrix.Identity(4)
            m.translation = offs_bone.to_translation()
            bpt.loc_mat = m
        else:
            bpt.loc_mat = bpt.rotscale_mat
        return bpt

    p = skel.bones[b.parent]
    offs_bone = p.rest_inv @ b.rest
    parent_pose = pose[b.parent]
    parent_arm = p.rest
    use_rotation = b.inherit_rotation
    mode = b.inherit_scale
    full_transform = use_rotation and mode == 'FULL'

    if full_transform:
        bpt.rotscale_mat = parent_pose @ offs_bone
    else:
        if use_rotation:
            tmat = parent_pose.copy()
            if mode in {'FULL', 'FIX_SHEAR'}:
                pass
            elif mode in {'NONE', 'AVERAGE'}:
                _orthogonalize_m4_stable(tmat, 1, True)
            elif mode == 'ALIGNED':
                _orthogonalize_m4_stable(tmat, 1, False)
                bpt.post_scale = _normalize_m4_ex(tmat)
            elif mode == 'NONE_LEGACY':
                _normalize_m4(tmat)
        else:
            tmat = parent_arm.copy()
            if mode == 'FULL':
                _rescale_m4(tmat, _mat4_to_size(parent_pose))
            elif mode == 'FIX_SHEAR':
                _rescale_m4(tmat, _mat4_to_size_fix_shear(parent_pose))
            elif mode == 'ALIGNED':
                bpt.post_scale = _mat4_to_size_fix_shear(parent_pose)
            # NONE / AVERAGE / NONE_LEGACY: 保持无缩放
        if mode == 'AVERAGE':
            _mul_mat3_m4_fl(
                tmat, abs(_mat4_to_volume_scale(parent_pose)) ** (1.0 / 3.0))
        bpt.rotscale_mat = tmat @ offs_bone
        if mode == 'FIX_SHEAR':
            _orthogonalize_m4_stable(bpt.rotscale_mat, 1, False)

    if not b.use_local_location:
        bone_loc = Matrix.Identity(4)
        bone_loc.translation = parent_pose @ offs_bone.to_translation()
        rot3 = parent_pose.to_3x3().to_4x4()
        bpt.loc_mat = bone_loc @ rot3
    elif not full_transform:
        bpt.loc_mat = parent_pose @ offs_bone
    else:
        bpt.loc_mat = bpt.rotscale_mat
    return bpt


def bpt_apply(bpt, chan):
    """BKE_bone_parent_transform_apply: pose = rotscale@chan, 平移走 loc_mat。"""
    out = bpt.rotscale_mat @ chan
    out.translation = bpt.loc_mat @ chan.to_translation()
    if bpt.post_scale != Vector((1.0, 1.0, 1.0)):
        _rescale_m4(out, bpt.post_scale)
    return out


_IDENTITY4 = Matrix.Identity(4)


def fk_pose(skel, basis_by_index):
    """前向运动学: {骨索引: basis 4x4} → 骨架空间姿态矩阵列表。

    缺失项视为 rest (basis=I)。use_connect 骨骼的 basis 平移被忽略
    (与 Blender 评估一致)。
    """
    pose = [None] * len(skel.bones)
    for i, b in enumerate(skel.bones):
        bpt = bone_parent_transform(skel, i, pose)
        basis = basis_by_index.get(i)
        if basis is None:
            pose[i] = bpt_apply(bpt, _IDENTITY4)
            continue
        if b.use_connect:
            basis = basis.copy()
            basis.translation = Vector((0.0, 0.0, 0.0))
        pose[i] = bpt_apply(bpt, basis)
    return pose


def compose_basis(loc, rot_quat, scale):
    return Matrix.LocRotScale(loc, rot_quat, scale)


# ---------------------------------------------------------------------------
# 映射求解参数
# ---------------------------------------------------------------------------

class SolvedMapping:
    """单条映射的预解算参数 (rest 相关量只算一次)。"""
    __slots__ = ('src_name', 'dest_name', 'src_i', 'dest_i', 'q_offset',
                 'loc_enabled', 'loc_axes', 'loc_scale', 'loc_rebase',
                 'src_origin', 'dest_origin',
                 'ik_enabled', 'ik_chain', 'ik_influence')

    def __init__(self):
        self.q_offset = Quaternion()
        self.loc_enabled = False
        self.loc_axes = (True, True, True)
        self.loc_scale = 1.0
        self.loc_rebase = False
        self.src_origin = Vector()
        self.dest_origin = Vector()
        self.ik_enabled = False
        self.ik_chain = 2
        self.ik_influence = 1.0


def _ortho_snap(q):
    e = q.to_euler('XYZ')
    for k in range(3):
        e[k] = round(e[k] / _HALF_PI) * _HALF_PI
    return e.to_quaternion()


def build_mappings(src_skel, dest_skel, mapping_dicts):
    """把 preset/state 的映射字典编译为 SolvedMapping 列表。

    mapping dict 结构 (与 JSON 预设一致):
      {"source": str, "dest": str,
       "rot": {"auto": bool, "ortho": bool, "offset": [x,y,z], "align": null|[w,x,y,z]},
       "loc": {"enabled": bool, "axes": [bool*3], "scale_mode": "NONE|AUTO|MANUAL", "scale": float},
       "ik":  {"enabled": bool, "influence": float, "chain": int}}
    返回 (mappings, warnings)。无效映射跳过并记录警告。
    """
    out, warnings = [], []
    for md in mapping_dicts:
        sname, dname = md.get('source', ''), md.get('dest', '')
        if not sname or not dname:
            continue
        si = src_skel.index.get(sname)
        di = dest_skel.index.get(dname)
        if si is None or di is None:
            warnings.append('跳过映射 %s -> %s: 骨骼不存在' % (sname, dname))
            continue
        m = SolvedMapping()
        m.src_name, m.dest_name, m.src_i, m.dest_i = sname, dname, si, di

        rot = md.get('rot', {})
        q = Quaternion()
        if rot.get('auto', True):
            src_rest = src_skel.rest_world_rot(si)
            align = rot.get('align')
            if align:
                dest_ref = Quaternion(align)  # 捕捉对齐时已存为世界空间
            else:
                dest_ref = dest_skel.rest_world_rot(di)
            q = src_rest.inverted() @ dest_ref
            if rot.get('ortho', False):
                q = _ortho_snap(q)
        offset = rot.get('offset')
        if offset and any(abs(v) > 1e-9 for v in offset):
            q = q @ Euler(offset, 'XYZ').to_quaternion()
        m.q_offset = q.normalized()

        loc = md.get('loc', {})
        m.loc_enabled = bool(loc.get('enabled', False))
        if m.loc_enabled:
            axes = loc.get('axes', (True, True, True))
            m.loc_axes = (bool(axes[0]), bool(axes[1]), bool(axes[2]))
            mode = loc.get('scale_mode', 'NONE')
            m.src_origin = src_skel.origin.copy()
            m.dest_origin = dest_skel.origin.copy()
            if mode == 'AUTO':
                m.loc_rebase = True
                src_h = src_skel.rest_world_head(si).z - src_skel.origin.z
                dst_h = dest_skel.rest_world_head(di).z - dest_skel.origin.z
                if abs(src_h) > 1e-6:
                    m.loc_scale = dst_h / src_h
                else:  # 髋高不可用时退化为骨长比
                    sl = src_skel.bones[si].length
                    m.loc_scale = (dest_skel.bones[di].length / sl) if sl > 1e-9 else 1.0
            elif mode == 'MANUAL':
                m.loc_rebase = True
                m.loc_scale = float(loc.get('scale', 1.0))
            if dest_skel.bones[di].use_connect:
                warnings.append('映射 %s -> %s: 目标骨为 connected, 位移传递无效'
                                % (sname, dname))

        ik = md.get('ik', {})
        m.ik_enabled = bool(ik.get('enabled', False))
        m.ik_chain = max(2, int(ik.get('chain', 2)))
        m.ik_influence = max(0.0, min(1.0, float(ik.get('influence', 1.0))))
        out.append(m)
    return out, warnings


# ---------------------------------------------------------------------------
# IK 求解 (世界空间)
# ---------------------------------------------------------------------------

def _any_perpendicular(v):
    a = v.cross(Vector((1.0, 0.0, 0.0)))
    if a.length_squared < 1e-12:
        a = v.cross(Vector((0.0, 0.0, 1.0)))
    return a.normalized()


def solve_ik_world(joint_heads, effector, goal):
    """求关节世界旋转增量, 使刚性跟随的 effector 点到达 goal。

    joint_heads: 自上而下 (根关节在前) 的关节头世界坐标列表。
    返回与 joint_heads 等长的**绝对**世界增量四元数列表:
    new_world_rot[j] = delta[j] @ old_world_rot[j]。
    子关节的增量已复合父关节的增量 (子树刚性随动)。
    """
    n = len(joint_heads)
    if n == 0:
        return []
    if n == 1:
        a = effector - joint_heads[0]
        b = goal - joint_heads[0]
        if a.length_squared < 1e-12 or b.length_squared < 1e-12:
            return [Quaternion()]
        return [a.rotation_difference(b)]
    if n == 2:
        return _solve_two_bone(joint_heads[0], joint_heads[1], effector, goal)
    return _solve_ccd(joint_heads, effector, goal)


def _solve_two_bone(a_head, b_head, effector, goal):
    """解析双骨 IK: 保持当前弯曲平面 (隐式极向量), 余弦定理定肘角。"""
    upper = b_head - a_head
    lower = effector - b_head
    la, lb = upper.length, lower.length
    if la < 1e-6 or lb < 1e-6:
        return [Quaternion(), Quaternion()]
    to_goal = goal - a_head
    reach = max(min(to_goal.length, la + lb - 1e-7), abs(la - lb) + 1e-7)

    # 弯曲平面法线: 退化 (伸直) 时取任意垂直轴, 保证确定性
    axis = upper.cross(lower)
    if axis.length_squared < 1e-12:
        axis = _any_perpendicular(upper)
    else:
        axis = axis.normalized()

    # 1) 肘关节: 由余弦定理得新夹角, 绕弯曲平面法线旋转
    cos_cur = max(-1.0, min(1.0, upper.normalized().dot(lower.normalized())))
    cur = math.acos(cos_cur)            # upper 与 lower 的夹角 (= π - 肘内角)
    cos_new = max(-1.0, min(1.0, (la * la + lb * lb - reach * reach) / (2.0 * la * lb)))
    new = pi - math.acos(cos_new)
    dq_b = Quaternion(axis, new - cur)

    # 2) 根关节: 把旋转后的 effector 摆向 goal
    new_lower = dq_b @ lower
    new_eff = b_head + new_lower
    v_cur = new_eff - a_head
    if v_cur.length_squared < 1e-12 or to_goal.length_squared < 1e-12:
        dq_a = Quaternion()
    else:
        dq_a = v_cur.rotation_difference(to_goal)
    # 绝对增量: 肘关节随根关节刚性转动, 故复合 dq_a
    return [dq_a, (dq_a @ dq_b).normalized()]


def _solve_ccd(joint_heads, effector, goal, iterations=24, damping=0.9,
               tolerance=1e-6):
    """阻尼 CCD, 固定迭代上限, 完全确定性。返回各关节绝对增量。"""
    n = len(joint_heads)
    heads = [h.copy() for h in joint_heads]
    eff = effector.copy()
    acc = [Quaternion() for _ in range(n)]
    for _ in range(iterations):
        if (eff - goal).length < tolerance:
            break
        for j in range(n - 1, -1, -1):
            a = eff - heads[j]
            b = goal - heads[j]
            if a.length_squared < 1e-12 or b.length_squared < 1e-12:
                continue
            dq = a.rotation_difference(b)
            if damping < 1.0:
                dq = Quaternion().slerp(dq, damping)
            # 子树 (含 j 自身朝向) 刚性随动
            pivot = heads[j]
            for k in range(j, n):
                acc[k] = (dq @ acc[k]).normalized()
                if k > j:
                    heads[k] = pivot + dq @ (heads[k] - pivot)
            eff = pivot + dq @ (eff - pivot)
    return acc


# ---------------------------------------------------------------------------
# 单帧求解
# ---------------------------------------------------------------------------

def _apply_targets(dest_skel, rot_targets, loc_targets):
    """按世界空间目标 (旋转/位置) 重建目标骨架姿态。

    rot_targets: {dest_i: Quaternion 世界旋转}
    loc_targets: {dest_i: (Vector 世界位置, (bx,by,bz) 轴掩码)}
    返回 (pose 骨架空间矩阵列表, bases {dest_i: basis 4x4})。
    未被任何目标触及的骨骼保持 rest (basis=I, 不输出)。
    """
    n = len(dest_skel.bones)
    pose = [None] * n
    bases = {}
    w = dest_skel.matrix_world
    w_inv = dest_skel.matrix_world_inv
    obj_rot_inv = dest_skel.world_rot_inv
    for i in range(n):
        bpt = bone_parent_transform(dest_skel, i, pose)
        cm = bpt_apply(bpt, _IDENTITY4)
        has_rot = i in rot_targets
        has_loc = i in loc_targets and not dest_skel.bones[i].use_connect
        if not has_rot and not has_loc:
            pose[i] = cm
            continue
        if has_rot:
            q_arm = (obj_rot_inv @ rot_targets[i]).normalized()
        else:
            q_arm = rot_of(cm)
        t_arm = cm.to_translation()
        if has_loc:
            p_goal_w, axes = loc_targets[i]
            p_fk_w = w @ t_arm
            blended = Vector((p_goal_w.x if axes[0] else p_fk_w.x,
                              p_goal_w.y if axes[1] else p_fk_w.y,
                              p_goal_w.z if axes[2] else p_fk_w.z))
            t_arm = w_inv @ blended
        # 只替换链矩阵的极分解旋转, 保留缩放/剪切残差 —
        # 目标旋转 == FK 旋转时 basis 严格等于单位阵 (恒等性逐位成立)
        cm3 = cm.to_3x3()
        stretch = rot_of(cm).to_matrix().inverted() @ cm3
        mat = (q_arm.to_matrix() @ stretch).to_4x4()
        mat.translation = t_arm
        pose[i] = mat
        # 反解 basis: 3x3 走 rotscale, 平移走 loc_mat (post_scale 逆先除掉)
        m3 = mat.to_3x3()
        if bpt.post_scale != Vector((1.0, 1.0, 1.0)):
            inv = Vector((1.0 / s if abs(s) > 1e-12 else 0.0
                          for s in bpt.post_scale))
            m4 = m3.to_4x4()
            _rescale_m4(m4, inv)
            m3 = m4.to_3x3()
        chan3 = bpt.rotscale_mat.to_3x3().inverted() @ m3
        chan = chan3.to_4x4()
        chan.translation = bpt.loc_mat.inverted() @ t_arm
        bases[i] = chan
    return pose, bases


def _ik_joint_indices(dest_skel, dest_i, chain_count):
    """IK 生效关节: 自 dest 父骨向上 (chain_count - 1) 根, 根关节在前。"""
    joints = []
    j = dest_skel.bones[dest_i].parent
    for _ in range(chain_count - 1):
        if j < 0:
            break
        joints.append(j)
        j = dest_skel.bones[j].parent
    joints.reverse()
    return joints


def solve_frame(dest_skel, mappings, src_world_mats):
    """单帧重定向求解 (纯函数)。

    src_world_mats: {源骨名: 世界空间姿态矩阵}
    返回 {目标骨名: basis 4x4} — 直接可写入 pose.bones[].matrix_basis
    或拆解为 location / rotation 关键帧。
    """
    rot_targets, loc_targets = {}, {}
    goals = {}
    for m in mappings:
        W = src_world_mats.get(m.src_name)
        if W is None:
            continue
        loc, rot, _sca = W.decompose()
        rot_targets[m.dest_i] = rot @ m.q_offset
        if m.loc_enabled or m.ik_enabled:
            if m.loc_rebase:
                p = m.dest_origin + (loc - m.src_origin) * m.loc_scale
            else:
                p = loc.copy()
            goals[m.dest_i] = p
            if m.loc_enabled:
                loc_targets[m.dest_i] = (p, m.loc_axes)

    pose, bases = _apply_targets(dest_skel, rot_targets, loc_targets)

    # IK 修正: 按链根深度排序逐条求解, 每条解完整体重建一次 (确定性)
    ik_list = [m for m in mappings
               if m.ik_enabled and m.dest_i in rot_targets and m.ik_influence > 1e-6]
    if ik_list:
        def chain_root_depth(m):
            joints = _ik_joint_indices(dest_skel, m.dest_i, m.ik_chain)
            return joints[0] if joints else m.dest_i
        ik_list.sort(key=chain_root_depth)
        w = dest_skel.matrix_world
        for m in ik_list:
            joints = _ik_joint_indices(dest_skel, m.dest_i, m.ik_chain)
            if not joints:
                continue
            eff = (w @ pose[m.dest_i]).to_translation()
            goal = goals[m.dest_i].lerp(eff, 1.0 - m.ik_influence) \
                if m.ik_influence < 1.0 else goals[m.dest_i]
            heads = [(w @ pose[j]).to_translation() for j in joints]
            deltas = solve_ik_world(heads, eff, goal)
            for j, dq in zip(joints, deltas):
                cur = rot_of(w @ pose[j])
                rot_targets[j] = (dq @ cur).normalized()
            pose, bases = _apply_targets(dest_skel, rot_targets, loc_targets)

    return {dest_skel.bones[i].name: b for i, b in bases.items()}


# ---------------------------------------------------------------------------
# 烘焙后处理工具
# ---------------------------------------------------------------------------

def quaternion_make_consistent(quats):
    """保证四元数序列半球连续 (避免烘焙曲线翻转突变)。原地修改。"""
    for k in range(1, len(quats)):
        if quats[k - 1].dot(quats[k]) < 0.0:
            quats[k] = -quats[k]
    return quats
