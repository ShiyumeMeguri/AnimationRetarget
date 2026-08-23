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

"""配置目录 = 一张骨架的有向图, 能走通的每一对都是一份可用的配置。

一份配置说的是两件事, 别的什么都不说:

    这张表连接**哪两副骨架**   source / dest, 各是 ``{family, config}``
    以及**怎么连**             mappings: 骨名对 + 每行的偏移等固有设置

骨架的身份是 **家族 + 配置**: 家族是这副骨架的出处 (一家公司换了个招牌不算两个家族),
配置是同一家族下的体型分支 (Girl / Boy)。两者都只是名字, **插件代码里不出现任何一个
具体家族**。

家族连同它的别名 (哪些游戏用这副骨架) 就写在**用到它的那份表自己里**:

    "source": {"family": "Illusion", "config": "Girl",
               "aliases": ["ILLGames", "Koikatu", "HoneyCome"]}

所以目录里只有 ``XXToXX.json``, 没有第二种文件、没有需要先读一遍的总表 —— 一份表拿走
就能单独用, 加一个游戏就是在提到那个家族的表里加个别名。同一个家族在多份表里各写各的
别名时**取并集**: 那是同一个家族在被从不同角度描述, 不是两个家族。

有了 A→B 和 B→C 两份文件, A→C 就**已经存在了**: 按中间骨架的骨名把两张表 join 起来
即可。所以浏览器列的不是"目录里有哪些文件", 而是**这些文件张成的全部转换**, 直连的和
拼出来的一视同仁。拼出来的那份是完整的 spec, 可以照常改偏移、照常另存 —— 存下来就是
图上的一条新边, 下一次枚举时它自己又能当跳板。

方向: 骨映射是双射, 一张表反着读就是反向的表 (``flip_spec``), 所以一条边两个方向都能走。

**拼接与翻转只带走本模块认识的东西** (mappings / settings / source / dest)。文件里别的
段落是别人写的、而且是**针对文件所声明的那一对**说的话 —— 换了一对就不再成立。原样直连
(单跳、不翻转) 时返回的就是读进来的那份 spec 本身, 所以那些段落一个不少。
"""

import collections
import json
import os

from . import presets

# 一条链最多几跳。图很小 (配置是手写的), 但组合数会爆, 而且跳得越多中间 join 掉的骨
# 越多 —— 四跳还找不到的两副骨架, 更像是缺一张表, 而不是缺一次搜索。
MAX_HOPS = 4

Hop = collections.namedtuple('Hop', 'name flipped')


def fold(name):
    """比对用的折叠键。**只用来比对, 从不用来显示或落盘** —— 名字一律按声明时的写法
    (Illusion / ILLGames / HoneyCome) 原样保留, 大小写不敏感只是匹配策略。"""
    return str(name or '').strip().lower()


class Side:
    """一副骨架的身份: 家族 + 配置, 外加这个家族的别名。

    **身份只是 家族+配置**: 别名是同一个家族的其它叫法 (共用这副骨架的游戏), 参与
    "这个名字指的是谁"的解析, 不参与"这是不是同一副骨架"的判断 —— 否则两份表把别名
    写得不一样就会变成两副骨架。"""

    __slots__ = ('family', 'config', 'aliases')

    def __init__(self, family, config, aliases=()):
        self.family = str(family or '').strip()
        self.config = str(config or '').strip()
        seen, names = set(), []
        for alias in aliases or ():
            text = str(alias).strip()
            if text and fold(text) not in seen and fold(text) != fold(self.family):
                seen.add(fold(text))
                names.append(text)
        self.aliases = tuple(names)

    @property
    def key(self):
        return '{0}.{1}'.format(self.family, self.config)

    @property
    def fold_key(self):
        return '{0}.{1}'.format(fold(self.family), fold(self.config))

    def valid(self):
        return bool(self.family and self.config)

    def __eq__(self, other):
        return isinstance(other, Side) and self.fold_key == other.fold_key

    def __hash__(self):
        return hash(self.fold_key)

    def __repr__(self):
        return '<Side {0}>'.format(self.key)


def side_of(entry):
    """spec 里的 ``{"family", "config", "aliases"}`` → Side。"""
    entry = entry or {}
    return Side(entry.get('family'), entry.get('config'), entry.get('aliases'))


def spec_sides(spec):
    """``(源 Side, 目标 Side)``。两侧都完整才算图上的一条边。"""
    spec = spec or {}
    return side_of(spec.get('source')), side_of(spec.get('dest'))


# ── 家族 ──────────────────────────────────────────────────────────────────────

def family_aliases(edges=None):
    """``{家族名: [别名]}`` —— 把每一份表各自声明的别名并起来。

    并集而不是"以某一份为准": 同一个家族出现在好几份表里, 每份只从自己的角度提到它,
    谁也不是权威。这也是加一个游戏只需要动一份表的原因。"""
    found = {}
    for _name, _spec, source, dest in (load_edges() if edges is None else edges):
        for side in (source, dest):
            key = fold(side.family)
            if not key:
                continue
            family, names = found.setdefault(key, (side.family, []))
            for alias in side.aliases:
                if fold(alias) not in {fold(n) for n in names}:
                    names.append(alias)
    return {family: names for family, names in found.values()}


def family_of(name, edges=None):
    """这个名字属于哪个家族 —— 家族名本身, 或它的某个别名 (游戏 productName)。
    认不出来就原样返回, 让调用方拿着它去报"没有这个家族"。"""
    needle = fold(name)
    for family, aliases in family_aliases(edges).items():
        if needle == fold(family) or any(needle == fold(alias) for alias in aliases):
            return family
    return str(name or '').strip()


# ── 图 ────────────────────────────────────────────────────────────────────────

class Route:
    """从一副骨架到另一副骨架的一条通路。``hops`` 为空表示两端本来就是同一副。"""

    __slots__ = ('source', 'dest', 'hops')

    def __init__(self, source, dest, hops):
        self.source = source
        self.dest = dest
        self.hops = tuple(hops)

    @property
    def direct(self):
        """盘上**真有一份文件正是这一对、正是这个方向**。

        一跳还不够: 反着读的那一跳同样只有一跳, 但那个方向并没有文件 —— 它和拼出来的
        一样是临时的, 存下来会新建一份。把两者都算"直连", 面板上就会看不出哪些是现成
        的、哪些是当场算出来的 (四份表铺满之后, 每一对都成了"一跳", 区分就整个消失了)。"""
        return len(self.hops) == 1 and not self.hops[0].flipped

    @property
    def stored_name(self):
        """这一对在盘上的那份文件名, 没有就是空 —— "保存"能不能直接覆盖看它。"""
        return self.hops[0].name if self.direct else ''

    @property
    def key(self):
        """存进 UI 属性、再拿回来查同一条路的字符串。"""
        return '{0}>{1}'.format(self.source.key, self.dest.key)

    @property
    def via(self):
        return ' + '.join(('~' if hop.flipped else '') + hop.name for hop in self.hops)

    def default_name(self, edges=None):
        return default_name(self.source, self.dest, edges)

    def label(self, count=None):
        arrow = '{0} {1} → {2} {3}'.format(self.source.family, self.source.config,
                                           self.dest.family, self.dest.config)
        if count is not None:
            arrow = '{0}  ({1})'.format(arrow, count)
        return arrow if self.direct else '{0}  [{1}]'.format(arrow, self.via)

    def __repr__(self):
        return '<Route {0} via {1}>'.format(self.key, self.via or 'nothing')


def default_name(source, dest, edges=None):
    """一对骨架在盘上叫什么: **家族To家族**。另存为拿它当默认值 —— 名字是从这一对推出
    来的, 不该让用户自己想一个。

    只在**家族名不足以区分**时才补上配置, 那有两种情形, 都会撞同一个文件名:
    同一家族内部的转换 (Ruri Girl → Ruri Boy), 以及同一对家族下已经存在另一个配置对
    (Endfield Girl → Ruri Girl 之外还有 Boy → Boy)。撞名不补配置的后果是后存的那份
    悄悄盖掉先存的。"""
    if not (source and dest and source.valid() and dest.valid()):
        return ''
    long_name = '{0}{1}To{2}{3}'.format(source.family, source.config,
                                        dest.family, dest.config)
    if fold(source.family) == fold(dest.family):
        return '{0}{1}To{2}'.format(source.family, source.config, dest.config)
    # 方向不敏感: 反着读同一张表还是同一对家族, 所以 Ruri→Endfield 也必须看见
    # Endfield→Ruri 那边已经有几个配置对。
    # 看的是**通路**而不是文件: 撞名撞的是"这一对家族下有几种配置组合", 而其中一种
    # 完全可能是拼出来的、还没有文件。只数文件就会让那一对里先存下来的那份占掉短名字。
    pair = {fold(source.family), fold(dest.family)}
    configs = set()
    for route in routes(edges):
        if {fold(route.source.family), fold(route.dest.family)} == pair:
            configs.add((fold(route.source.config), fold(route.dest.config)))
    if len(configs) > 1:
        return long_name
    return '{0}To{1}'.format(source.family, dest.family)


def load_edges():
    """``[(配置名, spec, 源 Side, 目标 Side)]`` —— 目录里每一份两侧都声明齐、且真的有
    映射行的配置。声明不全的不是图上的边 (但仍是一份能直接加载的文件)。"""
    edges = []
    for name in sorted(presets.list_presets()):
        try:
            spec = presets.load_preset(name)
        except Exception:
            continue
        source, dest = spec_sides(spec)
        if source.valid() and dest.valid() and presets.normalize_pairs(spec):
            edges.append((name, spec, source, dest))
    return edges


def sides(edges=None):
    """图上出现过的每一副骨架, 去重, 按 家族/配置 排序。"""
    found = {}
    for _name, _spec, source, dest in (load_edges() if edges is None else edges):
        for side in (source, dest):
            found.setdefault(side.fold_key, side)
    return [found[key] for key in sorted(found)]


def routes(edges=None, max_hops=MAX_HOPS):
    """图上能走通的**每一对骨架** → 最短的那条 Route。

    广度优先, 每条边两个方向都可走, 同长度时按配置名先后取定 —— 同样的目录永远给出
    同样的答案, 不会因为字典顺序换个 Blender 版本就变。"""
    edges = load_edges() if edges is None else edges
    by_key = {side.fold_key: side for side in sides(edges)}
    found = {}
    for start_key in sorted(by_key):
        seen = {start_key}
        frontier = collections.deque([(start_key, [])])
        while frontier:
            position, hops = frontier.popleft()
            if len(hops) >= max_hops:
                continue
            for name, _spec, source, dest in edges:
                for landing, flipped in ((dest, False), (source, True)):
                    here = (source if not flipped else dest).fold_key
                    if position != here or landing.fold_key in seen:
                        continue
                    seen.add(landing.fold_key)
                    route = Route(by_key[start_key], landing, hops + [Hop(name, flipped)])
                    found.setdefault(route.key, route)
                    frontier.append((landing.fold_key, list(route.hops)))
    return [found[key] for key in sorted(found)]


def resolve_side(name, edges=None):
    """把用户/宿主说的一个名字认成图上的一副骨架。

    接受 ``Family.Config``、光一个家族名、或一个别名 (游戏 productName)。只给家族而
    该家族在图上只有一个配置时用那个; 有好几个就**不猜** —— 返回 None, 由调用方说
    "得指明是哪个配置", 猜出来的答案会把动画套到另一种体型上。"""
    edges = load_edges() if edges is None else edges
    text = str(name or '').strip()
    if not text:
        return None
    known = sides(edges)
    if '.' in text:
        wanted = Side(*text.split('.', 1))
        wanted = Side(family_of(wanted.family, edges), wanted.config)
        return next((side for side in known if side == wanted), None)
    family = family_of(text, edges)
    matches = [side for side in known if fold(side.family) == fold(family)]
    return matches[0] if len(matches) == 1 else None


def route_for(source, dest, edges=None):
    """A→B 的那一条, 或 None。两端同一副骨架时返回一条空 Route (不需要转换)。"""
    edges = load_edges() if edges is None else edges
    start = resolve_side(source, edges)
    finish = resolve_side(dest, edges)
    if start is None or finish is None:
        return None
    if start == finish:
        return Route(start, finish, [])
    for route in routes(edges):
        if route.source == start and route.dest == finish:
            return route
    return None


# ── 拼接 ──────────────────────────────────────────────────────────────────────

def flip_spec(spec):
    """反向的表: 骨映射是双射, 交换两端即可。见模块头 —— 只带走本模块认识的段落。"""
    source, dest = spec_sides(spec)
    return presets.make_spec(
        [dict(row, source=str(row.get('dest') or ''), dest=str(row.get('source') or ''))
         for row in presets.normalize_pairs_rows(spec)],
        source=dest, dest=source,
        settings=dict(spec.get('settings') or {}))


def compose_specs(specs):
    """(a→m) 与 (m→d) 按中间骨架的骨名 join 成 (a→d)。

    每一行带的是**最后一跳**的逐行参数, 设置也取最后一跳的 —— 一行的偏移说的是"目标骨
    要额外转多少", 那是最后一跳才回答得了的问题。中间骨架上接不上的行直接消失, 这正是
    链越长覆盖越窄的原因, 也是它该有的样子。"""
    spec = specs[0]
    for hop_spec in specs[1:]:
        join = {}
        for row in presets.normalize_pairs_rows(hop_spec):
            join.setdefault(str(row.get('source') or ''), row)
        rows = []
        for row in presets.normalize_pairs_rows(spec):
            hop = join.get(str(row.get('dest') or ''))
            if hop is not None:
                rows.append(dict(hop, source=str(row.get('source') or ''),
                                 dest=str(hop.get('dest') or '')))
        source, _mid = spec_sides(spec)
        _mid2, dest = spec_sides(hop_spec)
        spec = presets.make_spec(rows, source=source, dest=dest,
                                 settings=dict(hop_spec.get('settings') or {}))
    return spec


def compose(route, edges=None):
    """这条 Route 的完整 spec。

    单跳且不翻转 = 那份文件本身, 原样返回, 所以它携带的一切 (包括本模块看不懂的段落)
    都在。其余情况是新拼出来的一对, 只带得动本模块认识的东西。"""
    if route is None or not route.hops:
        return {}
    by_name = {name: spec for name, spec, _s, _d in (load_edges() if edges is None else edges)}
    chain = []
    for hop in route.hops:
        spec = by_name.get(hop.name)
        if spec is None:
            return {}
        chain.append(flip_spec(spec) if hop.flipped else spec)
    if len(chain) == 1:
        return chain[0]
    return compose_specs(chain)
