# 提取范围与验证

2026-10-29 标注反馈修复：原始后端盖曾显示 37 个尺寸，但只有 19 个唯一模型尺寸；原始铁芯只有两处相同长度，缺内外径。新增 `drawing_annotation_policy.py` 做保守的模型身份/语义去重，`drawing_geometry_annotations.py` 在本轮拥有的新图纸中隐藏重复显示，并从正投影完整圆边补关联直径。真实铁芯圆边读取为外径 11.2 mm、内径 9 mm，原生补标读回与 PDF 试验已验证。此前的“文字重叠减少”数据不能当作整图可读或制造标注完整的结论。

新增补标只覆盖单页零件图中的缺失直径数值，不证明孔数量、孔位、深度、公差或每个同径实体的关联完整；遇到同值既有直径报告 attachment_unverified，始终保留工程复核。圆弧、斜投影椭圆、多页图纸和装配体不会被当作已验证圆径覆盖。原生 `AddDiameterDimension2` 的输出在排列后再次核对身份、类型、数值、可见性。矩阵投影按 [SOLIDWORKS IMathTransform 官方定义](https://help.solidworks.com/2023/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IMathTransform.html) 计算，避免部分 COM 环境的 MathUtility 调用异常。

反馈样例最终原生回归：铁芯只显示长度 17、内径 9、外径 11.2；后端盖隐藏 18 个重复显示，并补充实际圆边直径 3.1，共 20 个可见尺寸。等价直径分担到侧视图，跨视图近邻尺寸移至外侧，`SetPosition2` 后逐项核对原生坐标，未写源尺寸。两张最终 PDF 已渲染复核，SLDDRW/PDF/DXF 均成功，源哈希未变。代码指纹记录在原生报告中；本版适用测试 309 passed、2 skipped、2 个完整 MCP 专用测试排除。

另修正 SW 2025 的未定义接口调用：仅在 `IsHoleCallout=True` 时读取 `GetHoleCalloutVariables`，普通尺寸返回空变量集，未知状态继续保守保留。新增 `annotation_acceptance` 把零尺寸、遗漏圆径、未处理重复、圆审计错误和确认拥挤明确阻断。PDF 数字框没有重叠仍不能证明符号/引线可读，原生小样视觉复核是扩批前置步骤。

整批复核又发现两项检查器缺陷：估算文字框越界被标为确定失败，PDF 匹配结果误复用了最后一个尺寸的名称。现在估算/不可用框仅保留低置信风险，原生/PDF 真实边界仍失败；每个匹配项按原索引读取尺寸名称，碰撞可追溯到实际尺寸。新增回归后 313 passed、2 skipped、2 个完整 MCP 专用测试排除。检查器单独修订后的离线复核必须保留原生报告与生成指纹，并另记审查指纹，不把重新审查描述为重新生成 CAD 文件。

修订检查器后，两张原生回归仍为铁芯 3 个、后端盖 20 个可见尺寸，实际 PDF 已复核。24 件最终选择中，两件使用独立比例配置修正版，其余保留原批次；原生尺寸身份、数值、可见性签名相同，生成与审查指纹分列。最终共 251 个可见尺寸、213 个重复显示隐藏、44 个真实圆径补标；24 套 SLDDRW/PDF/DXF 均在，DXF 尺寸数逐件对应，721 个原始源文件哈希未变。结果 21 件 `review_required`、3 件 `blocked`：两件没有可导入/可安全补充的尺寸，一件仍有 PDF 文字碰撞，未宣称全自动标注完成或制造放行。

反馈前的 2026-10-29 批次（历史证据，不能代表本次标注修复）：24 个零件版本均保存 SLDDRW、PDF、DXF；21 件有可导入模型尺寸，共 420 个，DXF 顶层 DIMENSION 数逐件一致。10 件选用了重叠指标改善的布局，PDF 文字重叠总数从 32 降至 3；2 件仍未通过可读性门禁，另 3 件无模型尺寸。STEP 导出覆盖 24 个零件、8 个装配体，另 1 个装配体在批次及独立只读重试中均报告 dirty，保持阻断。721 个源文件该轮哈希未变。FreeCAD 实际导入未执行。回归测试 280 passed、2 skipped；独立包不含的两项 MCP 服务测试显式排除。

从 `vm-sw-automation-skill` 的 `8a572ce` 提取，独立分支为 `vm-sw-3d-2d`，对外 Skill 名为 `solidworks-3d-2d`。

## 保留的实现

- `scripts/sw_connect.py`、`sw_session.py`、`sw_process.py`：COM 上下文、完整路径、文档归属、同桌面会话作业互斥。
- `subskills/solidworks-engineering-drawing/scripts`：DrawingSpec、原生三视图/尺寸导入、纸张与比例保留、注释、导出、可读性审查、密集尺寸坐标附表。目录布局保留是为了复用既有实现，不要求另装父 Skill。
- `scripts/sw_project_inventory.py`、`sw_project_batch.py`、`artifact_ledger.py`：安全清单、可组合批次底层与账本。独立 CLI 选择全新运行目录，并不把底层 checkpoint 能力宣传为 CLI 已支持 resume。
- `scripts/sw_delivery.py`：BOM 与引用包辅助能力。本次 CLI 的目标是出图，不能把底层 PackAndGo 的存在当作每次输出已经自包含。
- `scripts/step_conversion.py` 与 `export-step` 入口：对工程目录中发现的零件和装配体做只读 STEP 几何交换导出，可用 `--extract-zips` 解包归档来源。输出只保证 STEP ISO-10303-21 的基本结构和导出证据，不保证参数树、配合或可编辑特征；FreeCAD 导入在本环境未执行，报告必须保持 `not_tested`。
- `convert` 默认还导出 DXF；`DrawingSpec.outputs.dxf=false` 才禁用。DXF 导出要求原生保存成功、输出文件新鲜度和实体计数通过，并检查顶层与块内实体的标注；导出期间临时关闭 `swDxfExportViewAsBlock`，结束时恢复 SolidWorks 偏好。FreeCAD 侧应使用内置 C++ DXF importer；块只保留几何，DXF/STEP 都不恢复模型参数关联，实际导入仍为 `not_tested`。参考：[FreeCAD DXF importer](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Draft_DXF.md)。

批次会话与布局边界：批量出图/STEP 导出在同一排他 SolidWorks 会话内串行处理，每项完成后关闭本轮拥有的文档；清理失败、连接丢失或重复错误会停止剩余批次，防止旧文档、配置或比例串入下一项。复杂零件的布局最多尝试 3 次，只有尺寸身份、数值、可见性完全一致且重新导出的 PDF 真实指标改善才接受候选；估算尺寸框不作为真实拥挤证据。

账本从桌面模块迁入 scripts，仅带入 `safe_job_id` 和 `now_iso` 两个依赖，避免安装整个桌面应用。移除建模缓存调用和不属于本包的建模/干涉审查入口；原模型与装配几何未被改写。原通用 MCP 不打包，用户可通过 Agent 的命令执行工具调用本 Skill。

## 证据范围

继承核心在 SolidWorks 2025 / revision 33.0.0 上执行过 24 图回归：23 项功能检查通过，1 项 PDF 原生错误码 1；可读性与制造完整性仍保持待审。另有 9 套 BOM、2 个引用副本重开、51 个线性尺寸分两页的附表回归。这些是继承核心证据，不能自动等同于本次独立安装后的端到端验证。

本次独立分支在 2026-10-28 的验证：236 个适用测试通过、2 个本地缺少 DXF 库的测试跳过、2 个完整 MCP 服务专用测试显式排除。Skill frontmatter 校验通过；安装器覆盖本地 Git 安装、发现新版本、快进更新、脏目录拒绝及失败回滚。独立 CLI 在 VM 的 SolidWorks 2025 上使用真实 A3 文档模板/图框和原生零件生成三视图及 12 个尺寸，SLDDRW 保存和 PDF 原生导出均成功，源文件 hash 不变、配置恢复通过、CAD 进程未重启。最终 PDF 已渲染检查，尺寸匹配不完整、材料及业务标题栏未补全仍保留 review_required，未声称制造通过。完整 MCP 专用测试在独立包验证时明确排除；无需为了通过测试伪装存在一个 MCP 服务。
PDF 尺寸回读按显示精度核对数值；缺少完整复合标注文本、候选歧义或角度语义不完整时保持 `review_required`，不能用估算框、首个数字或最近文字替代唯一语义证据。
独立安装验证：在全新目录从本分支克隆，经自带安装器创建隔离 .venv 并安装 requirements.txt 成功；pip check 通过，安装副本 CLI 可直接启动，本机未装 SolidWorks 时 doctor 正确阻断。更新器随后在这个真实安装副本上执行版本发现与快进升级验证，源工程不进入安装目录。
