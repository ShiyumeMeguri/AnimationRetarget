# Animation Retarget (纯数学重定向) v2.0

把动画从一个骨架重定向到另一个骨架。**零约束、零驱动、零修改器** ——
全部效果由纯数学直接计算并直写关键帧, 因此:

| 旧版痛点 | 新版 |
|---|---|
| 批量烘焙越烘越歪 (顺序漂移) | 每条动作是独立纯函数, **逐位确定**, 单烘 == 批量烘 (自检逐位断言) |
| root 骨骼位移烘不出来, 始终在原点 | 不走 `nla.bake`/骨骼选择/可见性, 直写 FCurve; **物体级根运动也会折叠进根骨曲线** |
| 约束常驻骨架, 状态残留 | 从不创建约束。预览=会话级数学驱动, 关闭即逐位复原 |
| 预设无法覆盖保存 | JSON 预设, **"保存"按钮直接覆盖当前预设**; 另存为/删除/导入导出 |
| 必须开 Blender 才能烘 | CLI 无头批量烘焙 + FBX 导出 (Unity), 甚至可用 pip 版 bpy 完全脱离 Blender 安装 |

## 算法 (只取顶级)

- **旋转**: `R_dest_world(t) = R_src_world(t) @ Q`, `Q = rot(src_rest_w)⁻¹ @ rot(dest_ref_w) @ R_manual`。
  统一了"世界旋转复制+精确偏移"(kumopult) 与 "rest-delta 相似变换"(Mwni) —— 二者数学等价。
  `dest_ref` 默认取 rest, 可"捕捉对齐姿态"支持 A-pose ↔ T-pose。
- **位移**: 世界空间传递 + 轴掩码 + 髋高比自动缩放 (体型不同的根运动)。
- **IK**: 解析双骨 IK (保持弯曲平面) / 单关节瞄准 / 阻尼 CCD, `链长` 与旧版约束 chain_count 同义。
- **FK 求值**: 逐行移植 Blender 内核 `BKE_bone_parent_transform_calc_from_matrices`
  (hinge + 全部 6 种 inherit_scale), 与 depsgraph 偏差在 float32 量化极限内。
- **采样**: 默认 `纯数学` 模式直接评估 FCurve (烘焙结果与时间轴/场景状态完全无关);
  源骨架自带约束/驱动时切 `场景评估` 模式 (同样无残留: 评估前归零、评估后还原)。

## Blender 内使用

侧栏 `AnimRetarget` 面板, 工作流与旧版一致:
选目标骨架与动画来源 → 编辑映射表 (映射/旋转/位移/IK 四页) → 预览 → 烘焙/批量烘焙。

新增: 智能猜测映射 (名称+同义词+左右分组)、对齐姿态捕捉、设置菜单里的
`产物推送NLA` / `导入·导出 JSON 配置`。

产物命名 = `源动作名 + 后缀` (默认 `_baked`), 同名**原地覆盖更新** (保持 NLA 引用), 并打上
`animret_generated` 标签 (批量烘焙永不会把产物当输入)。

## 骨架转换 (重命名) — 独立于映射的额外功能

侧栏同一 `AnimRetarget` 分类下另有一个独立面板 **"骨架转换 (重命名)"** (默认折叠,
对应 `skeleton_rename.py` / `ops_skeleton_rename.py` / `ui_skeleton_rename.py`)。
用途: 把一个骨架的骨骼名整体改成另一套命名规范 (例如把外部/游戏导入骨架的命名
对齐到项目自己的骨架命名), 交给 AI 读结构、代为识别与重命名。与上面的映射
预设是两套完全独立的数据、独立的目录、独立的 JSON 格式。

工作流:

1. **同时选中来源骨架与目标骨架**, 点击面板 ① 里的**导出骨架结构** →
   选中几个就导出几份, 各自固定路径原地覆盖 (不留旧档, 不用每次手动另存):
   `<Blender 用户脚本目录>/presets/AnimationRetarget/SkeletonConfig/<骨架名>.json`
   本机 (RuriConfig 自定义脚本路径) 的实际路径:
   `D:\Ruri\00.Model\Tools\BlenderProfile\RuriConfig\scripts\presets\AnimationRetarget\SkeletonConfig\`
2. 把这两份 json 交给 AI: "对比这两个骨架, 把 A 的骨骼名改成 B 的命名规范"。
   两边骨骼名往往毫无规律可循 (不同游戏/不同管线各写各的), **没法程序化硬编码**,
   要的就是 AI 拿层级 + 世界空间头尾 + `use_deform` + 蒙皮命中这些结构信息
   肉眼对比、逐根认语义。AI 直接写出重命名表 json 存进:
   `<Blender 用户脚本目录>/presets/AnimationRetarget/SkeletonRename/<名字>.json`
   本机实际路径:
   `D:\Ruri\00.Model\Tools\BlenderProfile\RuriConfig\scripts\presets\AnimationRetarget\SkeletonRename\`
3. 回 Blender, **选中要改名的那个骨架** (面板 ② 里的"改名对象"会显示是谁),
   从预设菜单按 `display_name` 选中 AI 刚写的那份, 点击**应用重命名**。
   AI 改完文件后点一下刷新按钮即可重新读取, 不用重启。

面板分成 ①② 两块, 因为两步的作用对象不同: ① 作用于**选中的全部骨架**
(要两个一起导才能对比), ② 只作用于**活动骨架**那一个 (改名是破坏性操作,
不能一次改两个骨架)。两处都把当前作用对象直接写在按钮上方。

重命名表由 AI 直接写文件、面板只负责读和应用, 所以不提供"导入/导出 JSON"
按钮 —— 预设菜单读的就是那个目录, 再加一套文件对话框只是同一件事的第二条路径。

重命名只做骨骼名本身的两阶段防冲突改名 (先集体改成临时名再改成最终名, 因此
A↔B 互换名之类场景也不会互相冲突)。顶点组名 / FCurve 路径 / 同骨架内约束
subtarget 由 **Blender 引擎自身**的 `Bone.name` setter 联动维护 (逐条属性
赋值即触发, 不限于走 UI 重命名操作符, 已实测确认) —— 蒙皮与已有动画不会因
改名而失效, 插件不重复手搓这块。

骨架结构导出 JSON:

```json
{ "format": "AnimationRetargetSkeleton", "version": 1,
  "armature": "EndField_Si", "bone_count": 87,
  "bones": [
    {"name": "Root", "parent": null, "children": ["Hips"], "depth": 0,
     "head_world": [0.0, 0.0, 0.0], "tail_world": [0.0, 0.1, 0.0],
     "length_world": 0.1, "use_connect": false, "use_deform": true,
     "deform_meshes": ["Body"]}
  ]}
```

重命名预设 JSON:

```json
{ "format": "AnimationRetargetSkeletonRename", "version": 1,
  "display_name": "EndField_Si → Ruri", "source_armature": "EndField_Si",
  "renames": [{"from": "J_Bip_C_Hips", "to": "Hips"}] }
```

## CLI 无头烘焙 (AI 直接可调)

两种运行方式任选:

```powershell
# A. 用 Blender (后台, 不开界面)
blender -b 场景.blend --python "<本目录>\cli.py" -- bake --preset 映射.json --export-fbx D:\out --per-action

# B. 完全不装 Blender: pip install bpy 后用系统 Python
python "<本目录>\cli.py" bake --blend 场景.blend --preset 映射.json --export-fbx D:\out --per-action
```

子命令:

```text
list                       列出骨架/动作/是否有嵌入配置 (JSON 输出)
validate                   校验映射与骨架匹配 (含"源骨架带约束建议 SCENE 模式"提示)
bake                       批量重定向烘焙
  --blend X.blend          要打开的文件 (已在 Blender 内则省略)
  --preset map.json        JSON 配置 (文件路径或用户预设名; 省略时读目标骨架嵌入配置)
  --source/--dest 名字     骨架对象名 (可省略: 按映射骨名自动识别)
  --actions "Walk*,Run"    动作通配符 (默认: 全部能作用于来源骨架的动作)
  --mode PURE|SCENE        采样模式   --step N  帧步长   --frame-range A:B
  --suffix _baked          产物后缀   --no-overwrite
  --import-fbx 目录或a;b   先批量导入动画 FBX 作为来源 (烘完自动清理)
  --export-fbx 目录        导出 FBX:  --per-action 每动作一个 / --single 名字.fbx 合并多take(NLA)
  --with-meshes            连同子网格导出   --fbx-args "{...}" 透传导出器参数
  --save-blend out.blend   烘焙后另存
```

所有进度输出 `@ANIMRET {json}` 行, 程序化解析即可。退出码非 0 = 有失败。

JSON 配置即面板里的预设文件 (设置菜单可导出), 结构:

```json
{ "format": "AnimationRetarget", "version": 2,
  "source_armature": "SrcRig", "dest_armature": "DstRig",
  "settings": {"frame_step": 1.0, "suffix": "_baked", "bake_mode": "PURE"},
  "mappings": [
    {"source": "hips", "dest": "Hips",
     "rot": {"auto": true, "ortho": false, "offset": [0,0,0], "align": null},
     "loc": {"enabled": true, "axes": [true,true,true], "scale_mode": "AUTO", "scale": 1.0},
     "ik":  {"enabled": false, "influence": 1.0, "chain": 2}}
  ]}
```

## 自检

```powershell
python tests\headless_selftest.py          # pip bpy
blender -b --factory-startup --python tests\headless_selftest.py
```

FK 忠实度(vs depsgraph) / 约束式结果数值等价 / 自重定向恒等 / IK 可达 /
批量逐位确定性 / 单烘==批量 / 根位移非常数 / 隐藏骨照烘 / 零约束零残留 /
JSON 预设覆盖保存与往返 / 操作符 / CLI 全链路 + FBX / 骨架结构导出往返 /
骨架重命名两阶段防冲突(含互换名)+ 引擎联动顶点组/FCurve/约束校验。
