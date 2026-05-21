---
name: mermaid-to-bpmn
description: 将 Mermaid 流程图转化为标准 BPMN 2.0 XML（Camunda 8 / bpmn.io 兼容），含自动布局、智能元素映射和结果验证
tags: [bpmn, bpmn20, workflow, camunda, process-modeling]
---

# Mermaid → BPMN 2.0 转换器

## 触发条件

用户说"转 BPMN"、"生成 BPMN 文件"、"导出 bpmn 图"、"BPMN 2.0"、"Camunda"，
或在 flowchart-pipeline 中被调用。

## 架构总览

```
Mermaid 流程图文本
    │
    ▼
┌─────────────────────────────────────┐
│ 第一步：解析 Mermaid 语法            │
│ 提取 nodes: {id, name, type}        │
│ 提取 edges: [{src, tgt, label}]     │
│ 提取 subgraph → lane 信息            │
│ 同时处理内联节点定义                  │
└─────────────────┬───────────────────┘
                  ▼
┌─────────────────────────────────────┐
│ 第二步：智能元素映射                  │
│ Mermaid 节点类型 → BPMN 2.0 元素    │
│ 根据上下文判断并记录映射理由          │
│ 不确定的先问人，不猜                 │
└─────────────────┬───────────────────┘
                  ▼
┌─────────────────────────────────────┐
│ 第三步：自动布局引擎                  │
│ 拓扑排序确定层级                      │
│ 判定分支左右分列                      │
│ BPMNDI 坐标计算                      │
└─────────────────┬───────────────────┘
                  ▼
┌─────────────────────────────────────┐
│ 第四步：生成 BPMN 2.0 XML           │
│ 逻辑层 + DI层 + Namespaces          │
│ Camunda 8 (Zeebe) 兼容的元素结构      │
└─────────────────┬───────────────────┘
                  ▼
┌─────────────────────────────────────┐
│ 第五步：验证与修正                    │
│ 1. XSD 结构验证                      │
│ 2. 业务逻辑验证                      │
│ 3. 输出映射说明                      │
└─────────────────┬───────────────────┘
                  ▼
              输出 .bpmn 文件
```

## 第一步：解析 Mermaid 语法

复用与 mermaid-to-drawio / mermaid-to-vsdx 相同的解析脚本模式 (`parse_mermaid`)。

⚠️ **实战教训：必须同时支持 `graph` 和 `flowchart` 前缀。** 用户可能用 `graph TD` 或 `flowchart TD`，正则必须匹配两者 `(?:graph|flowchart)`。

⚠️ **实战教训：必须先提取内联节点定义，再解析边。** Mermaid 最常见的写法是 `A[名称] --> B{判断}`（节点定义和边在同一行）。脚本先在每一行扫描 `ID[文本]` 和 `ID{文本}` 模式提取节点，再把节点定义替换为纯 ID，然后解析边。参见 `_extract_inline_nodes()` 函数。

### 解析要点

```python
# 正则匹配节点
# ID[文本]       → rectangle (Task)
# ID([文本])     → stadium (Start/End)
# ID{文本}       → rhombus (Decision/Gateway)
# ID[[文本]]     → subprocess

# 解析连线
# A --> B              → 无标签边
# A -->|条件| B        → pipe 语法带标签
# A -- 条件 --> B      → 内联文本带标签

# 解析 subgraph
# subgraph 名称        → 泳道名
# end                  → subgraph 结束
```

### 内联节点解析实现

```python
def _extract_inline_nodes(line: str, nodes: dict) -> str:
    """
    从可能含有多节点的行中提取节点定义。
    A[名称] --> B{判断}  → 提取 A 和 B 两个节点，返回 "A --> B"
    """
    pat = re.compile(r"(\w[\w\d_]*)\[([^\]]+)\]|(\w[\w\d_]*)\{([^}]+)\}")
    result = line
    offset = 0
    for match in pat.finditer(line):
        nid = match.group(1) or match.group(3)
        name = match.group(2) or match.group(4)
        shape_char = match.group(0)[len(nid)]
        nodes[nid] = {"id": nid, "name": name.strip(),
                      "type": "rectangle" if shape_char == '[' else "rhombus"}
        start = match.start() + offset
        end = match.end() + offset
        result = result[:start] + nid + result[end:]
        offset += len(nid) - (end - start)
    return result
```

脚本存放在 `scripts/mermaid_to_bpmn.py`

## 第二步：智能元素映射

这是本 skill 最有价值的步骤，也是**踩坑最多的地方**。

### 元素映射表

| Mermaid 节点类型 | 默认 BPMN 映射 | 映射理由 | 可选替代 |
|---|---|---|---|
| `([开始])` / `([启动])` | `<bpmn:startEvent>` | 流程起点 | messageEventDefinition |
| `([结束])` / `([终止])` | `<bpmn:endEvent>` | 流程终点 | terminateEventDefinition |
| `[活动名]` 方形 | 基于关键词推断 | 见下方规则 | — |
| `[[子流程]]` 双层方括号 | `<bpmn:subProcess>` | 可展开的复合活动 | callActivity |
| `{判断}` 菱形 | `<bpmn:exclusiveGateway>` | 单路径选择（XOR） | parallelGateway |
| `{平行}` / 用 `||` 标记 | `<bpmn:parallelGateway>` | 并行分支（AND） | — |
| subgraph | `<bpmn:lane>` | 角色/责任分区 | participant |

### 关键词映射规则（含实战调整后的版本）

**优先级顺序**（从上到下，先匹配到即返回，不继续匹配）：

1. **serviceTask（系统/自动/计算）** — 放在首位，避免"系统基于收货单自动校验发票"被"收货"匹配到 userTask
2. **userTask（审批/人工/填写/提交/发货/扫码/收货/入库/出库）** — 特别注意"发货"和"扫码"也在这里
3. **sendTask** — Camunda 8 中的 sendTask 需要在 _XML 生成时_ 降级为 serviceTask（见第四步）
4. **receiveTask**
5. **businessRuleTask（规则/校验/验证/检查）**
6. **scriptTask**
7. 无匹配 → `task`（plain task），在 XML 生成时会被降级为 serviceTask

```python
# 关键词列表和优先级（Python 实现）
service_kw = ["系统", "自动", "service", "auto", "计算"]  # 最高优先级
user_kw = ["审", "审核", "审批", "review", "approve", "人工", "填写", "提交",
           "发货", "供应商", "扫码", "收货", "入库", "出库"]     # 第二优先级
send_kw = ["发送", "通知", "notify", "send", "email", "邮件"]
receive_kw = ["接收", "等待", "receive", "wait", "收取"]
rule_kw = ["规则", "校验", "rule", "validate", "验证", "检查", "审批规则"]
script_kw = ["脚本", "script", "执行脚本"]
```

⚠️ **实战教训 1：关键词优先级决定了映射准确性。**
- 最初 userTask 在 serviceTask 前面检查，导致"系统基于收货单自动校验发票"因含"收货"被映射为 userTask（❌ 应该是 serviceTask）
- 修复：把 serviceTask 提前到 userTask 前面

⚠️ **实战教训 2："处理"一词不要放在 service_kw 里。**
- "财务人工处理异常"含"处理"，如果 serviceTask 在 userTask 前检查，会被误判为 serviceTask
- 修复：把"处理"从 service_kw 移除，它是中性词

⚠️ **实战教训 3：单个中文字符匹配太宽泛。**
- "发送"中的"发"会匹配"供应商发货"→ sendTask（❌ 应该是 userTask）
- 修复：用完整的双字词"发货"匹配 userTask，放在 serviceTask 之后

⚠️ **实战教训 4（最重要的）：拿不准就提问，不要猜。**
- "供应商发货"没有匹配任何关键词 → plain task → 降级为 serviceTask（❌ 应该是 userTask）
- 如果有人看一眼就知道是人工操作，但关键词没法体现这种"常识"
- **不确定时先问用户，用户不给信息再猜。** 不要自己直接生成交付物。

### 映射说明生成

自动输出映射理由到 stderr，格式：
```
"■ 部门经理审核" -> userTask (contains keyword "审", inferred as user task)
"◆ 预算检查" -> exclusiveGateway (out-degree 2, default XOR gateway)
"■ 供应商发货" -> userTask (contains keyword "发货", inferred as user task)
```

## 第三步：自动布局引擎

### BPMN 标准尺寸

```
Start/End Event:  36×36 像素（圆形，pin 在中心）
Task:            100×80 像素（圆角矩形）
Gateway:          50×50 像素（菱形）
SubProcess:      140×100 像素
```

### 布局算法

使用拓扑排序确定层级，分支左右分列（"通过/是"在右边，"拒绝/不通过"在左边）。

```python
# BPMNDI 坐标使用左上角坐标（非中心点）
def calc_bounds(x_center, y_center, width, height):
    return {
        'x': x_center - width / 2,
        'y': y_center - height / 2,
        'width': width,
        'height': height
    }
```

### 连接线 Waypoint

- 直线（同层/同列）：2 个 waypoint
- L 形折线（分支左右分列）：3 个 waypoint
- Z 形折线（跨多层）：4 个 waypoint

## 第四步：生成 BPMN 2.0 XML

### 命名空间（Camunda 8 / Zeebe 兼容，经实践验证）

```python
NS_BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS_BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
NS_DI = "http://www.omg.org/spec/DD/20100524/DI"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_MODELER = "http://camunda.org/schema/Modeler/1.0"
NS_CAMUNDA = "http://camunda.org/schema/1.0/bpmn"
NS_ZEEBE = "http://camunda.org/schema/zeebe/1.0"
```

**executionPlatform 设置**（Camunda Modeler 兼容）：
```python
modeler:executionPlatform="Camunda Cloud"
modeler:executionPlatformVersion="1.0.0"
```

### 任务类型的 Camunda 8 扩展元素

使用 `zeebe:` 命名空间而非 `camunda:` 命名空间（Camunda 8 不再支持 `camunda:class`）：

**serviceTask / sendTask（被降级为 serviceTask）**：
```xml
<bpmn:serviceTask id="D" name="退回修改">
  <bpmn:extensionElements>
    <zeebe:taskDefinition type="退回修改" />
  </bpmn:extensionElements>
  <bpmn:incoming>...</bpmn:incoming>
</bpmn:serviceTask>
```

**userTask**：
```xml
<bpmn:userTask id="A" name="员工提交请购单">
  <bpmn:extensionElements>
    <zeebe:formDefinition formKey="embedded:app:forms/员工提交请购单.html" />
  </bpmn:extensionElements>
  <bpmn:incoming>...</bpmn:incoming>
</bpmn:userTask>
```

⚠️ **实战教训 1：sendTask 在 Camunda 8 (Zeebe 1.0) 中不被完全支持。**
- Camunda Modeler 报 "A <Send Task> is only supported by Camunda 8 (Zeebe 1.1) or newer"
- 即使加了 `zeebe:taskDefinition` 也不行，因为 Camunda 8 对 sendTask 的支持有限
- **解决方案**：在 XML 生成阶段把 sendTask 映射为 serviceTask，加 `_send` 后缀的 taskDefinition type
```python
elif bpmn_short == "sendTask":
    bpmn_nodes[nid]["bpmn_type"] = "bpmn:serviceTask"
    elem.tag = "bpmn:serviceTask"
    ext = ET.SubElement(elem, "bpmn:extensionElements")
    td = ET.SubElement(ext, "zeebe:taskDefinition")
    td.set("type", name.replace(" ", "_") + "_send")
```

⚠️ **实战教训 2：plain `task` 在 Camunda 8 中不支持。**
- 无类型声明的 `bpmn:task` 在 Camunda Modeler 中报 "no implementation defined" 警告
- **解决方案**：自动降级为 serviceTask + zeebe:taskDefinition

⚠️ **实战教训 3：`conditionExpression` 不要加 `xsi:type`。**
- 之前写 `<bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">` 会在 Camunda Modeler 中报 "unknown type <undefined:tFormalExpression>"
- Camunda 8 不识别 `xsi:type` 的 `tFormalExpression` 写法
- **解决方案**：直接写裸的 `<bpmn:conditionExpression>${条件}</bpmn:conditionExpression>`，不需要 `xsi:type`

### 逻辑层结构

```xml
<bpmn:process id="Process_1" isExecutable="true" name="流程名称">
  <!-- startEvent（自动添加，如 Mermaid 无显式开始节点） -->
  <bpmn:startEvent id="StartEvent_auto" name="开始">
    <bpmn:outgoing>Flow_start_1</bpmn:outgoing>
  </bpmn:startEvent>

  <!-- 活动节点 -->
  <bpmn:userTask id="A" name="员工提交请购单">
    <bpmn:extensionElements>
      <zeebe:formDefinition formKey="..." />
    </bpmn:extensionElements>
    <bpmn:incoming>Flow_start_A</bpmn:incoming>
    <bpmn:outgoing>edge_1</bpmn:outgoing>
  </bpmn:userTask>

  <!-- 网关 + 条件分支 -->
  <bpmn:exclusiveGateway id="B" name="预算检查">
    <bpmn:incoming>edge_1</bpmn:incoming>
    <bpmn:outgoing>edge_2</bpmn:outgoing>
    <bpmn:outgoing>edge_3</bpmn:outgoing>
  </bpmn:exclusiveGateway>

  <!-- 条件 sequenceFlow -->
  <bpmn:sequenceFlow id="edge_2" sourceRef="B" targetRef="C">
    <bpmn:conditionExpression>${预算不足/超规}</bpmn:conditionExpression>
  </bpmn:sequenceFlow>

  <!-- 无条件 sequenceFlow -->
  <bpmn:sequenceFlow id="edge_4" sourceRef="C" targetRef="D" />
</bpmn:process>
```

### DI（Diagram Interchange）层

```xml
<bpmndi:BPMNShape id="StartEvent_auto_di" bpmnElement="StartEvent_auto">
  <dc:Bounds x="152" y="102" width="36" height="36" />
</bpmndi:BPMNShape>

<bpmndi:BPMNEdge id="edge_2_di" bpmnElement="edge_2">
  <di:waypoint x="200" y="120" />
  <di:waypoint x="300" y="120" />
</bpmndi:BPMNEdge>
```

⚠️ **实战教训：不要在 XML 文件中手动替换元素类型。** 之前用 `content.replace()` 把 sendTask 改为 serviceTask 时，只改了开头标签没改关闭标签（`</bpmn:sendTask>`），导致 XML 非法。**所有修改都应该通过脚本的生成逻辑完成。**

## 第五步：验证

### 结构验证

```bash
# 验证 XML 格式
python3 -c "import xml.etree.ElementTree as ET; ET.parse('output.bpmn')"

# 检查 sendTask 是否已降级
python3 -c "import xml.etree.ElementTree as ET
ns = {'bpmn': 'http://www.omg.org/spec/BPMN/20100524/MODEL'}
root = ET.parse('output.bpmn')
cnt = len(root.findall('.//bpmn:sendTask', ns))
print(f'sendTask count: {cnt}')  # 应为 0
"
```

### 手动验证步骤

1. **bpmn.io**：拖到 https://bpmn.io/ → 看是否渲染成功
2. **Camunda Modeler**：打开 → 检查属性面板是否能编辑元素，有无 warning
3. **流程走一遍**：在脑子里走一遍 token 流，确认所有路径都通

## 完整实现脚本

可运行脚本：`scripts/mermaid_to_bpmn.py`

```bash
# 基本用法
cat input.mermaid | python3 scripts/mermaid_to_bpmn.py output.bpmn
python3 scripts/mermaid_to_bpmn.py 'mermaid语法' output.bpmn --format --validate
```

### 脚本核心接口

```python
def parse_mermaid(text: str) -> Tuple[Dict, List, str]:
    """解析 Mermaid 流程图，返回 (nodes, edges, flow_name)"""

def auto_map_elements(nodes: Dict, edges: List) -> Tuple[Dict, List, List]:
    """智能元素映射，返回 (bpmn_nodes, bpmn_edges, mapping_explanations)"""

def auto_layout(bpmn_nodes: Dict, bpmn_edges: List) -> Dict:
    """自动布局，返回 {node_id: {x, y, layer, col, ...}}"""

def build_bpmn_xml(...) -> str:
    """生成 BPMN 2.0 XML 字符串"""

def validate_bpmn_xml(...) -> List:
    """验证 BPMN XML 结构完整性"""
```

## 交付规范

### 核心原则

1. **拿不准先问人，不猜，不交付。** 不确定节点类型的映射（如\"供应商发货\"是系统自动还是人工操作），先问用户。**在得到用户确认之前，不要生成最终 BPMN 文件。** 不要自己推断后直接交付——用户更愿意被问也不愿意看到错的交付物。
2. **把映射理由告知用户。** 每生成一个 BPMN，同步输出映射说明，并标注哪些节点是确定的、哪些是需要用户确认的。
3. **不确定的地方标注说明。** 如\"判定节点'{节点名}'出度为{N}且连线无标签，我默认按 XOR 处理\"。
4. **验证通过后再交付。** 生成后先用 Camunda Modeler 或 bpmn.io 验证，确认零警告再发文件给用户。

### 文件发送

通过飞书 API 上传 `.bpmn` 文件：
```bash
# 1. upload → 获取 file_key
curl -X POST 'https://open.feishu.cn/open-apis/im/v1/files' \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file_type=stream' \
  -F 'file_name=xxx.bpmn' \
  -F 'file=@/path/to/xxx.bpmn'

# 2. 发送文件消息
curl -X POST 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"receive_id":"USER_ID","msg_type":"file","content":"{\"file_key\":\"...\"}"}'
```

## 参考

- BPMN 2.0 规范: https://www.omg.org/spec/BPMN/2.0/
- Camunda BPMN 参考: https://camunda.com/bpmn/reference/
- bpmn.io 在线查看器: https://bpmn.io/
- Camunda Modeler: https://camunda.com/modeler/

## 已知限制

本 skill 已实现的：
- ✅ 基础流程图：Start → Task → ExclusiveGateway(XOR) → End
- ✅ Parallel Gateway (AND) — 使用 `||` 标记自动识别
- ✅ userTask / serviceTask / sendTask(降级) / receiveTask / businessRuleTask
- ✅ 条件分支标签 — 连线标注写入 conditionExpression
- ✅ 自动布局 — 拓扑排序 + 左右分列
- ✅ BPMNDI 布局坐标 — 可被 Camunda Modeler 和 bpmn.io 渲染
- ✅ XML 结构验证
- ✅ 格式美化
- ✅ 映射说明输出到 stderr
- ✅ `graph` 和 `flowchart` 前缀都支持
- ✅ 内联节点定义 `A[名称] --> B{判断}`
- ✅ Camunda 8 (Zeebe) 兼容：zeebe:taskDefinition / zeebe:formDefinition
- ✅ 自动添加 startEvent（Mermaid 无显式开始节点时）
- ✅ 条件表达式不加 xsi:type
- ✅ sendTask 自动降级为 serviceTask

暂不支持：
- ❌ 泳道（Lane）/ subgraph — 已记录 subgraph 信息但未生成 lane 元素
- ❌ 边界事件（Boundary Event）
- ❌ 消息流（Message Flow）
- ❌ 子流程展开态（Expanded SubProcess）
- ❌ 多实例（Multi-Instance）标记
- ❌ 补偿（Compensation）相关
- ❌ 链接事件（Link Event）
