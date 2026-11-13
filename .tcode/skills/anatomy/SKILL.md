---
name: anatomy
version: 1482e846-tcode-4
description: 为智能 UI 制作精细机械原理图、等轴测插图、设备结构图与拆解教学图。使用 Anatomy 的真实回转曲面、圆环和分面明暗，输出受控空间装配与可绑定中文部件说明的无脚本 SVG 参考；由 TCode 提供探索、拆解、动画、版本和研究包。不是通用网页设计或工程认证工具。
license: MIT
compatibility: TCode；render 动作需要沙箱中有 Node.js 20.10+；无需浏览器或网络。
metadata:
  routing:
    domains: [visual-design]
    intents: [detailed-diagram, isometric-illustration, assembly-explainer]
    aliases: [Anatomy, 精细原理图, 精美的原理图, 精美原理图, 机械原理图, 等轴测插图, 等轴测图, 设备结构图, 拆解教学图, 拆解图, 装配教学图, 三维轴承装配教学图, 精密轴承, 可交互的三维轴承]
    positive_examples:
      - 帮我做一张精细机械原理图，点击零件看中文说明
      - 生成等轴测插图，支持按顺序拆解和合拢
      - 画一个精密轴承装配教学图，保留三维旋转
    negative_examples:
      - 只用文字解释轴承原理，不要画图
      - 画一张销售柱状图
      - 查找相关论文
      - 修改登录页面的按钮样式
    excludes: [只用文字, 仅用文字, 不要画图, 不用画图, 销售柱状图, 登录页面]
    priority: 70
  runtime:
    capability: default
    timeout_seconds: 30
  actions:
    render:
      description: 从受控几何 JSON 生成 TCode SVG 与部件元数据，写入 JSON 文件；不会执行输入脚本。
      entrypoint: scripts/render.mjs
      interpreter: node
      arguments:
        scene:
          type: string
          required: true
        style:
          type: string
          choices: [clay, manual]
          default: clay
        output:
          type: path
          required: true
          artifact: true
---

# Anatomy · TCode 适配

按需使用，不要让普通问答、研究检索或数据图表都加载本技能。用户无需提 Anatomy 或平台工具名。

## 先选正确的产物

1. 调用 `canvas(action="style", request=用户的绘图请求)`，读取当前账户风格与图形建议。界面、文字、交互沿用平台设置；用户明确允许时插图材质可有独立配色。
2. **需要旋转的真实三维、动态遮挡、逐步装配、三维干涉检查，或修改已有三维图：使用 `canvas.compose/read/edit/spatial_check`。** 轴承逐步装配使用 `bore_fits:[{shaft_id,bore_id}]` 引用转轴与内圈，并将动画模式设为 `assemble`。为运动部件填写中文说明；装配倒序执行拆解顺序。参考仓库 `docs/IntelligentUI/examples/guided-bearing.json`。阅读本技能的设计规则改善部件搭配，不得将现有 `solid_scene` 改成纯 SVG。
3. **精细二维等轴测原理图同样必须保留空间装配：使用 `skill_run(skill="anatomy", action="render", arguments={scene:JSON字符串, style:当前风格, output:工作区JSON路径})`。** 使用平台回执中的 `output_path` 作为 `artifact_path`，配合 render 返回的 `artifact_digest` 调用 `canvas.check`，通过后以同一路径和摘要调用 `canvas.create` 实际展示；不要使用沙箱内部路径；无需读取或转写完整 JSON/SVG；不要只交付文件或代码块。render 产物中的 scene 是权威空间定义，平台校验后生成标准源与部件元数据；图解视图固定相机，以深度缓冲表达穿孔及遮挡，三维视图允许旋转，两者共用空间位置。style 由账户决定，不能在生成过程中改用户设置。
4. 输出来自受控参数，无任意 JS、HTML、CSS、网络或外链。没有 Node 或沙箱权限时明确说明此绘图路径暂不可用，改用平台受控部件库；不要安装依赖、放开脚本或宣称已经运行。

## 场景格式

完整可运行示例：[精密轴承二维图](examples/bearing.json)。render 读取 JSON 字符串，不执行模型代码。

- 顶层：`id,title,description,assemblies,animation?`。名称说明用用户能理解的中文；ID 仅供程序绑定。
- 1–12 个组件，每组 1–12 个零件，合计最多 48 个。组件：`id,label,description,explode:[dx,dy,dz],order:0..11,pieces`。
- 相机固定方位角45°、仰角30°，z向上；所有位置共用世界坐标。组件本身没有隐式位移。坐标±200，拆解方向±160。不得用屏幕坐标把独立零件贴成装配图。轴与孔的轴向、中心、插入深度必须相容；转轴穿过孔时应由表面深度决定哪段可见。
- 零件公共字段：`id,label,description,kind,position:[x,y,z],material`。材质 `alloy/polymer/rubber/accent`，可选 `color:"#rrggbb"`；保留材质明暗，避免大面积饱和色。
- `slab`：底座／支座，`size:[宽,深,高]`，每项0.5–160，position是最小角。
- `lathe`：轴／旋钮／回转壳体，`axis:"x"|"y"|"z",length,radius`；position是轴向起点。可选 `profile:[[轴向距离,半径],...]`，2–16点、距离递增，允许同距离的台阶。距离0–160，半径0–80。默认倒角轮廓要求length≥2。
- `ring`：空心轴承／套环，`axis,length,radius,inner_radius`，0<内半径<外半径。
- `sphere`：滚珠，`radius`。所有圆件半径0.5–80，长度0.5–160。
- 两组以上自动带 `animation:{duration_seconds:6,mode:"roundtrip"}`；可选时长1–60秒，模式 `explode/assemble/roundtrip`。这是共享空间组件的教学动画；图解固定视角，遮挡随位置更新，不是机械仿真。
- 稳定组件 g 与零件 g 嵌套绑定 parts，宿主提供点击、说明、高亮、聚焦、返回整体、拆解、时间轴与 GIF。不要再绘制假的按钮。一个组件时不生成时间轴；零件级未定义物理拆解路径。

## 绘图质量

先读 [craft.md](references/craft.md) 与 [kit.md](references/kit.md) 中与当前对象有关的部分，再设计真实结构：一个明确主体、接触面和共轴关系正确、每个细节都有用途。少用随机装饰方块；优先有台阶的回转轮廓、空心环、可解释的紧固件和滚珠。

轮廓、层次、明暗共同表达结构，选中后再显示长说明。默认48零件及SVG预算160KB；超限时减少采样/零件或拆成多张图，不要绕过平台限制。原版 `kit/iso-kit.mjs` 和 `kit/lathe.mjs` 保留为插图参考源；平台以受控 scene 标准化入库，回转轮廓、球体、空心环使用现有空间渲染器。不要丢弃 scene、只保存旧的叠层 SVG。导出的结构 SVG 是静态参考，不能等同于带深度遮挡的界面或 GIF。

生成后用 `canvas.check` 检查草稿，再 `canvas.create`，两步均传 `artifact_path` 和 `artifact_digest`，不要同时传 title/source/parts。检查名称、层级、裁切、遮挡、拆解方向与说明对应关系。布局检查不能证明对象真实或工程可用。无需浏览器截图即可完成离线结构检查；桌面测试只有用户允许且实际操作后才能记为通过。

## 交互、修改与交接

- 平台临时探索、播放和进度拖动不产生内容版本；保存改色、名称、说明与方向才产生版本。继续使用现有选区与 `canvas.edit`，不要为同一修改另建图解。
- 本适配器生成的 SVG 沿用平台的原始源/parts 保存、恢复、删除找回与研究包交接。收到研究包直接恢复产物，不依赖接收者重新安装 Node 才能看图。scene 与版本和快照一起保存。旧版仅 SVG 的图没有空间定义，需要明确重新生成，不能猜测坐标或覆盖旧历史。
- 重新生成改变结构需要遵循已有版本/选区协议，不应覆盖旧稿或移除三维数据。对已有三维图的二维特有编辑仍应被拒绝，而不是悄悄降级。
- 说清“教学示意，不代表工程验证”；上游几何审计也不是工程认证。本轮未接入上游 WebGL、社区发布、任意HTML运行或任意CAD碰撞规划。
- 最终回答用图解标题说明已展示的结果，不输出内部画布或版本编号。若附文件链接，必须使用平台实际返回的发布路径；不得将文件名拼接到工作区根目录。没有核实的路径不要写成链接。

## 来源

上游 https://github.com/wheresryan22/anatomy ，固定提交 `1482e8469b0a9e0d8cc18ea2740a01423d29ae80`，MIT。原版几何文件与 [LICENSE](LICENSE) 保留；完整上游入口另存 [upstream-skill.md](references/upstream-skill.md)，仅作来源参考，其中浏览器、发布、React、WebGL及未安装脚本的流程不适用于 TCode。
