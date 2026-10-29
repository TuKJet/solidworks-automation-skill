# 提取范围与验证

2026-10-29 原生回归（SolidWorks 2025 / Python 3.12）：24 个零件版本均保存 SLDDRW、PDF、DXF；21 件有可导入模型尺寸，共 420 个，DXF 顶层 DIMENSION 数逐件一致。10 件选用了更好的布局，PDF 文字重叠总数从 32 降至 3；2 件仍未通过可读性门禁，另 3 件无模型尺寸。STEP 导出覆盖 24 个零件、8 个装配体，另 1 个装配体在批次及独立只读重试中均报告 dirty，保持阻断。721 个源文件本轮哈希未变。FreeCAD 实际导入未执行。回归测试 280 passed、2 skipped；独立包不含的两项 MCP 服务测试显式排除。

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
