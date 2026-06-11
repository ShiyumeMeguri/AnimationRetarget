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

bl_info = {
    "name": "Animation Retarget",
    "author": "ShiyumeMeguri",
    "description": "纯数学动画重定向: 零约束零驱动, 直写关键帧烘焙, "
                   "支持 CLI 无头批量烘焙输出到 Unity",
    "blender": (3, 3, 0),
    "version": (2, 0, 0),
    "location": "View 3D > Sidebar > AnimRetarget",
    "category": "Animation",
    "doc_url": "",
    "tracker_url": "",
}

import bpy

from . import retarget_math
from . import core
from . import state
from . import presets
from . import ops
from . import ui

modules = (state, ops, ui)


def register():
    from importlib import reload
    # 开发期热重载支持
    for mod in (retarget_math, core, presets):
        reload(mod)
    for mod in modules:
        for cls in mod.classes:
            bpy.utils.register_class(cls)
    bpy.types.Scene.animret_dest = bpy.props.PointerProperty(
        name='目标骨架', type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE')
    bpy.types.Armature.animret = bpy.props.PointerProperty(
        type=state.AnimRetState, override={'LIBRARY_OVERRIDABLE'})
    core.register_handlers()


def unregister():
    core.preview_stop_all()
    core.unregister_handlers()
    for mod in reversed(modules):
        for cls in reversed(mod.classes):
            bpy.utils.unregister_class(cls)
    del bpy.types.Scene.animret_dest
    del bpy.types.Armature.animret
