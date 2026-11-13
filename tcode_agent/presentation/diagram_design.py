"""Small, offline diagram guidance and conservative checks for validated SVG trees.

Inspired by diagram-design (MIT): select by meaning, overview/detail, readable
labels and connector clearance. No upstream templates or scripts are bundled.
"""
from __future__ import annotations

import math
import re
import unicodedata
from xml.etree import ElementTree as ET


TYPES = {
    "structure": ("组成示意图", "canvas", "保持对象比例；部件用短名称标注，长说明放入可点击部件。"),
    "tree": ("层级结构图", "canvas", "按父子层级分区，同层对齐；避免用连接表示并不存在的因果关系。"),
    "flowchart": ("流程图", "canvas", "沿一个阅读方向排步骤，分支标明条件；连接绕开无关节点和文字。"),
    "timeline": ("时间线", "canvas", "时间方向一致；只在真实时间数据存在时按比例定位，否则明确表示阶段顺序。"),
    "relationship": ("关系图", "canvas", "分组相关对象；边用简短中文说明关系，不能把相关性画成因果关系。"),
    "comparison": ("对比表", "intelligent_page", "用卡片中的对应属性或 HTML 表格比较；只有关系结构改变时才画前后地图。"),
    "bar": ("柱状图", "intelligent_page", "比较类别数量；标明单位，提供数据表，只使用已知值或明确标注的示例值。"),
    "line": ("折线图", "intelligent_page", "展示连续时间趋势；标明时间、单位和缺失值，不把无序类别连成趋势。"),
    "page": ("组合交互页面", "intelligent_page", "用少量图表、卡片、步骤和真实按钮组合；复用页面主题变量与本地交互。"),
}

# Layout only: colors, materials and corner treatment still belong to the user's style.
LAYOUTS = {
    "structure": "主体占绘图区约三分之二，保持真实比例；注释放在两侧对齐的短标签列，指引线避开主体关键细节；说明留给点击详情。",
    "tree": "根节点居中；每层共用基线，同层节点按标签宽度加内边距定宽；先计算叶节点总宽，再定位父节点；空间不足改纵向或拆分详情。",
    "flowchart": "默认从上到下；步骤同宽、同层对齐，判断用菱形、起止用圆角形；分支逐条标明条件，回流走外侧独立通道，不穿过无关节点。",
    "timeline": "时间轴独立成一列或一行，日期与事件对齐；同一时间多事件分轨排列，说明不挤进轴线；仅真实时间间隔才按比例。",
    "relationship": "先按语义分区，再放节点；核心假说居中，支持与反对证据分区；关系标注靠近对应边，节点外预留连线通道，不用颜色单独表示证据状态。",
    "comparison": "共用属性行与单位，名称列固定；长内容换行、表头清晰；窄窗口使用对应属性卡片或可横滚表格，不把文字缩小。",
    "bar": "共享零基线与刻度；类别标签长时优先横向条形；单位写在轴或值旁，保留数值表；不要用装饰性三维或颜色改变数值含义。",
    "line": "共享坐标轴和单位；缺失数据断开；优先在线尾标记系列，拥挤时改图外图例；给极值标签留边距。",
    "page": "先主图后解释与操作；参数靠近受影响的图，局部操作就近放置；使用响应式网格，窄窗口单列；说明默认简短，长内容可展开。",
}


def visual_recipe(kind: str) -> dict:
    return {
        "layout": LAYOUTS[kind],
        "spacing": {"grid": 8, "safe_margin": 40, "node_padding": 16, "peer_gap": 24, "lane_gap": 48, "label_clearance": 8},
        "typography": {"title": 28, "section": 20, "label": 18, "note": 14,
                       "rule": "以960单位宽SVG为起点；按实际显示比例检查可读性，窄图改布局而非整体缩小文字。中文短标签优先，不用等宽字体排正文。"},
        "composition": "先预留标题、主图、图外图例与来源位置，再画节点；对齐边缘与基线，缩短无意义的空白；同类节点保持一致，按内容需要改变宽度。",
        "connectors": "结构图优先水平／垂直折线；各条线使用独立连接点及通道。先画线后画节点，标签周围留8单位净空；用断开的线段避开文字，不画线穿字。物体轮廓和数据曲线不受直角规则限制。",
        "style_policy": "继承effective_style的配色、材质、圆角及用户指定颜色；只强调1–2个重点。HTML使用宿主主题变量，SVG沿用当前风格色。不引入外部字体、脚本、SVG滤镜或新的设置开关。",
        "delivery": "保留稳定元素ID、parts及原交互；临时查看不增版本。先检查再保存，报告不是排版自动修复或浏览器验收。",
    }


def recommend_diagram(request: str) -> dict:
    """A local hint, not a hard tool filter; the Agent can revise it from context."""
    text = request.casefold()
    explicit = (
        ("tree", ("树状图", "层级图", "组织架构图", "tree diagram")),
        ("flowchart", ("流程图", "flowchart")),
        ("timeline", ("时间线", "时间轴", "timeline")),
        ("relationship", ("关系图", "证据地图", "假说地图", "relationship graph")),
        ("bar", ("柱状图", "条形图", "bar chart")),
        ("line", ("折线图", "line chart")),
        ("comparison", ("对比表", "比较表", "表格", "comparison table")),
    )
    kind = next((kind for kind, words in explicit if any(w in text for w in words)), "")
    reason = "优先采用用户明确指定的图形类型。"
    if not kind:
        rules = (
            ("page", ("按钮", "卡片", "交互页面", "dashboard", "参数联动", "滑块", "试算")),
            ("timeline", ("发展历程", "按时间", "时间顺序", "里程碑")),
            ("line", ("趋势", "随时间", "逐月", "逐年", "trend")),
            ("tree", ("层级", "父子", "分类体系", "hierarchy")),
            ("flowchart", ("步骤", "流程", "如何处理", "操作顺序", "workflow")),
            ("relationship", ("关系", "支持证据", "反证", "依赖", "关联")),
            ("comparison", ("比较", "对比", "优缺点", "compare")),
            ("bar", ("各类数量", "销量", "数量分布")),
        )
        kind = next((kind for kind, words in rules if any(w in text for w in words)), "structure")
        reason = "根据问题的组织目的推荐；结合上下文修正，不限制工具权限或用户选择。"
    label, renderer, guide = TYPES[kind]
    return {"diagram_type": kind, "label": label, "recommended_tool": renderer,
            "reason": reason, "layout_guide": guide,
            "visual_recipe": visual_recipe(kind),
            "shared_rules": "沿用当前账户风格；统一字号、间距和对齐。总览优先保留约 9 个重点对象，"
            "更复杂内容分组或总览加详情，不丢弃数据。仅强调 1–2 个阅读重点；科研状态仍需文字和线型区分。"
            "内部 ID 仅用于点击绑定，显示中文名称。生成前说明选择即可，不额外要求用户确认。"
            "仅 SVG 画布草稿先用 canvas check 检查；修复 errors，审查 warnings，再 create。"
            "HTML 页面沿用页面协议校验，不将 HTML 交给 SVG 检查。"}


def check_layout(root: ET.Element, parts=()) -> dict:
    """Exact basic-shape bounds; text is estimated, curves intentionally unverified.

    Warnings never claim browser rendering or scientific validation. Bounded
    samples prevent quadratic label checks on the maximum accepted SVG tree.
    """
    issues: list[dict] = []
    counts = {"error": 0, "warning": 0}
    skipped: set[str] = set()
    labels: list[tuple] = []
    connectors: list[tuple] = []
    font_sizes: set[float] = set()

    def issue(code, message, elements=(), *, error=False):
        severity = "error" if error else "warning"
        counts[severity] += 1
        if len(issues) < 40:
            issues.append({"code": code, "severity": severity, "message": message,
                           "element_ids": list(elements)})

    values = re.split(r"[\s,]+", root.get("viewBox", "").strip())
    try:
        viewport = tuple(float(v) for v in values)
        if len(viewport) != 4 or not all(math.isfinite(v) for v in viewport) or min(viewport[2:]) <= 0:
            raise ValueError
    except ValueError:
        issue("invalid_viewbox", "请设置四个有效数值的 viewBox，宽高必须大于零。", error=True)
        return {"status": "failed", "issues": issues, "counts": counts, "coverage": "partial"}
    vx, vy, vw, vh = viewport

    def outside(box):
        x0, y0, x1, y1 = box
        return x0 < vx - 2 or y0 < vy - 2 or x1 > vx + vw + 2 or y1 > vy + vh + 2

    def intersection(a, b):
        return max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))

    def visit(node, dx=0., dy=0., font=16., anchor="start", hidden=False):
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in {"title", "desc"}:
            return
        nums = lambda value: [float(v) for v in re.split(r"[\s,]+", value.strip()) if v]
        number = lambda name, default=0.: float(node.get(name, str(default)))
        try:
            transform = node.get("transform", "")
            if transform:
                move = nums(transform[transform.index("(")+1:-1])
                dx += move[0]; dy += move[1] if len(move) > 1 else 0
            font = number("font-size", font)
            anchor = node.get("text-anchor", anchor)
            hidden = hidden or number("opacity", 1) == 0
            if hidden:
                return
            box = None
            if tag == "rect":
                x, y, w, h = (number(k) for k in ("x", "y", "width", "height"))
                if w < 0 or h < 0:
                    issue("negative_size", "图形宽高不能为负数。", [node.get("id")], error=True)
                elif w and h:
                    box = (x, y, x+w, y+h)
            elif tag in {"circle", "ellipse"}:
                x, y = number("cx"), number("cy")
                rx, ry = (number("r"), number("r")) if tag == "circle" else (number("rx"), number("ry"))
                if min(rx, ry) < 0:
                    issue("negative_size", "图形半径不能为负数。", [node.get("id")], error=True)
                elif rx and ry:
                    box = (x-rx, y-ry, x+rx, y+ry)
            elif tag == "line":
                x1, y1, x2, y2 = (number(k) for k in ("x1", "y1", "x2", "y2"))
                box = (min(x1,x2), min(y1,y2), max(x1,x2), max(y1,y2))
                if len(connectors) < 150:
                    connectors.append((node.get("id"), (x1+dx,y1+dy,x2+dx,y2+dy)))
                else:
                    skipped.add("超过 150 段直线的连线检查")
            elif tag in {"polygon", "polyline"}:
                coords = nums(node.get("points", ""))
                if len(coords) >= 4 and len(coords) % 2 == 0:
                    box = (min(coords[::2]), min(coords[1::2]), max(coords[::2]), max(coords[1::2]))
                    if tag == "polyline":
                        for i in range(0, len(coords)-2, 2):
                            if len(connectors) < 150:
                                connectors.append((node.get("id"), (coords[i]+dx, coords[i+1]+dy, coords[i+2]+dx, coords[i+3]+dy)))
                            else:
                                skipped.add("超过 150 段直线的连线检查")
                else:
                    skipped.add("无效或不完整的多边形坐标")
            elif tag == "text":
                if len(node):
                    skipped.add("含 tspan 的文字排版")
                else:
                    text = (node.text or "").strip()
                    width = sum(1 if unicodedata.east_asian_width(c) in {"W", "F"} else .56 for c in text) * font
                    x, y = number("x")+dx, number("y")+dy
                    x -= width/2 if anchor == "middle" else width if anchor == "end" else 0
                    baseline = node.get("dominant-baseline", "auto")
                    top = y-font/2 if baseline in {"middle", "central"} else y-font*.8
                    bounds = (x, top, x+width, top+font)
                    if text and font < 12:
                        issue("small_text", "字号偏小，建议至少 13–14；长说明放入点击详情。", [node.get("id")])
                    if text and outside(bounds):
                        issue("text_bounds_estimated", "文字可能超出画布，请缩短、换行或扩大画布。", [node.get("id")])
                    if text and len(labels) < 150:
                        labels.append((node.get("id"), bounds))
                        font_sizes.add(font)
                    elif text:
                        skipped.add("超过 150 个文字标签的重叠检查")
            elif tag == "path":
                skipped.add("曲线路径边界及路径连线避让")
            if box and outside((box[0]+dx,box[1]+dy,box[2]+dx,box[3]+dy)):
                issue("shape_outside_viewbox", "图形超出画布，请调整位置或扩大 viewBox。", [node.get("id")], error=True)
        except (ValueError, IndexError):
            skipped.add("包含非单值坐标的元素")
        for child in node:
            visit(child, dx, dy, font, anchor, hidden)

    visit(root)
    if any(x1 != x2 and y1 != y2 for _, (x1,y1,x2,y2) in connectors):
        skipped.add("斜线与标签的相交检查")
    ids = {node.get("id") for node in root.iter()}
    part_ids = [p.get("element_id") if isinstance(p, dict) else p.element_id for p in parts]
    if len(set(part_ids)) != len(part_ids) or any(p not in ids for p in part_ids):
        issue("invalid_part_binding", "可点击部件必须绑定唯一且实际存在的元素。", error=True)
    for i, (a, box) in enumerate(labels):
        for b, other in labels[i+1:]:
            if intersection(box, other) > min((box[2]-box[0])*(box[3]-box[1]), (other[2]-other[0])*(other[3]-other[1]))*.2:
                issue("label_overlap_estimated", "两个标签可能重叠，请拉开距离并检查实际显示。", [a,b])
        for b, (x1,y1,x2,y2) in connectors:
            if (y1 == y2 and box[1] < y1 < box[3] and max(x1,x2) > box[0] and min(x1,x2) < box[2]
                or x1 == x2 and box[0] < x1 < box[2] and max(y1,y2) > box[1] and min(y1,y2) < box[3]):
                issue("connector_label_overlap", "直线可能穿过标签，请为文字留白或调整连接路线。", [a,b])
    if len(parts) > 9:
        issue("overview_density", "总览对象较多，建议分组或增加局部详情；不要为了简化而丢弃内容。")
    if len(font_sizes) > 5:
        issue("typography_scale", "字号层级过多，建议统一为标题、分区、标签、注释四级；保留必要的公式标记。")
    for element, box in labels:
        if not outside(box) and min(box[0]-vx, box[1]-vy, vx+vw-box[2], vy+vh-box[3]) < 8:
            issue("label_edge_clearance", "文字贴近画布边缘，建议至少留8单位净空，重新分配留白而非缩小字号。", [element])
    return {"status": "failed" if counts["error"] else "needs_review" if counts["warning"] or skipped else "passed",
            "counts": counts, "issues": issues, "issues_truncated": sum(counts.values()) > len(issues),
            "coverage": "partial", "unchecked": sorted(skipped),
            "note": "静态几何检查；文字尺寸为估算。未验证浏览器实际字体、缩放后的可读性、遮挡和科研内容准确性。"}
