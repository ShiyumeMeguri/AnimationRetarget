"""MMD 表情 VMD 重定向到游戏脸骨: VMD morph 时间线 -> 预设表 -> 控制项权重 -> 脸骨关键帧, 外加修正表(按网格几何标定的骨骼偏移)。
解算(solve_*)需要 RuriRipperImporter 的脸表与资产库; 写入(write_curves)只需要 bpy 与 numpy, 可在不启用任何插件的会话里存盘。"""

import json
import os
import struct

import numpy

PRESET_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "presets", "AnimationRetarget"))
MORPH_PRESET = "MMDMorphToEndfieldFace.json"
CORRECTION_PRESET = "MMDFaceCorrections.json"


def load_json(name):
    with open(os.path.join(PRESET_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def save_corrections(spec):
    with open(os.path.join(PRESET_DIR, CORRECTION_PRESET), "w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def read_vmd_morphs(path):
    data = open(path, "rb").read()
    bones = struct.unpack_from("<I", data, 50)[0]
    offset = 54 + bones * 111
    count = struct.unpack_from("<I", data, offset)[0]
    series = {}
    for index in range(count):
        base = offset + 4 + index * 23
        name = data[base:base + 15].split(b"\0")[0].decode("cp932", "replace")
        frame, weight = struct.unpack_from("<If", data, base + 15)
        series.setdefault(name, []).append((frame, weight))
    length = max(frame for keys in series.values() for frame, _ in keys) + 1
    timelines = {}
    for name, keys in series.items():
        keys.sort()
        timelines[name] = numpy.interp(numpy.arange(length), [frame for frame, _ in keys], [weight for _, weight in keys])
    return timelines, length


def asset_weights(morph_state, skeletal_morph, name, at=0.0, side=None, prefixes=None):
    weights = skeletal_morph.sample_weights(morph_state.ASSETS[name], at)
    if prefixes:
        weights = {ctrl: weight for ctrl, weight in weights.items() if ctrl.startswith(tuple(prefixes))}
    if side is not None:
        suffix = "_%s_ctrl" % side
        weights = {ctrl: weight for ctrl, weight in weights.items() if ctrl.endswith(suffix)}
    return {ctrl: weight for ctrl, weight in weights.items() if weight != 0.0}


class Resolved:
    def __init__(self, mapping, morph_state, skeletal_morph):
        self.mapping = mapping
        self.plain = {}
        self.phonemes = {}
        self.mouth = {driver.ctrl for asset in morph_state.ASSETS.values() if asset.kind in ("emotion", "pose") for driver in asset.channels.get("_mouthValue", ())}
        lipsync = morph_state.ASSETS[mapping["LipsyncConfig"]]
        sets = mapping["LipsyncSets"]
        for morph, targets in mapping["Morphs"].items():
            for target in targets:
                if "Phoneme" in target:
                    slot = int(target["Phoneme"])
                    normal = asset_weights(morph_state, skeletal_morph, lipsync.phoneme_sets[sets["Normal"]][slot])
                    happy = asset_weights(morph_state, skeletal_morph, lipsync.phoneme_sets[sets["Happy"]][slot])
                    self.phonemes.setdefault(morph, []).append((normal, happy))
                else:
                    self.plain.setdefault(morph, []).append(asset_weights(morph_state, skeletal_morph, target["Asset"], target.get("At", 0.0), target.get("Side"), target.get("CtrlPrefixes")))

    def at(self, morph_weights, happy):
        emotion = {}
        sung = {}
        coverage = 0.0
        for morph, weight in morph_weights.items():
            if weight <= 1e-4:
                continue
            for ctrls in self.plain.get(morph, ()):
                for ctrl, value in ctrls.items():
                    emotion[ctrl] = emotion.get(ctrl, 0.0) + value * weight
            for normal, happy_ctrls in self.phonemes.get(morph, ()):
                coverage += weight
                for ctrl, value in normal.items():
                    sung[ctrl] = sung.get(ctrl, 0.0) + value * weight * (1.0 - happy)
                for ctrl, value in happy_ctrls.items():
                    sung[ctrl] = sung.get(ctrl, 0.0) + value * weight * happy
        keep = 1.0 - min(1.0, coverage)
        out = {ctrl: value * keep if ctrl in self.mouth else value for ctrl, value in emotion.items()}
        for ctrl, value in sung.items():
            out[ctrl] = out.get(ctrl, 0.0) + value
        return {ctrl: max(-1.0, min(1.0, value)) for ctrl, value in out.items()}


def frame_weights(resolved, timelines, length):
    mapping = resolved.mapping
    happy = numpy.zeros(length)
    for name in mapping["HappyMorphs"]:
        if name in timelines:
            happy = numpy.maximum(happy, timelines[name][:length])
    names = [name for name in timelines if name in resolved.plain or name in resolved.phonemes]
    if mapping.get("NormalizePhonemes"):
        timelines = dict(timelines)
        for name in names:
            peak = float(timelines[name].max())
            if name in resolved.phonemes and 0.0 < peak < 1.0:
                timelines[name] = timelines[name] / peak
    return [resolved.at({name: float(timelines[name][frame]) for name in names}, float(happy[frame])) for frame in range(length)]


def correction_weights(rows, correction):
    coefficients = correction["CtrlCoefficients"]
    weights = numpy.zeros(len(rows))
    for index, row in enumerate(rows):
        weights[index] = sum(value * coefficients.get(ctrl, 0.0) for ctrl, value in row.items())
    return numpy.clip(weights, 0.0, 1.0)


CHANNEL_TOLERANCE = {"location": 5e-5, "rotation_quaternion": 1e-4, "scale": 1e-5}


def reduce_keys(frames, values, tolerance):
    keep = numpy.zeros(len(frames), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(frames) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        span = frames[first + 1:last]
        line = values[first] + (values[last] - values[first]) * (span - frames[first]) / (frames[last] - frames[first])
        error = numpy.abs(values[first + 1:last] - line)
        worst = int(error.argmax())
        if error[worst] > tolerance:
            keep[first + 1 + worst] = True
            stack.append((first, first + 1 + worst))
            stack.append((first + 1 + worst, last))
    return numpy.flatnonzero(keep)


def solve_curves(binding, rows, corrections, first_frame):
    animated = set()
    for row in rows:
        animated |= set(row)
    weights = [(correction, correction_weights(rows, correction)) for correction in corrections["Corrections"]]
    offsets = {}
    for correction, weight in weights:
        if not weight.any():
            continue
        for bone in correction["Bones"]:
            offsets.setdefault(bone, []).append((numpy.array(correction.get("Offset", (0.0, 0.0, 0.0)), dtype=numpy.float64), numpy.array(correction.get("Scale", (1.0, 1.0, 1.0)), dtype=numpy.float64), weight))
    bones = set(binding.bones_for(animated)) | {bone for bone in offsets if bone in binding.governed_bones}
    count = len(rows)
    frames = numpy.arange(count, dtype=numpy.float64) + first_frame
    keys = [tuple(sorted(row.items())) for row in rows]
    cache = {}
    curves = []
    for name in sorted(bones):
        locations = numpy.empty((count, 3), dtype=numpy.float32)
        quaternions = numpy.empty((count, 4), dtype=numpy.float32)
        scales = numpy.empty((count, 3), dtype=numpy.float32)
        for index, key in enumerate(keys):
            entry = cache.get((name, key))
            if entry is None:
                location, rotation, scale = binding.basis_at(dict(key), name).decompose()
                entry = cache[(name, key)] = ((location.x, location.y, location.z), (rotation.w, rotation.x, rotation.y, rotation.z), (scale.x, scale.y, scale.z))
            locations[index], quaternions[index], scales[index] = entry
        for offset, scale_target, weight in offsets.get(name, ()):
            locations += (weight[:, None] * offset[None, :]).astype(numpy.float32)
            scales *= (1.0 + weight[:, None] * (scale_target[None, :] - 1.0)).astype(numpy.float32)
        flips = numpy.cumsum(numpy.einsum("ij,ij->i", quaternions[1:], quaternions[:-1]) < 0.0) % 2
        quaternions[1:][flips == 1] *= -1.0
        prefix = 'pose.bones["%s"].' % name
        for channel, values in (("location", locations), ("rotation_quaternion", quaternions), ("scale", scales)):
            for axis in range(values.shape[1]):
                column = values[:, axis].astype(numpy.float64)
                kept = reduce_keys(frames, column, CHANNEL_TOLERANCE[channel])
                co = numpy.empty(len(kept) * 2, dtype=numpy.float64)
                co[0::2] = frames[kept]
                co[1::2] = column[kept]
                curves.append((prefix + channel, axis, co))
    return curves, sorted(binding.governed_bones)


def write_curves(rig, action, slot, curves, governed):
    fcurves = action.layers[0].strips[0].channelbag(slot, ensure=True).fcurves
    prefixes = tuple('pose.bones["%s"].' % name for name in governed)
    for fcurve in [fcurve for fcurve in fcurves if fcurve.data_path.startswith(prefixes)]:
        fcurves.remove(fcurve)
    for name in governed:
        pose_bone = rig.pose.bones.get(name)
        if pose_bone is not None:
            pose_bone.location = (0.0, 0.0, 0.0)
            pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pose_bone.rotation_euler = (0.0, 0.0, 0.0)
            pose_bone.scale = (1.0, 1.0, 1.0)
    for path, axis, co in curves:
        count = len(co) // 2
        fcurve = fcurves.new(path, index=axis)
        fcurve.keyframe_points.add(count)
        fcurve.keyframe_points.foreach_set("co", co)
        fcurve.keyframe_points.foreach_set("interpolation", numpy.full(count, 1, dtype=numpy.int32))
        fcurve.update()
    return len(curves)


def head_space_points(bpy, rig, mesh_object, head_bone):
    pose_bone = rig.pose.bones[head_bone]
    rest = rig.data.bones[head_bone].matrix_local
    inverse = (rig.matrix_world @ (pose_bone.matrix @ rest.inverted())).inverted()
    evaluated = mesh_object.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    points = numpy.array([tuple(inverse @ (mesh_object.matrix_world @ vertex.co)) for vertex in mesh.vertices])
    evaluated.to_mesh_clear()
    return points


def group_vertices(mesh_object, names, threshold=0.5):
    wanted = {group.index for group in mesh_object.vertex_groups if group.name in names}
    return [vertex.index for vertex in mesh_object.data.vertices if sum(item.weight for item in vertex.groups if item.group in wanted) > threshold]


def measure(bpy, rig, binding, spec, weights):
    mesh_object = bpy.data.objects[spec["Mesh"]]
    rig.animation_data.action = None
    ids = group_vertices(mesh_object, spec["Groups"])
    binding.apply({})
    bpy.context.view_layer.update()
    rest = head_space_points(bpy, rig, mesh_object, spec["HeadBone"])[ids]
    binding.apply(weights)
    bpy.context.view_layer.update()
    posed = head_space_points(bpy, rig, mesh_object, spec["HeadBone"])[ids]
    return rest, posed


def displacement_along(rest, posed, axis):
    return float(-((posed - rest).mean(axis=0) @ numpy.array(axis, dtype=numpy.float64)))


def displacement_rms(rest, posed):
    return float(numpy.sqrt(((posed - rest) ** 2).sum(axis=1).mean()))


def calibrate(bpy, rig, binding, morph_state, skeletal_morph, spec, candidates):
    """按网格几何标定每个控制项对该修正的系数, 写回 spec 的 CtrlCoefficients。"""
    measurement = spec["Measure"]
    if measurement["Mode"] == "Linked":
        return spec["CtrlCoefficients"]
    if measurement["Mode"] == "Closure":
        reference = asset_weights(morph_state, skeletal_morph, measurement["ReferenceAsset"], measurement["ReferenceAt"])
        rest, posed = measure(bpy, rig, binding, measurement, reference)
        full = displacement_along(rest, posed, measurement["Axis"])
        scale = lambda rest, posed: displacement_along(rest, posed, measurement["Axis"]) / full
    else:
        scale = lambda rest, posed: displacement_rms(rest, posed) / measurement["Saturation"]
    coefficients = {}
    for ctrl in candidates:
        rest, posed = measure(bpy, rig, binding, measurement, {ctrl: 1.0})
        value = max(0.0, min(1.0, scale(rest, posed)))
        if value > 0.02:
            coefficients[ctrl] = round(value, 4)
    spec["CtrlCoefficients"] = coefficients
    return coefficients


def link_corrections(spec):
    by_name = {correction["Name"]: correction for correction in spec["Corrections"]}
    for correction in spec["Corrections"]:
        if correction["Measure"]["Mode"] == "Linked":
            correction["CtrlCoefficients"] = dict(by_name[correction["Measure"]["Source"]]["CtrlCoefficients"])
