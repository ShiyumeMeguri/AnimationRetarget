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
    Q = rot(src_rest_world)⁻¹ @ rot(dest_ref_world)

其中 dest_ref_world = 目标骨骼在"参考姿态"下的世界朝向 —— 参考姿态描述的是
"源骨架处于 rest 时目标骨架该摆成什么样"。这个姿态由 solve_reference_alignment
从两侧 rest 的关节几何直接解出 (把目标骨架虚拟地摆成源 rest 的样子), 因此
A-pose ↔ T-pose 等任意 rest 约定差异被逐骨自动解掉, 无需人工干预; 两侧约定
一致时解严格退化为自身 rest, 同构骨架的行为逐位不变。手动偏移叠加其上, 缺省只影响
被偏移的那根骨; 关掉 self_only 才按层级 FK 求值、带着整条子链一起转。

配置里**只记录关系, 不记录骨骼的固有数据**: 一行映射说的是"这根骨对应那根骨"
外加一个相对偏移。朝向是每次从两侧活 rest 现算的, 所以同一份表对任何骨架、任何
rest 约定都成立, 改名或重指向骨骼都不会让它失效。曾经存在过一个"捕捉对齐姿态"
——把目标骨的世界朝向快照进配置并**压过自动解**——它在骨架被重指向后就指向一个
该骨已经没有的朝向, 不报错、只错位, 而且分不清"过期"还是"故意"。已删除。

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

    def rest_world_length(self, i):
        """骨长的世界尺度 (物体缩放已计入)。"""
        b = self.bones[i]
        head = self.matrix_world @ b.rest.to_translation()
        tail = self.matrix_world @ (b.rest @ Vector((0.0, b.length, 0.0)))
        return (tail - head).length


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
                 'local_enabled', 'local_conv', 'local_scale', 'local_axis',
                 'local_connect',
                 'ik_enabled', 'ik_chain', 'ik_influence')

    def __init__(self):
        self.q_offset = Quaternion()
        self.loc_enabled = False
        self.loc_axes = (True, True, True)
        self.loc_scale = 1.0
        self.loc_rebase = False
        self.src_origin = Vector()
        self.dest_origin = Vector()
        self.local_enabled = False          # 局部通道 (位移/缩放) 透传
        self.local_conv = Quaternion()      # 源骨 rest 轴 → 目标骨 rest 轴
        self.local_scale = 1.0              # 两侧骨长比 (局部位移的尺度)
        self.local_axis = (0, 1, 2)         # 缩放通道的轴对应
        self.local_connect = False          # 目标骨 connected: 引擎会忽略基座位移
        self.ik_enabled = False
        self.ik_chain = 2
        self.ik_influence = 1.0


def _ortho_snap(q):
    e = q.to_euler('XYZ')
    for k in range(3):
        e[k] = round(e[k] / _HALF_PI) * _HALF_PI
    return e.to_quaternion()


def _solve_direction_alignment(pairs):
    """加权方向组 {(v_dest, v_src, w)} 的全局最优对齐旋转 (Wahba 问题,
    Davenport q-method: 最优四元数 = 4x4 增益矩阵 K 的最大特征向量,
    对称幂迭代求出 — 固定种子固定上限, 完全确定性)。

    任意约束配置都稳定: 单对 → 最小弧; 对称扇形 (骨盆 = 脊柱↑ + 双腿↓,
    方向和近零 — 旧的"和向量当主轴"在这里被噪声劫持出任意大旋转, 正是
    躯干 180° 翻转的根因) → 特征结构不经过和向量, 解仍是全局最优;
    真简并 (全部共线) 时滚转分量无约束, 幂迭代给出确定的一个。
    单约束的最优解族有一整维滚转自由度 — 那里的正确代表是零滚转,
    即解析最小弧, 不进特征迭代 (种子会把噪声滚转留在简并维里)。"""
    if not pairs:
        return Quaternion()
    if len(pairs) == 1:
        v_dest, v_src, _weight = pairs[0]
        return v_dest.rotation_difference(v_src)
    b00 = b01 = b02 = b10 = b11 = b12 = b20 = b21 = b22 = 0.0
    weight_total = 0.0
    for v_dest, v_src, weight in pairs:
        weight_total += weight
        b00 += weight * v_src.x * v_dest.x
        b01 += weight * v_src.x * v_dest.y
        b02 += weight * v_src.x * v_dest.z
        b10 += weight * v_src.y * v_dest.x
        b11 += weight * v_src.y * v_dest.y
        b12 += weight * v_src.y * v_dest.z
        b20 += weight * v_src.z * v_dest.x
        b21 += weight * v_src.z * v_dest.y
        b22 += weight * v_src.z * v_dest.z
    if weight_total <= 0.0:
        return Quaternion()
    trace = b00 + b11 + b22
    z0 = b12 - b21
    z1 = b20 - b02
    z2 = b01 - b10
    shift = 2.0 * weight_total
    matrix = [
        [trace + shift, z0, z1, z2],
        [z0, b00 + b00 - trace + shift, b01 + b10, b02 + b20],
        [z1, b01 + b10, b11 + b11 - trace + shift, b12 + b21],
        [z2, b02 + b20, b12 + b21, b22 + b22 - trace + shift],
    ]
    # 反复平方: M^(2^24) —— 有效幂 1.7e7, 特征值间隙再小也被碾平;
    # 每轮按最大元素归一防溢出。真简并时收敛到简并子空间投影, 种子选出确定代表。
    for _ in range(24):
        squared = [[sum(matrix[r][t] * matrix[t][c] for t in range(4))
                    for c in range(4)] for r in range(4)]
        peak = max(abs(squared[r][c]) for r in range(4) for c in range(4))
        if peak < 1e-300:
            return Quaternion()
        matrix = [[squared[r][c] / peak for c in range(4)] for r in range(4)]
    seed = (1.0, 0.02, 0.013, 0.007)
    q = tuple(sum(matrix[r][c] * seed[c] for c in range(4)) for r in range(4))
    norm = math.sqrt(sum(component * component for component in q))
    if norm < 1e-150:
        return Quaternion()
    solved = Quaternion((q[0] / norm, -q[1] / norm, -q[2] / norm, -q[3] / norm)).normalized()
    return _strip_unconstrained_twist(solved, pairs)


def _alignment_gain(rotation, pairs):
    return sum(weight * (rotation @ v_dest).dot(v_src)
               for v_dest, v_src, weight in pairs)


def _strip_unconstrained_twist(solved, pairs):
    """最优解族的最小角代表: 约束组共线 (rank-1, 现实骨架里 = 扭曲骨与主骨
    同向) 时绕主轴的滚转是一整维简并, 幂迭代把种子噪声留在那一维里;
    剥掉绕加权主轴的 twist, 仅当 Wahba 增益不降 (滚转确实无约束) 时采用。
    判据是最优性本身, 不是几何阈值。增益余量按 Σw 取相对值: mathutils 是
    float32, 复合噪声在 1e-7·Σw 量级, 而真被约束的滚转一剥就损失 1e-2·Σw
    量级 — 1e-5·Σw 隔着三个数量级坐在中间。"""
    principal = Vector((0.0, 0.0, 0.0))
    weight_total = 0.0
    for v_dest, v_src, weight in pairs:
        weight_total += weight
        principal = principal + v_src * (weight if principal.dot(v_src) >= 0.0
                                         else -weight)
    if principal.length_squared < 1e-24 or weight_total <= 0.0:
        return solved
    principal.normalize()
    twist_scalar = (solved.x * principal.x + solved.y * principal.y
                    + solved.z * principal.z)
    twist = Quaternion((solved.w, twist_scalar * principal.x,
                        twist_scalar * principal.y, twist_scalar * principal.z))
    if twist.magnitude < 1e-12:
        return solved
    twist.normalize()
    stripped = (twist.inverted() @ solved).normalized()
    margin = 1e-5 * weight_total
    if _alignment_gain(stripped, pairs) + margin >= _alignment_gain(solved, pairs):
        return stripped
    return solved


def solve_reference_alignment(src_skel, dest_skel, index_pairs):
    """自动参考姿态: {目标骨索引: 世界空间参考朝向} —— dest_ref 的构造本体。

    对每根映射骨 (目标骨架拓扑序), 它到每个"最近的已映射子孙"的 rest 关节
    偏移方向 (经父链累计旋转刚性传输) 必须对齐源侧对应骨间的 rest 方向;
    逐骨解 _solve_direction_alignment 并沿层级复合。方向约束在刚体传输下
    只依赖累计旋转, 无需 FK 位置簿记。无子孙约束的链末端纯继承父链传输,
    与父骨的 rest 关系原样保留。"""
    src_of = {di: si for si, di in index_pairs}
    children = [[] for _ in dest_skel.bones]
    for i, bone in enumerate(dest_skel.bones):
        if bone.parent >= 0:
            children[bone.parent].append(i)

    def mapped_descendants(start):
        found = []
        stack = list(children[start])
        while stack:
            j = stack.pop()
            if j in src_of:
                found.append(j)
            else:
                stack.extend(children[j])
        return found

    references = {}
    accumulated = {}
    for di in range(len(dest_skel.bones)):
        if di not in src_of:
            continue
        parent_rotation = Quaternion()
        j = dest_skel.bones[di].parent
        while j >= 0:
            if j in accumulated:
                parent_rotation = accumulated[j]
                break
            j = dest_skel.bones[j].parent
        head_dest = dest_skel.rest_world_head(di)
        head_src = src_skel.rest_world_head(src_of[di])
        pairs = []
        for dc in mapped_descendants(di):
            v_dest = dest_skel.rest_world_head(dc) - head_dest
            v_src = src_skel.rest_world_head(src_of[dc]) - head_src
            if v_dest.length_squared < 1e-14 or v_src.length_squared < 1e-14:
                continue
            # 权重 = 两侧偏移的较短者: 方向的置信度就是偏移长度 -- 毫米级
            # 关节堆叠 (Pelvis 与根几乎重合) 的方向是安放噪声, 不配约束姿态
            pairs.append(((parent_rotation @ v_dest).normalized(),
                          v_src.normalized(),
                          min(v_dest.length, v_src.length)))
        rotation = _solve_direction_alignment(pairs)
        acc = (rotation @ parent_rotation).normalized()
        accumulated[di] = acc
        references[di] = (acc @ dest_skel.rest_world_rot(di)).normalized()
    return references


def _axis_mapping(q):
    """局部轴换算 → 目标骨每根轴主要对应源骨的哪根轴 (缩放通道用)。

    缩放是骨骼局部帧里的对角量, 换帧后一般不再对角; 两套 rig 的轴差实际上
    都是 90° 倍数的置换, 取主轴即精确。
    """
    m = q.to_matrix()
    return tuple(max(range(3), key=lambda j: abs(m[i][j])) for i in range(3))


def _manual_offset(rot_spec):
    """映射 rot 字典里的手动偏移 → 四元数; 全零返回 None。"""
    offset = rot_spec.get('offset')
    if not offset or not any(abs(v) > 1e-9 for v in offset):
        return None
    return Euler(offset, 'XYZ').to_quaternion()


def _reference_corrections(dest_skel, rot_by_dest, auto_references):
    """手动偏移在目标骨架上按 FK 求值 → {骨索引: 相对自身参考朝向的修正}。

    偏移等价于"在姿态模式里绕自身轴向转这根骨"。**转出去的这一下要不要带着子链一起走,
    由每行的 ``self_only`` 说了算, 缺省是不带 (只影响这根骨本身)。**

    两种都得有, 因为它们回答的是两个不同的问题:

    * 只影响自身 (缺省): 这根骨的 rest 轴向跟对面不是一个约定, 要单独掰正。掰的是
      *它自己*的对应关系 —— 前臂、手掌各有各的约定, 不该被上臂掰的那一下连累。
      给上臂 Y 转 90° 却看见整条手臂都转了 90°, 就是这个缺省要消除的意外。
    * 带着子链 (关掉 self_only): 整段肢体的朝向约定成体系地偏了一个角度, 一次转到位。

    实现是两遍 FK: ``pose_chain`` 只含带子链的那些偏移, 子孙从它身上继承; ``pose_self``
    含全部偏移, 只供偏移骨自己取值。没有任何 self_only 偏移时两遍逐位相同, 于是行为
    与只有层级偏移的旧配置逐位不变。没有任何手动偏移时直接返回空表 (空态零成本)。
    """
    aligns = dict(auto_references)
    offsets, chained = {}, {}
    for di, rot_spec in rot_by_dest.items():
        q = _manual_offset(rot_spec)
        if q is None:
            continue
        offsets[di] = q
        if not rot_spec.get('self_only', True):
            chained[di] = q
    if not offsets:
        return {}

    def posed_by(applied):
        # 参考姿态先还原成各骨的 basis, 手动偏移再按骨骼自身轴向后乘上去
        _pose, bases = _apply_targets(dest_skel, aligns, {})
        for i, q in applied.items():
            local = q.to_matrix().to_4x4()
            base = bases.get(i)
            bases[i] = base @ local if base is not None else local
        return fk_pose(dest_skel, bases)

    pose_chain = posed_by(chained)
    pose_self = pose_chain if len(chained) == len(offsets) else posed_by(offsets)

    out = {}
    for di in rot_by_dest:
        if di in offsets:
            pose = pose_self            # 自己那一下, 两种偏移都算数
        else:
            i = dest_skel.bones[di].parent
            while i >= 0 and i not in chained:
                i = dest_skel.bones[i].parent
            if i < 0:      # 自身没偏移, 祖先也没有"带子链"的偏移 → 参考朝向原样不动
                continue
            pose = pose_chain
        ref = aligns[di] if di in aligns else dest_skel.rest_world_rot(di)
        posed = dest_skel.world_rot @ rot_of(pose[di])
        out[di] = (ref.inverted() @ posed).normalized()
    return out


def build_mappings(src_skel, dest_skel, mapping_dicts):
    """把 preset/state 的映射字典编译为 SolvedMapping 列表。

    mapping dict 结构 (与 JSON 预设一致):
      {"source": str, "dest": str,
       "rot": {"auto": bool, "ortho": bool, "offset": [x,y,z], "self_only": bool},
       "loc": {"enabled": bool, "axes": [bool*3], "scale_mode": "NONE|AUTO|MANUAL", "scale": float},
       "ik":  {"enabled": bool, "influence": float, "chain": int}}
    返回 (mappings, warnings)。无效映射跳过并记录警告。
    """
    out, warnings = [], []
    entries = []
    for md in mapping_dicts:
        sname, dname = md.get('source', ''), md.get('dest', '')
        if not sname or not dname:
            continue
        si = src_skel.index.get(sname)
        di = dest_skel.index.get(dname)
        if si is None or di is None:
            warnings.append('跳过映射 %s -> %s: 骨骼不存在' % (sname, dname))
            continue
        entries.append((md, sname, dname, si, di))

    # 自动参考姿态: 目标骨架按源 rest 的关节几何虚拟摆位 (见模块头)
    auto_references = solve_reference_alignment(
        src_skel, dest_skel, [(si, di) for _md, _s, _d, si, di in entries])
    # 手动偏移必须先在目标骨架上整体求值一次, 才能带动各自的子链
    corrections = _reference_corrections(
        dest_skel, {di: md.get('rot', {}) for md, _s, _d, _si, di in entries},
        auto_references)

    for md, sname, dname, si, di in entries:
        m = SolvedMapping()
        m.src_name, m.dest_name, m.src_i, m.dest_i = sname, dname, si, di

        rot = md.get('rot', {})
        q = Quaternion()
        if rot.get('auto', True):
            src_rest = src_skel.rest_world_rot(si)
            q = src_rest.inverted() @ auto_references[di]
            if rot.get('ortho', False):
                q = _ortho_snap(q)
        correction = corrections.get(di)
        if correction is not None:
            q = q @ correction
        m.q_offset = q.normalized()

        # 局部通道换算 (表情类微位移/缩放): 源骨 rest 轴 → 目标骨 rest 轴。
        # 两套脸的关节位置可以完全一致而骨骼轴向完全不同, 裸抄位移会朝错方向。
        local = md.get('local', {})
        m.local_enabled = bool(local.get('enabled', False)
                               if isinstance(local, dict) else local)
        if m.local_enabled:
            m.local_conv = (dest_skel.rest_world_rot(di).inverted()
                            @ src_skel.rest_world_rot(si)).normalized()
            src_len = src_skel.rest_world_length(si)
            m.local_scale = (dest_skel.rest_world_length(di) / src_len
                             if src_len > 1e-9 else 1.0)
            m.local_axis = _axis_mapping(m.local_conv)
            m.local_connect = dest_skel.bones[di].use_connect

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

def _apply_targets(dest_skel, rot_targets, loc_targets, local_targets=None):
    """按世界空间目标 (旋转/位置) + 局部通道目标重建目标骨架姿态。

    rot_targets:   {dest_i: Quaternion 世界旋转}
    loc_targets:   {dest_i: (Vector 世界位置, (bx,by,bz) 轴掩码)}
    local_targets: {dest_i: (Vector 局部位移, Vector 局部缩放)} —
                   已换算到目标骨 rest 帧, 直接落进 basis (表情类微位移)
    返回 (pose 骨架空间矩阵列表, bases {dest_i: basis 4x4})。
    未被任何目标触及的骨骼保持 rest (basis=I, 不输出)。
    """
    n = len(dest_skel.bones)
    pose = [None] * n
    bases = {}
    local_targets = local_targets or {}
    w = dest_skel.matrix_world
    w_inv = dest_skel.matrix_world_inv
    obj_rot_inv = dest_skel.world_rot_inv
    for i in range(n):
        bpt = bone_parent_transform(dest_skel, i, pose)
        cm = bpt_apply(bpt, _IDENTITY4)
        has_rot = i in rot_targets
        has_loc = i in loc_targets and not dest_skel.bones[i].use_connect
        has_local = i in local_targets
        if not has_rot and not has_loc and not has_local:
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
        elif has_local:
            # 局部位移作用在骨骼 rest 帧里 (与 Blender 的 basis 平移同一处)
            t_arm = bpt.loc_mat @ local_targets[i][0]
        # 只替换链矩阵的极分解旋转, 保留缩放/剪切残差 —
        # 目标旋转 == FK 旋转时 basis 严格等于单位阵 (恒等性逐位成立)
        cm3 = cm.to_3x3()
        stretch = rot_of(cm).to_matrix().inverted() @ cm3
        mat3 = q_arm.to_matrix() @ stretch
        if has_local:
            mat3 = mat3 @ Matrix.Diagonal(local_targets[i][1])
        mat = mat3.to_4x4()
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


_ONE = Vector((1.0, 1.0, 1.0))


def _local_channel(mapping, basis):
    """源骨 basis 的位移/缩放 → 换算到目标骨 rest 帧; 两者皆为空时返回 None。

    旋转不走这条路 (世界公式已经把两侧 rest 朝向差解干净了), 这里只补
    世界公式表达不了的两个通道 —— 关节式面部绑定的表情几乎全在这两个通道里。
    """
    t = basis.to_translation()
    s = basis.to_scale()
    moved = t.length_squared > 1e-18 and not mapping.local_connect
    scaled = (s - _ONE).length_squared > 1e-18
    if not moved and not scaled:
        return None
    t_out = (mapping.local_conv @ t) * mapping.local_scale if moved \
        else Vector((0.0, 0.0, 0.0))
    s_out = Vector((s[mapping.local_axis[0]], s[mapping.local_axis[1]],
                    s[mapping.local_axis[2]])) if scaled else _ONE.copy()
    return t_out, s_out


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


def solve_frame(dest_skel, mappings, src_world_mats, src_basis_mats):
    """单帧重定向求解 (纯函数)。

    src_world_mats: {源骨名: 世界空间姿态矩阵}
    src_basis_mats: {源骨名: basis 4x4} — 源骨骼的局部通道原值, 其中的位移与
        缩放会换算到目标骨 rest 帧后透传 (面部表情几乎全是这两个通道)。
    返回 {目标骨名: basis 4x4} — 直接可写入 pose.bones[].matrix_basis
    或拆解为 location / rotation / scale 关键帧。
    """
    rot_targets, loc_targets, local_targets = {}, {}, {}
    goals = {}
    for m in mappings:
        W = src_world_mats.get(m.src_name)
        if W is None:
            continue
        loc, rot, _sca = W.decompose()
        rot_targets[m.dest_i] = rot @ m.q_offset
        if m.local_enabled:
            basis = src_basis_mats.get(m.src_name)
            local = _local_channel(m, basis) if basis is not None else None
            if local is not None:
                local_targets[m.dest_i] = local
        if m.loc_enabled or m.ik_enabled:
            if m.loc_rebase:
                p = m.dest_origin + (loc - m.src_origin) * m.loc_scale
            else:
                p = loc.copy()
            goals[m.dest_i] = p
            if m.loc_enabled:
                loc_targets[m.dest_i] = (p, m.loc_axes)

    pose, bases = _apply_targets(dest_skel, rot_targets, loc_targets,
                                 local_targets)

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
            pose, bases = _apply_targets(dest_skel, rot_targets, loc_targets,
                                         local_targets)

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
