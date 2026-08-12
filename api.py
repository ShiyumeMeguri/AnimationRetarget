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

"""供其他插件程序化调用的稳定入口: 全部转发到既有能力, 不含重定向数学。"""

import os

from . import core
from . import presets
from . import skeleton_rename


def retarget_actions(source_object, dest_object, mappings, settings=None,
                     actions=None):
    return core.bake_all(source_object, dest_object, {'mappings': mappings},
                         settings, actions)


def load_mapping(name_or_path):
    is_path = (os.path.isabs(name_or_path)
               or os.sep in name_or_path
               or (os.altsep is not None and os.altsep in name_or_path)
               or name_or_path.lower().endswith('.json'))
    if is_path:
        spec = presets.load_preset_file(name_or_path)
    else:
        spec = presets.load_preset(name_or_path)
    return presets.normalize_pairs(spec)


def mapping_dir():
    return presets.preset_dir()


def export_skeleton_structure(armature_object, out_path=None):
    return skeleton_rename.write_skeleton_export(armature_object, out_path)
