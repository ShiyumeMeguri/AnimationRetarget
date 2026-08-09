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

"""AnimationRetarget 无头 CLI — 不开 Blender 界面直接批量重定向烘焙/导出。

两种运行方式 (二选一):

  A. 用 Blender 可执行文件:
     blender -b 场景.blend --python cli.py -- bake --preset 映射.json ^
         --export-fbx D:\\out --per-action

  B. 完全不装 Blender, 用 pip 的 bpy 模块 (pip install bpy):
     python cli.py bake --blend 场景.blend --preset 映射.json ^
         --export-fbx D:\\out --per-action

子命令:
  list      列出场景中的骨架与动作 (JSON 输出, 供 AI/脚本解析)
  validate  校验映射配置与骨架的匹配情况
  bake      批量重定向烘焙 (+可选 FBX 导出 / 保存 blend)

所有进度与结果都会输出 "@ANIMRET {json}" 行, 方便程序化解析。
"""

import argparse
import fnmatch
import json
import os
import sys

import bpy

# 让本目录可作为包导入 (无论插件是否已在 Blender 中启用)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

_PKG = os.path.basename(_HERE)
core = __import__(_PKG + '.core', fromlist=['core'])
rm = __import__(_PKG + '.retarget_math', fromlist=['retarget_math'])
presets = __import__(_PKG + '.presets', fromlist=['presets'])


def emit(event, **kw):
    kw['event'] = event
    print('@ANIMRET ' + json.dumps(kw, ensure_ascii=False))
    sys.stdout.flush()


def fail(msg, code=1):
    emit('error', message=msg)
    sys.exit(code)


# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog='AnimationRetarget CLI', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp):
        sp.add_argument('--blend', help='要打开的 .blend 文件 (已在 Blender 内打开时省略)')
        sp.add_argument('--dest', help='目标骨架对象名 (省略时取配置里的 dest_armature)')
        sp.add_argument('--source', help='来源骨架对象名 (省略时取配置里的 source_armature)')
        sp.add_argument('--preset', help='JSON 配置: 文件路径或用户预设名 (省略时用目标骨架上嵌入的配置)')

    sp = sub.add_parser('list', help='列出骨架与动作')
    sp.add_argument('--blend')

    sp = sub.add_parser('validate', help='校验配置')
    common(sp)

    sp = sub.add_parser('bake', help='批量重定向烘焙')
    common(sp)
    sp.add_argument('--actions', help='动作名通配符, 逗号分隔, 如 "Walk*,Run*" (默认: 全部可烘动作)')
    sp.add_argument('--suffix', help='产物后缀 (默认取配置, 再默认 _baked)')
    sp.add_argument('--step', type=float, help='帧步长')
    sp.add_argument('--frame-range', help='帧范围覆盖 A:B')
    sp.add_argument('--mode', choices=['PURE', 'SCENE'], help='采样模式')
    sp.add_argument('--interp', choices=['LINEAR', 'BEZIER'], help='关键帧插值')
    sp.add_argument('--no-overwrite', action='store_true', help='不覆盖同名产物')
    sp.add_argument('--import-fbx', help='先批量导入动画 FBX 作为来源动作: 目录或分号分隔的文件列表')
    sp.add_argument('--save-blend', help='烘焙后另存 .blend 到此路径')
    sp.add_argument('--export-fbx', help='导出 FBX 的目录')
    sp.add_argument('--per-action', action='store_true',
                    help='每条动作导出一个 FBX (文件名=动作名去后缀)')
    sp.add_argument('--single', help='所有动作经 NLA 合并导出为单个 FBX (指定文件名)')
    sp.add_argument('--with-meshes', action='store_true', help='连同目标骨架的子网格一起导出')
    sp.add_argument('--fbx-args', help='透传给 FBX 导出器的 JSON 参数字典')

    # 兼容 blender --python cli.py -- <args> 与 python cli.py <args>
    if '--' in argv:
        argv = argv[argv.index('--') + 1:]
    return p.parse_args(argv)


# ---------------------------------------------------------------------------

def open_blend(path):
    if not path:
        return
    if not os.path.isfile(path):
        fail('blend 文件不存在: %s' % path)
    bpy.ops.wm.open_mainfile(filepath=path)
    emit('opened', blend=path)


def find_armature(name, role):
    if name:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'ARMATURE':
            fail('%s 骨架 "%s" 不存在或不是骨架, 可用: %s' % (
                role, name,
                [o.name for o in bpy.data.objects if o.type == 'ARMATURE']))
        return obj
    return None


def spec_from_idprops(arm_data):
    """无需插件注册, 直接从骨架数据读取嵌入配置。

    插件已注册时走 RNA; 否则读 IDProps (5.x 在 system properties 容器,
    旧版直接挂在 ID 上)。
    """
    st = getattr(arm_data, 'animret', None)
    if st is not None and hasattr(st, 'to_spec'):
        spec = st.to_spec()
        return spec if spec.get('mappings') else None
    raw = None
    sys_get = getattr(arm_data, 'bl_system_properties_get', None)
    if sys_get is not None:
        try:
            grp = sys_get()
            if grp is not None:
                raw = grp.get('animret')
        except Exception:
            raw = None
    if raw is None:
        raw = arm_data.get('animret')
    if raw is None:
        return None
    try:
        data = raw.to_dict()
    except AttributeError:
        return None
    mappings = []
    for it in data.get('mappings', []):
        md = {
            'source': it.get('source', ''),
            'dest': it.get('dest', ''),
            'rot': {
                'auto': bool(it.get('rot_auto', 1)),
                'ortho': bool(it.get('rot_ortho', 0)),
                'offset': list(it.get('rot_offset', (0.0, 0.0, 0.0))),
                'align': list(it.get('align_rot'))
                if it.get('align_set') and it.get('align_rot') else None,
            },
        }
        if it.get('loc_enabled'):
            md['loc'] = {
                'enabled': True,
                'axes': [bool(v) for v in it.get('loc_axes', (1, 1, 1))],
                'scale_mode': ('NONE', 'AUTO', 'MANUAL')[
                    it['loc_scale_mode']] if isinstance(
                    it.get('loc_scale_mode'), int) else
                it.get('loc_scale_mode', 'NONE'),
                'scale': float(it.get('loc_scale', 1.0)),
            }
        if it.get('ik_enabled'):
            md['ik'] = {'enabled': True,
                        'influence': float(it.get('ik_influence', 1.0)),
                        'chain': int(it.get('ik_chain', 2))}
        if md['source'] and md['dest']:
            mappings.append(md)
    if not mappings:
        return None
    settings = {}
    return {'format': 'AnimationRetarget', 'version': 2,
            'source_armature': '', 'dest_armature': '',
            'settings': settings, 'mappings': mappings}


def resolve_spec(args):
    if args.preset:
        if os.path.isfile(args.preset):
            spec = presets.load_preset_file(args.preset)
        else:
            try:
                spec = presets.load_preset(args.preset)
            except Exception:
                fail('找不到预设: %s (不是文件, 用户预设里也没有)' % args.preset)
        emit('preset_loaded', preset=args.preset,
             mappings=len(spec.get('mappings', [])))
        return spec
    return None


def resolve_rigs(args, spec):
    dest = find_armature(args.dest, '目标')
    src = find_armature(args.source, '来源')
    if dest is None and spec:
        dest = find_armature(spec.get('dest_armature') or None, '目标')
    if src is None and spec:
        src = find_armature(spec.get('source_armature') or None, '来源')
    arms = [o for o in bpy.data.objects if o.type == 'ARMATURE']
    if (dest is None or src is None) and spec:
        # 按映射骨名与各骨架骨骼的交集自动判别
        want_dest = {m.get('dest') for m in spec.get('mappings', [])}
        want_src = {m.get('source') for m in spec.get('mappings', [])}

        def best(want, exclude):
            scored = [(len(want & {b.name for b in o.data.bones}), o)
                      for o in arms if o is not exclude]
            scored.sort(key=lambda kv: kv[0], reverse=True)
            return scored[0][1] if scored and scored[0][0] > 0 else None
        if dest is None:
            dest = best(want_dest, src)
        if src is None:
            src = best(want_src, dest)
        if dest is not None and src is not None:
            emit('auto_detected', dest=dest.name, source=src.name)
    if dest is None or src is None:
        if len(arms) == 2 and (dest or src):
            other = [o for o in arms if o is not (dest or src)][0]
            dest = dest or other
            src = src or other
        else:
            fail('无法确定骨架, 请用 --dest/--source 指定。场景中的骨架: %s'
                 % [o.name for o in arms])
    if dest is src:
        fail('目标骨架与来源骨架不能是同一个对象')
    if spec is None:
        spec = spec_from_idprops(dest.data)
        if spec is None:
            fail('未给 --preset 且目标骨架上没有嵌入配置')
        emit('preset_loaded', preset='<embedded:%s>' % dest.name,
             mappings=len(spec['mappings']))
    return src, dest, spec


def build_settings(args, spec):
    settings = dict(spec.get('settings') or {})
    settings.setdefault('frame_step', 1.0)
    settings.setdefault('interpolation', 'LINEAR')
    settings.setdefault('suffix', '_baked')
    settings.setdefault('overwrite', True)
    settings.setdefault('bake_mode', 'PURE')
    settings['fake_user'] = True
    if args.suffix is not None:
        settings['suffix'] = args.suffix
    if args.step is not None:
        settings['frame_step'] = args.step
    if args.mode:
        settings['bake_mode'] = args.mode
    if args.interp:
        settings['interpolation'] = args.interp
    if args.no_overwrite:
        settings['overwrite'] = False
    if args.frame_range:
        try:
            a, b = args.frame_range.split(':')
            settings['frame_start'] = float(a)
            settings['frame_end'] = float(b)
        except ValueError:
            fail('--frame-range 格式应为 A:B')
    return settings


# ---------------------------------------------------------------------------

def cmd_list(args):
    open_blend(args.blend)
    arms = []
    for o in bpy.data.objects:
        if o.type != 'ARMATURE':
            continue
        arms.append({
            'name': o.name, 'bones': len(o.data.bones),
            'action': o.animation_data.action.name
            if o.animation_data and o.animation_data.action else None,
            'has_embedded_spec': spec_from_idprops(o.data) is not None,
        })
    actions = []
    for a in bpy.data.actions:
        fr = a.frame_range
        actions.append({
            'name': a.name, 'range': [fr[0], fr[1]],
            'generated': bool(a.get(core.GENERATED_TAG)),
            'source': a.get(core.SOURCE_TAG, None),
        })
    emit('list', armatures=arms, actions=actions)


def cmd_validate(args):
    open_blend(args.blend)
    spec = resolve_spec(args)
    src, dest, spec = resolve_rigs(args, spec)
    src_skel = core.snapshot_skeleton(src)
    dest_skel = core.snapshot_skeleton(dest)
    mappings, warnings = rm.build_mappings(src_skel, dest_skel,
                                           spec.get('mappings', []))
    bakeable = core.list_bakeable_actions(src)
    src_constrained = [pb.name for pb in src.pose.bones if pb.constraints]
    emit('validate', source=src.name, dest=dest.name,
         valid_mappings=len(mappings), warnings=warnings,
         bakeable_actions=[a.name for a in bakeable],
         source_bones_with_constraints=src_constrained,
         hint='来源骨架带约束时建议 --mode SCENE' if src_constrained else '')
    if warnings:
        sys.exit(2)


def import_fbx_sources(arg):
    """导入动画 FBX, 返回 [(导入骨架对象, [其动作])]; 调用方负责清理。"""
    if os.path.isdir(arg):
        files = sorted(os.path.join(arg, f) for f in os.listdir(arg)
                       if f.lower().endswith('.fbx'))
    else:
        files = [f for f in arg.split(';') if f]
    out = []
    for path in files:
        if not os.path.isfile(path):
            emit('warning', message='FBX 不存在: %s' % path)
            continue
        new_objects, new_actions = core.import_file_datablocks(path)
        arm = next((o for o in new_objects if o.type == 'ARMATURE'), None)
        if arm is None:
            emit('warning', message='FBX 中没有骨架: %s' % path)
            for o in new_objects:
                bpy.data.objects.remove(o, do_unlink=True)
            continue
        core.name_actions_after_file(new_actions, path)
        out.append((arm, new_actions, new_objects))
        emit('imported', fbx=path, armature=arm.name,
             actions=[a.name for a in new_actions])
    return out


def ensure_fbx_addon():
    try:
        import addon_utils
        addon_utils.enable('io_scene_fbx', default_set=False)
    except Exception:
        pass
    return hasattr(bpy.ops.export_scene, 'fbx')


def export_fbx(dest, dest_actions, args, settings):
    if not ensure_fbx_addon():
        fail('FBX 导出器不可用')
    out_dir = os.path.abspath(args.export_fbx)
    os.makedirs(out_dir, exist_ok=True)
    fbx_kwargs = dict(
        use_selection=True,
        object_types={'ARMATURE', 'MESH'} if args.with_meshes
        else {'ARMATURE'},
        add_leaf_bones=False,
        apply_unit_scale=True,
        apply_scale_options='FBX_SCALE_ALL',
        armature_nodetype='NULL',
        bake_anim=True,
        bake_anim_use_all_bones=True,
        bake_anim_use_all_actions=False,
        bake_anim_simplify_factor=0.0,
    )
    if args.fbx_args:
        try:
            fbx_kwargs.update(json.loads(args.fbx_args))
        except Exception as e:
            fail('--fbx-args 不是合法 JSON: %s' % e)

    # 选择导出对象
    for o in bpy.data.objects:
        o.select_set(False)
    dest.select_set(True)
    bpy.context.view_layer.objects.active = dest
    if args.with_meshes:
        for child in dest.children_recursive:
            if child.type == 'MESH':
                child.select_set(True)

    scene = bpy.context.scene
    suffix = settings.get('suffix', '_baked')
    if args.single:
        core.push_to_nla(dest, dest_actions)
        core.assign_action(dest, None)
        path = os.path.join(out_dir, args.single if args.single.lower()
                            .endswith('.fbx') else args.single + '.fbx')
        starts = [a.frame_range[0] for a in dest_actions]
        ends = [a.frame_range[1] for a in dest_actions]
        scene.frame_start = int(min(starts)) if starts else 1
        scene.frame_end = int(max(ends)) if ends else 1
        kw = dict(fbx_kwargs)
        kw['bake_anim_use_nla_strips'] = True
        bpy.ops.export_scene.fbx(filepath=path, **kw)
        emit('exported', fbx=path, takes=len(dest_actions))
    else:
        for act in dest_actions:
            core.assign_action(dest, act)
            fr = act.frame_range
            scene.frame_start = int(fr[0])
            scene.frame_end = int(fr[1])
            name = act.name
            if suffix and name.endswith(suffix):
                name = name[: -len(suffix)]
            path = os.path.join(out_dir, name + '.fbx')
            kw = dict(fbx_kwargs)
            kw['bake_anim_use_nla_strips'] = False
            bpy.ops.export_scene.fbx(filepath=path, **kw)
            emit('exported', fbx=path, action=act.name)


def cmd_bake(args):
    open_blend(args.blend)
    spec = resolve_spec(args)
    src, dest, spec = resolve_rigs(args, spec)
    settings = build_settings(args, spec)
    emit('begin', source=src.name, dest=dest.name,
         mode=settings['bake_mode'], suffix=settings['suffix'])

    imported = []
    if args.import_fbx:
        imported = import_fbx_sources(args.import_fbx)

    # 收集待烘动作
    jobs = []   # (src_obj, action)
    if imported:
        for arm, acts, _objs in imported:
            jobs += [(arm, a) for a in acts]
    actions = core.list_bakeable_actions(src)
    if args.actions:
        patterns = [p.strip() for p in args.actions.split(',') if p.strip()]
        actions = [a for a in actions
                   if any(fnmatch.fnmatch(a.name, p) for p in patterns)]
    jobs += [(src, a) for a in actions]
    if not jobs:
        fail('没有可烘焙的动作')

    results, errors = [], []
    for i, (job_src, act) in enumerate(jobs):
        emit('progress', index=i, total=len(jobs), action=act.name)
        try:
            dest_action, info = core.bake_action(job_src, dest, spec, act,
                                                 settings)
            results.append(dest_action)
            emit('baked', action=act.name, output=dest_action.name,
                 frames=info['frames'], bones=info['bones'],
                 warnings=info['warnings'])
        except Exception as e:
            errors.append((act.name, str(e)))
            emit('bake_failed', action=act.name, error=str(e))

    # 清理导入的临时来源
    for arm, acts, objs in imported:
        for o in objs:
            bpy.data.objects.remove(o, do_unlink=True)
        for a in acts:
            try:
                bpy.data.actions.remove(a)
            except Exception:
                pass

    if args.export_fbx and results:
        export_fbx(dest, results, args, settings)

    if args.save_blend:
        path = os.path.abspath(args.save_blend)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=path)
        emit('saved_blend', path=path)

    emit('done', baked=len(results), failed=len(errors))
    if errors:
        sys.exit(1)


def main():
    args = parse_args(sys.argv[1:])
    {'list': cmd_list, 'validate': cmd_validate, 'bake': cmd_bake}[args.cmd](args)


if __name__ == '__main__':
    main()
