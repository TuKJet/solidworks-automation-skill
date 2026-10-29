---
name: solidworks-3d-2d
description: "将 SolidWorks 零件、装配体或工程目录中的 3D 模型转换为带模型尺寸的二维工程图草稿，默认输出 SLDDRW、PDF、DXF 和审查报告，并支持经结构校验的 STEP 几何交换；支持独立安装、检查版本及更新。用于 SolidWorks 3D 转 2D、批量零件/装配体出图和出图结果复核。"
---

# SolidWorks 3D 转 2D

这是独立 Skill，代码和运行依赖均在本目录，不需要完整 solidworks-automation 或通用 MCP 服务。执行主机必须是已安装并授权 SolidWorks 的 Windows，原生操作须处于可用的交互桌面会话。若模型在 VM，须在 VM 的该会话中运行；本机安装 Skill 并不等于能直接操作远端 COM。

## 使用入口

先用安装目录中的 `.venv/Scripts/python.exe scripts/solidworks_3d_2d.py doctor` 检查依赖与 SolidWorks；不存在 `.venv` 时按 [README](README.md) 安装依赖。执行脚本时使用绝对路径，或把工作目录设为 Skill 安装目录。

- `inventory --source <工程目录或零件> --output <新清单.json>`：不打开 CAD，核对文件、重复内容、同名版本及 ZIP 来源。
- `convert --source <零件.SLDPRT或工程目录> --drawing-template <.drwdot> --sheet-format <.slddrt>`：实际出图。先运行 `convert --help` 查看当前参数。默认工程下 `agent/solidworks-3d-2d/<run-id>`；原生图和 PDF 文件名加 `_ai`。
- `convert` 默认同时生成 DXF；只有在 `DrawingSpec.outputs.dxf` 明确设为 `false` 时禁用。DXF 交付需通过原生保存成功、新鲜文件时间/大小和实体计数核验，并分别审查顶层实体与块内实体中的标注；原生导出会临时关闭 `swDxfExportViewAsBlock`，完成后恢复原 SolidWorks 偏好。
- `export-step --source <零件、装配体或工程目录> [--output <新目录>] [--configuration <真实配置>] [--extract-zips]`：只读打开并把发现的零件和装配体导出为经结构校验的 STEP；压缩包模型只有指定 `--extract-zips` 才会解包。报告会明确记录 FreeCAD 导入未在本环境执行，不能把 STEP 结构校验写成 FreeCAD 兼容性验收。
- `--configuration` 必须用模型中真实存在的名字，不能把中文“默认”猜成 `Default`。目录批次的配置名会应用到所有选中模型，名字不一致应分批。
- `convert --layout-attempts` 最多允许 3 次有界布局尝试；先保留原比例增加视图间距，再尝试降低一档标准比例。只有尺寸身份、数值和可见性完全相同，且新鲜 PDF 的真实指标改善时才采用候选布局。不能靠不断缩小零件让文字重叠计数变为零。
- 默认只对零件出图；装配工程图需 `--include-assemblies --bom-template <.sldbomtbt>`。模板路径应来自本机 SolidWorks 或用户提供的模板；不猜测不存在的路径。
- 高级需求可用 `--spec <DrawingSpec.json>`，结构见 [DrawingSpec schema](subskills/solidworks-engineering-drawing/schemas/drawing_spec.schema.json)。剖视、局部图、复杂孔表/GD&T 等未实现字段须按能力门禁报告，不能静默忽略。

先读取用户源路径和已有工程约定，能从实际工程获取的信息不重复询问。缺少真实模板、配置或制造语义时，说明具体缺项；不要临时重写另一个出图脚本来跳过门禁。

## 必须保留的出图边界

- 全程使用同会话 CAD 作业锁；批次复用一个 SolidWorks 会话，并在每个零件/装配体后清理本轮文档。清理失败或会话连接丢失即停止批次，不能继续打开下一个模型，避免串件、旧文档引用或比例状态泄漏；遇到占用不得杀掉 SLDWORKS 或别人的任务。
- 只读打开源文件、不保存源模型，只关闭本轮拥有的文档。配置切换需回读且恢复既有状态，尺寸来自真实模型包围盒与模型尺寸。
- 输出目录属于单次运行，已有目录不覆盖；当前独立 CLI 不承诺断点续跑。保留每次运行的报告、账本与依赖，不把历史无归属文件当成可删除 scratch。
- `convert` 不直接接受 STEP；先用 SolidWorks 导入保存 SLDPRT。`export-step` 是几何交换导出，不保留参数树、配合或可编辑特征保证；无可导入尺寸的纯几何模型不会自动得到完整制造标注。只生成视图也不能称作已完成的标注图。
- FreeCAD 使用 DXF 时应选择其内置 C++ DXF importer；块只提供几何交换，DXF/STEP 都不会恢复 SolidWorks 模型参数关联。FreeCAD 实际导入未在本环境执行，状态保持 `not_tested`。参考：[FreeCAD DXF importer](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Draft_DXF.md)。
- 保存、PDF 导出、可读性和制造完整性分别报告。原生导出错误即使留下 PDF 也不能通过；图纸始终保留工程终审要求。
- 新生成图纸导入模型尺寸后，按来源、配置、模型尺寸身份、原生数值及完整标注语义去除重复显示；只隐藏证据一致的显示实例，不删除或改写模型尺寸。同值但不同身份、公差或孔标注不能合并。报告分别列出存储尺寸和可见尺寸，并回读确认保留实例仍可见。
- 去重后仍需分配引线负载：当直径在圆形视图和侧视图有等价显示实例时，圆形视图优先保留少量径向直径，其余分配到侧视图；唯一的孔径标注必须保留，不能为了达到数量上限而删除。重点检查径向引线是否挤在同一扇区，不能只检查文字框。
- 对单页零件图的正投影完整圆边，读取实际圆半径并补充缺少的直径数值；排除圆弧、斜投影椭圆，新增关联直径须经过原生类型/数值/可见性回读。补标最多 6 个不同直径，超限明确阻断；多页和装配体圆边补标尚不支持，必须保留复核状态。相同数值的既有直径只能记为“有此数值、关联未核验”，不能认定每一个孔或圆边都已覆盖。
- 视觉检查必须同时看“是否太多”和“是否漏了关键尺寸”：圆环检查内外径及长度，复杂端盖检查重复尺寸、引线交叉、视图尺寸和细小台阶。`文字重叠=0` 只是一项检测结果，不代表可读性或标注完整性通过。孔数量、孔位、深度、配合、公差和制造要求仍需独立证据，不以自动补圆径替代。
- 标注验收必须通过 `annotation_acceptance`：请求标注却没有可见尺寸、真实圆径值遗漏、重复模型尺寸仍可见、零件圆边审计失败、PDF 确认拥挤或越界均明确 `blocked`；不能用泛化的 `review_required` 掩盖这些缺陷。`review_required` 只表示尚需工程终审，并不等于上述缺陷可忽略。
- 更新标注逻辑后，先用同一实现版本完成原生小样回归再扩批：至少包含一个缺失内外径的圆环类样例和一个曾经拥挤的复杂样例。小样须检查实际 PDF/DXF、关键直径、可见重复及源文件哈希。报告中的 `implementation_fingerprint` 必须与后续批次一致；改过代码就重新验证小样，不能把旧版预览或 mock 测试当作新版原生验收。样例路径、源哈希和预期尺寸放在项目配置/证据中，不硬编码进 Skill。
- 检查最终 PDF 预览中的尺寸重叠、跨图和裁切。PDF 数字证据按实际显示精度匹配；复合数字缺少完整期望文本、多个候选或同一文字被多个尺寸争用时保持复核，不以首个数字或最近位置强行通过。密集尺寸可使用已保留的坐标附表模块，但只转移有真实 DXF 定义点、唯一匹配且数值通过校验的尺寸。不能为了看起来干净就隐藏未映射尺寸。

## 用户要求更新时

安装来源固定为 `https://github.com/TuKJet/solidworks-automation-skill.git` 的 `vm-sw-3d-2d` 分支。使用本 Skill 自带更新器，不自行删除重装，也不修改其他 Skill 或 MCP 配置。

```powershell
py -3 "<SKILL_DIR>\scripts\skill_manager.py" check --target "<SKILL_DIR>"
py -3 "<SKILL_DIR>\scripts\skill_manager.py" update --target "<SKILL_DIR>" --dependencies
```

“更新这个 Skill 到 GitHub 最新版本”已授权执行更新；只有用户说“检查更新”时只执行 check。更新前确认没有本 Skill 的转换任务正在运行；运行中先完成该任务。遇到 dirty、分支/仓库不符、非快进、依赖安装失败时，按实际错误报告并保留用户改动，不用强制 reset 绕过。记录 before/after commit，重新读取更新后的 SKILL.md，再运行 doctor。网络失败可使用用户已有的有效代理，仅作用于当前命令。

安装和完整示例见 [README](README.md)，实现来源与验证边界见 [架构与验证](references/architecture.md)。
