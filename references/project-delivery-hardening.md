# 工程批量交付的实施与回归约定

本轮先稳定 1–6，再集成 7–12。复用现有工作流；项目路径、零件选择和模板放在回归配置，不能写进通用算法。

| 顺序 | 能力 | 验收 |
|---|---|---|
| 1 | 文档上下文与归属 | 已打开文件也明确激活；同名异路径不混用；不关闭用户文档 |
| 2 | 图框替换 | sheet/view 比例、继承关系、投影保持；旧格式注释策略显式 |
| 3 | 尺寸身份与单位 | 模型/config/尺寸身份；可见、隐藏、未知分开；长度与角度不混算 |
| 4 | 注释写入更新 | 清选择、文本/位置回读；换行兼容；最终 PDF 保留 |
| 5 | 布局与有界修复 | 包含注释/标题栏/纸张；约束轴不乱移；无进展停止 |
| 6 | 统一验收 | 导出成功、可读性、制造完整性分层；失败不降级为通过 |
| 7 | 工程清单 | 递归、ZIP 安全解包、内容去重、同名版本、来源 |
| 8 | 批量与续跑 | 输入/策略/输出 hash；生成/复核/发布状态；重复错误停机 |
| 9 | 密集尺寸 | 可追溯坐标/孔表分页；不硬编码零件或 RD 编号 |
| 10 | 产物生命周期 | owner/hash/dependencies/retention；清理预览和逐文件保护；批次单报告 |
| 11 | 依赖打包 | 复用 PackAndGo；重开核验引用；临时引用不能当自包含交付 |
| 12 | BOM | 层级/parent/depth/quantity_scope/异常；多种输出同一数据源 |

回归工程：`2D and 3D-20260817/2D and 3D`，66 个原始文件、24 个不同零件版本、9 个装配。
每轮先记录当前源文件 SHA，再证明运行前后不变；相对旧清单的既有变化单列，不能抹掉或误称本轮写入。新回归证据写到独立 run 目录；已有交付与临时依赖在证明可替换前保留。
SW2025 实测不能升级为 SW2026 已验证。几何可读不等于制造完整；公差、材料、基准等未知项保留复核。

## 已落地的批量交付加固入口

- `apps/desktop/cad_workbench/artifact_ledger.py:RunLedger`：登记 run owner、root、policy、产物 SHA、角色、依赖和 retention；checkpoint 与 cleanup 均做路径/owner/hash 复核。scratch 清理默认 dry-run，且只处理本 run 创建、未被 final/evidence 传递依赖保护的单文件。
- `scripts/sw_project_batch.py:run_batch`：按 source/dependency/policy fingerprint 续跑；分别记录 generated、verified、published；每件完成后原子更新 `batch_report.json`。生成、复核或可读性失败不得直接发布；相同错误连续两次停止批次。
- `scripts/sw_delivery.py:collect_assembly_bom` 与 `export_bom_bundle`：flat 模式明确 `flat_aggregate_all_levels`，hierarchy 模式保留 parent/depth/component_type/localqty 和异常标志；CSV、XLSX、JSON、HTML 使用同一行数据。
- `tests/solidworks_project_regression.py`：真实工程回归入口，读取 VM manifest；可用 `--config`、`--output-dir`、`--limit`、`--validate-config`。它不替代人工工程终审，也不把可读性证据升级为制造通过。

本地回归命令（不启动 VM 的部分）：

```powershell
D:\work\cad-agent\deployment\winvm01-mcp-20261026\client-venv\Scripts\python.exe -m pytest solidworks-automation-skill/tests/test_artifact_lifecycle.py --basetemp D:\work\cad-agent\.pytest-tmp-ledger
D:\work\cad-agent\deployment\winvm01-mcp-20261026\client-venv\Scripts\python.exe -m py_compile solidworks-automation-skill/apps/desktop/cad_workbench/artifact_ledger.py solidworks-automation-skill/scripts/sw_project_batch.py
```

限制：本地测试覆盖的是路径、hash、断点、依赖闭包和静态证据合同；未在此文档中宣称任意 SolidWorks 版本的 native PDF、COM 引用重开或制造图纸通过。真实 VM 回归仍需使用项目配置中的 manifest，并分别报告 functional、drawing review 和 manufacturing review 状态。

## 执行规则与可复用入口

- 同一个 SolidWorks 桌面一次只运行一个 COM 作业。先检查后台 CAD 定时任务；禁止为了抢占而杀掉 SLDWORKS。调用者停止自己的作业后保留源文件与未确认归属的文档。
- 文件系统清单不猜配置：`configuration_inventory.status=not_inspected`。原生配置由已有配置读取器或实际视图的 `ReferencedConfiguration` 提供，不能把中文“默认”自动替换成 `Default`。
- `setup_current_sheet(..., embedded_format_path=...)` 在旧外部图框路径失效、已有视图且明确要求保留时，用 `save_current_sheet_format` 导出图内格式。输出必须是新路径；这只证明格式保留，不证明 GB/T 内容合格。
- `repair_native_drawing_layout` 使用已有有界规划器、原生视图关系、位置回读和每次新 PDF；无真实文字框、轴向关系不明或导出失败即停止。MCP `solidworks_generate_drawing(repair_layout=true)` 可显式调用；没有安全移动方案属于待审。
- `contract_from_native_projection` 从真实 DXF 的 DIMENSION 定义点匹配尺寸并验证比例和值；只接受有唯一匹配的线性项。`generate_coordinate_appendices` 统一生成原始矢量轮廓、去重点号、跨页唯一行号、CSV 与 manifest 映射。不得以标签位置代替测量端点。
- `relocate_copied_references` 只修改包内、已关闭的副本；替代模型必须与原件 hash 相同。API 成功仍是 pending reopen，必须把重开后的真实引用交给 `validate_packaged_references`。
- `RunLedger` 的 final/evidence 及其传递依赖均保留；scratch 默认只预览清理。旧的无账本文件不能追认成“本 run 创建”后删除。受管函数内部临时文件必须由上下文管理器释放，不另造散落的工程脚本。

原生回归统一入口（在已登录的 SolidWorks 桌面任务中执行）：

```powershell
python tests/solidworks_project_regression.py --config tests/fixtures/project_2d3d_20260817.json --stage all --output-dir "C:\Work\2D and 3D-20260817\2D and 3D\agent\skill回归_ai\RUN_ID"
python tests/solidworks_dense_regression.py --config tests/fixtures/project_2d3d_20260817.json --input-root C:\cad-output\2D_3D_ai_20261028 --output "C:\Work\2D and 3D-20260817\2D and 3D\agent\skill回归_ai\DENSE_RUN_ID"
```

单件复测使用 `--part-id`，后半流程使用 `--stage auxiliary`。真实工程路径只放 fixture；脚本不依赖具体零件号。目录内 `.run-owner.json` 与 `*.run-ledger.json` 记录本轮归属；`batch_report.json` 是续跑状态，最终报告分别记录源文件 hash、原生 API、可读性、制造完整性，不能互相替代。
