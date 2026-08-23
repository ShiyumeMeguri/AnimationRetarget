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

"""配置目录 = 一张骨架家族的有向图, 能走通的每一对都是一份可用的配置。

一份预设声明它桥接哪两个家族 (``skeletons.source`` / ``skeletons.dest``, 各是别名
列表 —— 同一副骨架的姊妹作共用一张表)。有了 A→B 和 B→C 两份文件, A→C 就**已经存在
了**: 按中间骨架的骨名把两张表 join 起来即可。所以浏览器列的不是"目录里有哪些文件",
而是**这些文件张成的全部转换**, 直连的和拼出来的一视同仁。

拼出来的那份是完整的 spec, 可以照常改 offset、照常另存 —— 存下来就成为一份独立的
A→C 文件, 也就是图上的一条新边, 下一次枚举时它自己又能当跳板。

方向: 骨映射是双射, 一张表反着读就是反向的表 (``flip_spec``), 所以一条边两个方向
都能走。

**拼接与翻转只带走本模块认识的东西** (mappings / settings / skeletons)。文件里别的
段落是别人写的、而且是**针对文件所声明的那一对**说的话 —— 换了一对就不再成立, 带过去
就是把一份别处的陈述安到它没同意的骨架上。原样直连 (单跳、不翻转) 时返回的就是读进来
的那份 spec 本身, 所以那些段落一个不少。
"""

import collections

from . import presets

# 一条链最多几跳。图很小 (预设是手写的), 但组合数会爆, 而且跳得越多中间 join 掉的
# 骨越多 —— 四跳还找不到的两副骨架, 更像是缺一张表, 而不是缺一次搜索。
MAX_HOPS = 4

Hop = collections.namedtuple('Hop', 'name flipped')


class Route:
    """从一副骨架到另一副骨架的一条通路。``hops`` 为空表示两端本来就是同一副。

    两端是**别名组**而不是单个名字: 一张表的 source 侧写了几个名字, 就是在说这几个
    title 共用同一副骨架 —— 它们之间不需要任何转换。把它们当成图上的不同节点, 就会
    出现 honeycome → endfield → illusion 这种"绕出去再绕回来"的通路: 拼得通、行也剩着,
    但它在描述一次并不存在的转换。"""

    __slots__ = ('source', 'dest', 'hops', 'source_aliases', 'dest_aliases')

    def __init__(self, source, dest, hops, source_aliases=(), dest_aliases=()):
        self.source = source
        self.dest = dest
        self.hops = tuple(hops)
        self.source_aliases = tuple(source_aliases or (source,))
        self.dest_aliases = tuple(dest_aliases or (dest,))

    @property
    def direct(self):
        return len(self.hops) == 1

    @property
    def key(self):
        """存进 UI 属性、再拿回来查同一条路的字符串。"""
        return '{0}>{1}'.format(self.source, self.dest)

    @property
    def via(self):
        return ' + '.join(('~' if hop.flipped else '') + hop.name for hop in self.hops)

    @staticmethod
    def _side(names):
        names = list(names)
        return names[0] if len(names) == 1 else '{0} (+{1})'.format(names[0], len(names) - 1)

    def label(self, count=None):
        arrow = '{0} → {1}'.format(self._side(self.source_aliases),
                                   self._side(self.dest_aliases))
        if count is not None:
            arrow = '{0}  ({1})'.format(arrow, count)
        return arrow if self.direct else '{0}  [{1}]'.format(arrow, self.via)

    def __repr__(self):
        return '<Route {0} via {1}>'.format(self.key, self.via or 'nothing')


def fold(name):
    """比对用的折叠键。**只用来比对, 从不用来显示或落盘** —— 名字一律按声明时的写法
    (Illusion / ILLGames / HoneyCome) 原样保留, 大小写不敏感只是匹配策略, 不是存储格式。"""
    return str(name or '').strip().lower()


def spec_family_lists(spec):
    """``(源侧名字表, 目标侧名字表)``, **原样保留声明的写法与顺序**。

    顺序有意义: **第一个名字是这副骨架的家族名, 后面的是别名** —— 通常是共用这副骨架的
    具体游戏 (productName), 因为浏览器问的是"我现在在浏览 HoneyCome", 而配置答的是
    "那是 Illusion 那副骨架"。排序会把这层作者意图抹掉, 于是面板上就会拿一个游戏名
    当家族名显示。重复按折叠键去掉 (同一个名字写两遍大小写不同不是两个家族)。"""
    declared = (spec or {}).get('skeletons') or {}

    def clean(names):
        out, seen = [], set()
        for name in names or []:
            text = str(name).strip()
            key = fold(text)
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    return clean(declared.get('source')), clean(declared.get('dest'))


def spec_families(spec):
    """``(源侧折叠键集, 目标侧折叠键集)`` —— 比对用。要显示或落盘的名字用
    spec_family_lists, 那边是声明时的原样写法。"""
    source, dest = spec_family_lists(spec)
    return {fold(name) for name in source}, {fold(name) for name in dest}


def load_edges():
    """``[(预设名, spec, 源家族集, 目标家族集)]`` —— 目录里每一份声明了两侧家族、
    且真的有映射行的配置。声明不全的配置不是图上的边 (但仍是一份能直接加载的文件)。"""
    edges = []
    for name in sorted(presets.list_presets()):
        try:
            spec = presets.load_preset(name)
        except Exception:
            continue
        source, dest = spec_families(spec)
        if source and dest and presets.normalize_pairs(spec):
            edges.append((name, spec, source, dest))
    return edges


def families(edges=None):
    """声明过的每一个名字, **按声明的写法**, 大小写不敏感排序。"""
    found = {}
    for _name, spec, _source, _dest in (load_edges() if edges is None else edges):
        for side in spec_family_lists(spec):
            for name in side:
                found.setdefault(fold(name), name)
    return [found[key] for key in sorted(found)]


def alias_groups(edges=None):
    """``{名字: 这副骨架的家族名}``。

    同一张表同一侧列出的名字, 说的就是"这几个 title 是同一副骨架"——它们之间没有转换
    可言, 图上也该是同一个节点。

    组的名字取**声明里第一个**, 按配置名先后第一次出现的那份为准 (所以同一份目录永远
    给出同一个答案)。取字典序最小会把一个游戏名顶成家族名, 而那正是这层声明想避免的事。
    一个名字只在别人的别名表里露过面、自己从没打过头, 就用它自己 —— 总得有个答案。"""
    edges = load_edges() if edges is None else edges
    parent = {}

    def find(name):
        parent.setdefault(name, name)
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    declared_heads = []
    written = {}
    for _name, spec, _source, _dest in edges:
        for side in spec_family_lists(spec):
            if not side:
                continue
            for name in side:
                written.setdefault(fold(name), name)
            declared_heads.append(side[0])
            for other in side[1:]:
                union(fold(side[0]), fold(other))
    for name in families(edges):
        find(fold(name))

    head_of = {}
    for head in declared_heads:
        head_of.setdefault(find(fold(head)), head)
    return {key: head_of.get(find(key), written.get(key, key)) for key in parent}


def routes(edges=None, max_hops=MAX_HOPS):
    """图上能走通的**每一对骨架** → 最短的那条 Route。

    节点是别名组而不是名字 (见 alias_groups / Route), 所以同一副骨架的两个 title 之间
    不会冒出一条"绕出去再绕回来"的假通路。广度优先, 每条边两个方向都可走, 同长度时按
    预设名先后取定 —— 同样的目录永远给出同样的答案。"""
    edges = load_edges() if edges is None else edges
    groups = alias_groups(edges)                       # 折叠键 -> 家族名 (声明写法)
    spelling = {fold(name): name for name in families(edges)}
    members = {}
    for key, family in sorted(groups.items()):
        members.setdefault(family, []).append(spelling.get(key, key))
    # 家族名排在第一个: 面板显示的就是列表的头一项, 那必须是家族而不是碰巧字典序在前的
    # 某个游戏名。
    for family, names in members.items():
        members[family] = [family] + [n for n in names if fold(n) != fold(family)]

    def group_of(side):
        return {groups[key] for key in side if key in groups}

    found = {}
    for start in sorted(members):
        seen = {start}
        frontier = collections.deque([(start, [])])
        while frontier:
            position, hops = frontier.popleft()
            if len(hops) >= max_hops:
                continue
            for name, _spec, source, dest in edges:
                for landing, flipped in ((dest, False), (source, True)):
                    side = group_of(source if not flipped else dest)
                    if position not in side:
                        continue
                    for group in sorted(group_of(landing)):
                        if group in seen:
                            continue
                        seen.add(group)
                        route = Route(start, group, hops + [Hop(name, flipped)],
                                      members[start], members[group])
                        found.setdefault(route.key, route)
                        frontier.append((group, list(route.hops)))
    return [found[key] for key in sorted(found)]


def route_for(source, dest, edges=None):
    """A→B 的那一条, 或 None。

    两端按别名解析: 问 honeycome → waifu 和问 koikatu → waifu 是同一个问题, 因为那两个
    名字写在同一张表的同一侧。两端落在同一副骨架上时返回一条空 Route (不需要转换)。"""
    if not fold(source) or not fold(dest):
        return None
    edges = load_edges() if edges is None else edges
    groups = alias_groups(edges)
    source_group = groups.get(fold(source), str(source).strip())
    dest_group = groups.get(fold(dest), str(dest).strip())
    if fold(source_group) == fold(dest_group):
        return Route(str(source).strip(), str(dest).strip(), [])
    for route in routes(edges):
        if fold(route.source) == fold(source_group)                 and fold(route.dest) == fold(dest_group):
            return route
    return None


def flip_spec(spec):
    """反向的表: 骨映射是双射, 交换两端即可。见模块头 —— 只带走本模块认识的段落。"""
    source, dest = spec_family_lists(spec)
    return presets.make_spec(
        [dict(row, source=str(row.get('dest') or ''), dest=str(row.get('source') or ''))
         for row in presets.normalize_pairs_rows(spec)],
        display_name=_flip_name(spec.get('display_name') or ''),
        settings=dict(spec.get('settings') or {}),
        skeletons={'source': list(dest), 'dest': list(source)})


def _flip_name(name):
    if ' → ' in name:
        left, _sep, right = name.partition(' → ')
        return '{0} → {1}'.format(right, left)
    return name


def compose_specs(specs):
    """(a→m) 与 (m→d) 按中间骨架的骨名 join 成 (a→d)。

    每一行带的是**最后一跳**的逐行参数, 设置也取最后一跳的 —— 一行的 offset 说的是
    "目标骨要额外转多少", 那是最后一跳才回答得了的问题。中间骨架上接不上的行直接消失,
    这正是链越长覆盖越窄的原因, 也是它该有的样子。"""
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
        source, _mid = spec_family_lists(spec)
        _mid2, dest = spec_family_lists(hop_spec)
        spec = presets.make_spec(
            rows,
            display_name='{0} → {1}'.format(source[0] if source else '?',
                                            dest[0] if dest else '?'),
            settings=dict(hop_spec.get('settings') or {}),
            skeletons={'source': list(source), 'dest': list(dest)})
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
