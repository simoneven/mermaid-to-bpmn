# Mermaid → BPMN 2.0 Converter

将 Mermaid 流程图文本自动转换为标准的 **BPMN 2.0 XML** 文件，兼容 Camunda 8 / bpmn.io。

纯 Python 标准库实现，零依赖。

## 用法

```bash
# 从标准输入读取 Mermaid 文本，输出 .bpmn 文件
python3 scripts/mermaid_to_bpmn.py output.bpmn < flow.txt

# 或直接传入文本
python3 scripts/mermaid_to_bpmn.py "graph TD; A[开始] --> B{判断}" output.bpmn
```

### 支持的 Mermaid 语法

| Mermaid 语法 | BPMN 元素 | 说明 |
|---|---|---|
| `A[name]` | task / userTask / serviceTask | 矩形 → 任务，根据关键词智能分类 |
| `A{name}` | exclusiveGateway / parallelGateway | 菱形 → 网关 |
| `A([name])` 或 `A((name))` | startEvent / endEvent / intermediateCatchEvent | 圆角 → 事件，根据出入度判断 |
| `A[[name]]` | subProcess | 双矩形 → 子流程 |
| `A --> B` | sequenceFlow | 默认连接 |
| `A -->\|label\| B` | 带标签的连接 | 网关分支 → conditionExpression |
| `subgraph ... end` | lane（泳道） | 子图边界用于泳道分区 |

### 智能任务类型映射

脚本根据节点名称中的关键字自动判断任务类型：

- **userTask**: 审/审批/审核/人工/提交/发货/扫码/收货/入库/出库
- **serviceTask**: 系统/自动/service/auto/计算
- **sendTask**: 发送/通知/notify/email
- **receiveTask**: 接收/等待/receive/wait
- **businessRuleTask**: 规则/校验/validate/检查

### 命令行选项

```bash
# 格式化输出（缩进）
python3 scripts/mermaid_to_bpmn.py --format input.txt output.bpmn

# 验证生成的 BPMN XML
python3 scripts/mermaid_to_bpmn.py --validate input.txt output.bpmn
```

## 项目结构

```
mermaid-to-bpmn/
├── SKILL.md                    # Hermes Skill 元数据（触发条件、映射规则表、注意事项）
├── scripts/
│   ├── mermaid_to_bpmn.py      # 主脚本（part1 + part2 合并版，1280 行）
│   ├── mermaid_to_bpmn_part1.py  # Part 1: Mermaid 解析 + BPMN 元素映射
│   └── mermaid_to_bpmn_part2.py  # Part 2: 布局引擎 + BPMN XML 生成
├── LICENSE                     # MIT 协议
├── .gitignore
└── README.md
```

## 架构

```
Mermaid 流程图文本
    │
    ▼
┌─────────────────────────┐
│ parse_mermaid()         │  提取 nodes / edges / subgraph
│   stage 1: 方向/标题     │  支持内联节点定义 A[name] --> B{name}
│   stage 2: 节点+边解析  │
│   stage 3: 关系构建     │
└─────────┬───────────────┘
          ▼
┌─────────────────────────┐
│ auto_map_elements()     │  Mermaid → BPMN 类型映射
│   形状 + 关键字启发式     │  输出映射理由
└─────────┬───────────────┘
          ▼
┌─────────────────────────┐
│ auto_layout()           │  拓扑排序 + 层级/列分配
│   Kahn 拓扑排序          │  分支左右分列
│   坐标计算               │
└─────────┬───────────────┘
          ▼
┌─────────────────────────┐
│ build_bpmn_xml()        │  生成 BPMN 2.0 XML
│   BPMN 逻辑层            │  含命名空间、DI 层、waypoint
│   Zeebe 扩展             │
└─────────┬───────────────┘
          ▼
┌─────────────────────────┐
│ validate_bpmn_xml()     │  结构验证 + 业务逻辑检查
└─────────────────────────┘
```

## Requirements

- Python 3.8+
- 标准库（无外部依赖）

## 输出兼容

- ✅ **Camunda Modeler** 直接打开
- ✅ **bpmn.io** 在线查看器
- ✅ **Camunda 8 / Zeebe** 可执行流程
- ✅ **flowable** (基本兼容)

## License

MIT
