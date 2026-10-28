---
name: solidworks-3d-2d
description: "将 SolidWorks 零件或工程目录中的 3D 模型转换为带模型尺寸的二维工程图草稿，输出 SLDDRW、PDF 和审查报告；支持独立安装、检查版本及更新。用于 SolidWorks 3D 转 2D、批量零件出图和出图结果复核。"
---

# SolidWorks 3D 转 2D

这是独立 Skill，代码和运行依赖均在本目录，不需要完整 solidworks-automation 或通用 MCP 服务。执行主机必须是已安装并授权 SolidWorks 的 Windows，原生操作须处于可用的交互桌面会话。若模型在 VM，须在 VM 的该会话中运行；本机安装 Skill 并不等于能直接操作远端 COM。

## 使用入口

先用安装目录中的 `.venv/Scripts/python.exe scripts/solidworks_3d_2d.py doctor` 检查依赖与 SolidWorks；不存在 `.venv` 时按 [README](README.md) 安装依赖。执行脚本时使用绝对路径，或把工作目录设为 Skill 安装目录。

- `inventory --source <工程目录或零件> --output <新清单.json>`：不打开 CAD，核对文件、重复内容、同名版本及 ZIP 来源。
- `convert --source <零件.SLDPRT或工程目录> --drawing-template <.drwdot> --sheet-format <.slddrt>`：实际出图。先运行 `convert --help` 查看当前参数。默认工程下 `agent/solidworks-3d-2d/<run-id>`；原生图和 PDF 文件名加 `_ai`。
- `--configuration` 必须用模型中真实存在的名字，不能把中文“默认”猜成 `Default`。目录批次的配置名会应用到所有选中模型，名字不一致应分批。
- 默认只对零件出图；装配工程图需 `--include-assemblies --bom-template <.sldbomtbt>`。模板路径应来自本机 SolidWorks 或用户提供的模板；不猜测不存在的路径。
- 高级需求可用 `--spec <DrawingSpec.json>`，结构见 [DrawingSpec schema](subskills/solidworks-engineering-drawing/schemas/drawing_spec.schema.json)。剖视、局部图、复杂孔表/GD&T 等未实现字段须按能力门禁报告，不能静默忽略。

先读取用户源路径和已有工程约定，能从实际工程获取的信息不重复询问。缺少真实模板、配置或制造语义时，说明具体缺项；不要临时重写另一个出图脚本来跳过门禁。

## 必须保留的出图边界

- 全程使用同会话 CAD 作业锁；遇到占用不得杀掉 SLDWORKS 或别人的任务。
- 只读打开源文件、不保存源模型，只关闭本轮拥有的文档。配置切换需回读且恢复既有状态，尺寸来自真实模型包围盒与模型尺寸。
- 输出目录属于单次运行，已有目录不覆盖；当前独立 CLI 不承诺断点续跑。保留每次运行的报告、账本与依赖，不把历史无归属文件当成可删除 scratch。
- CLI 不直接接受 STEP；先用 SolidWorks 导入保存 SLDPRT。无可导入尺寸的纯几何模型不会自动得到完整制造标注。只生成视图也不能称作已完成的标注图。
- 保存、PDF 导出、可读性和制造完整性分别报告。原生导出错误即使留下 PDF 也不能通过；图纸始终保留工程终审要求。
- 检查最终 PDF 预览中的尺寸重叠、跨图和裁切。密集尺寸可使用已保留的坐标附表模块，但只转移有真实 DXF 定义点、唯一匹配且数值通过校验的尺寸。不能为了看起来干净就隐藏未映射尺寸。

## 用户要求更新时

安装来源固定为 `https://github.com/TuKJet/solidworks-automation-skill.git` 的 `vm-sw-3d-2d` 分支。使用本 Skill 自带更新器，不自行删除重装，也不修改其他 Skill 或 MCP 配置。

```powershell
py -3 "<SKILL_DIR>\scripts\skill_manager.py" check --target "<SKILL_DIR>"
py -3 "<SKILL_DIR>\scripts\skill_manager.py" update --target "<SKILL_DIR>" --dependencies
```

“更新这个 Skill 到 GitHub 最新版本”已授权执行更新；只有用户说“检查更新”时只执行 check。更新前确认没有本 Skill 的转换任务正在运行；运行中先完成该任务。遇到 dirty、分支/仓库不符、非快进、依赖安装失败时，按实际错误报告并保留用户改动，不用强制 reset 绕过。记录 before/after commit，重新读取更新后的 SKILL.md，再运行 doctor。网络失败可使用用户已有的有效代理，仅作用于当前命令。

安装和完整示例见 [README](README.md)，实现来源与验证边界见 [架构与验证](references/architecture.md)。