# SolidWorks 3D → 2D Skill

可独立安装的 Agent Skill：读取 SolidWorks 零件或工程目录，生成带可导入模型尺寸的二维工程图草稿（`SLDDRW`、`PDF`）和审查报告。支持自然语言调用与 GitHub 更新。

二维出图默认同时导出可供 FreeCAD 导入的 `DXF`；`export-step` 可把现有零件和装配体导出为三维 STEP。交换文件保留几何，不保证 SolidWorks 参数树、装配配合或尺寸关联在 FreeCAD 中可恢复。

**独立分支：[`vm-sw-3d-2d`](https://github.com/TuKJet/solidworks-automation-skill/tree/vm-sw-3d-2d)**。完整 CAD 自动化工具包仍在 `vm-sw-automation-skill`；本分支不包含桌面应用、建模/仿真模块或通用 MCP 服务。

## 运行条件

- Windows 10/11，已安装且可正常使用的 SolidWorks；需要可用的交互桌面会话。
- Python 3.10+、Git。建议使用官方 Python 的 `py -3` 启动器。
- 能读取工程的原生模型、引用文件及工程模板。
- `.drwdot` 是工程图文档模板，`.slddrt` 是图纸格式，二者不同。使用本机 SolidWorks 或公司提供的真实文件；本仓库不分发商业图框。
- 依赖安装在独立 `.venv`，不修改其他 Skill、MCP 或系统 Python 环境。

核心库有 SolidWorks 2025 工程回归证据；本包属于 pilot。导出成功不代表制造图纸已经终审。

## 安装

在 PowerShell 中执行：

```powershell
git clone --depth 1 --single-branch --branch vm-sw-3d-2d https://github.com/TuKJet/solidworks-automation-skill.git solidworks-3d-2d-source
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\solidworks-3d-2d-source\install.ps1 -Dependencies
```

安装器默认写入 `$env:CODEX_HOME\skills\solidworks-3d-2d`；未设置 `CODEX_HOME` 时写入 `$HOME\.codex\skills\solidworks-3d-2d`。`ExecutionPolicy Bypass` 只作用于该安装进程。安装目录已存在时拒绝覆盖，请使用更新命令。

其他兼容 Skill 的 Agent 可以指定自己的 skills 目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\solidworks-3d-2d-source\install.ps1 -Target "$HOME\.agents\skills\solidworks-3d-2d" -Dependencies
```

不想使用 PowerShell 包装器时，可直接执行：

```powershell
py -3 .\solidworks-3d-2d-source\scripts\skill_manager.py install --dependencies
```

完成后让 Agent 重新发现 Skill（必要时新开会话）。可把这个仓库分支链接和上述安装说明交给 Agent，直接说“把这个独立 Skill 安装到我的 skills 目录”。源下载目录只用于安装，不是出图工作目录。

## 自然语言使用

安装后可直接说：

> 使用 $solidworks-3d-2d，把 C:\Work\我的工程 里的 SolidWorks 零件转为二维标注图，结果放在工程的 agent 子目录。使用我提供的 A3 文档模板和图纸格式；列出未完成标注和需要复核的项。

> 用 solidworks-3d-2d 给这个零件出图，使用配置“默认”，保留三视图并检查 PDF 中的尺寸是否重叠。

Agent 会读取本 Skill 的 `SKILL.md`，检查环境、源文件与模板，然后调用脚本。模型没有可导入的设计尺寸、缺引用或需要人工确定公差时，报告会保留缺项，不会编造数值。

## 直接运行

设置安装目录（自定义安装请改成实际位置）：

```powershell
$skillsBase = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$skill = Join-Path $skillsBase 'skills\solidworks-3d-2d'
$python = Join-Path $skill '.venv\Scripts\python.exe'
& $python "$skill\scripts\solidworks_3d_2d.py" doctor
& $python "$skill\scripts\solidworks_3d_2d.py" convert --help
```

先列清单（不打开 CAD；输出文件使用新路径）：

```powershell
& $python "$skill\scripts\solidworks_3d_2d.py" inventory --source 'C:\Work\我的工程' --output 'C:\Work\我的工程\agent\inventory_ai.json'
```

批量出图；把模板路径改为本机实际路径：

```powershell
& $python "$skill\scripts\solidworks_3d_2d.py" convert `
  --source 'C:\Work\我的工程' `
  --drawing-template 'C:\Templates\A3.drwdot' `
  --sheet-format 'C:\Templates\a3 - gb.slddrt' `
  --paper A3 --standard GB_T
```

GB_T 模式的图框候选筛选要求文件名包含对应图幅（如 A3）及 `gb` / `gbt` / `国标` 标识。请使用真实匹配的图框，不要只改文件名伪装合规；名称匹配也不等于图框内容已通过标准审查。

`--source` 也可指定单个 `.SLDPRT`。`--configuration` 使用真实配置名；省略时记录模型实际活动配置。`--include-assemblies --bom-template <真实.sldbomtbt>` 增加装配图。更详细的 DrawingSpec 见 [schema](subskills/solidworks-engineering-drawing/schemas/drawing_spec.schema.json)。

默认输出到源工程 `agent/solidworks-3d-2d/<run-id>`，不同零件分目录保存，文件名带 `_ai`。`--output` 可指定一个全新目录；已有目录拒绝覆盖，本版 CLI 不提供断点续跑。不要把输出放进 Skill 安装目录，以免影响更新。报告明确区分保存、PDF 导出、可读性与制造完整性；`review_required` 需要工程复核，`blocked/failed` 不是可交付成功。

### ZIP、密集尺寸及边界

默认最多尝试三次布局；只有尺寸身份、值、可见性及所属视图保持一致，且真实 PDF 文字检查改善时才采用候选。图框比例和每个视图比例均回读核对。`--layout-attempts 1` 可关闭重排。重叠和无法唯一核对的尺寸仍列为复核项，不通过隐藏尺寸消除问题。

DXF 导出会临时关闭“视图作为块”并恢复原偏好，记录原生错误、单位字段和顶层标注实体。建议使用 FreeCAD 内置 C++ DXF 导入器；文件结构检查不等于已经在 FreeCAD 中导入验收。可通过 DrawingSpec 的 `outputs.dxf: false` 禁用 DXF。

清单包含 ZIP 中的模型与来源，原始 ZIP 不会被改写。需要包含 ZIP 内的零件时，在上述 convert 命令中加 `--extract-zips`：解包到本次输出目录的 `sources_ai`，保留相对引用；默认不解包，独有档案模型列为未转换并使总状态待复核。CLI 不直接接受 STEP；应先在 SolidWorks 导入并保存成 SLDPRT，纯几何仍可能没有可导入的设计尺寸。

标准三视图、模型尺寸导入、图框保留和模板化装配 BOM 复用原库。复杂剖面/局部视图、GD&T、孔表和制造基准仍受能力门禁限制；未实现需求会返回明确状态。密集线性尺寸可通过保留的 `drawing_tables.py` 生成可追溯坐标附表，这是高级 API，并非默认每件自动改成坐标表。需要时安装附表依赖：

```powershell
& $python -m pip install -r "$skill\requirements-dense.txt"
```

## 导出三维 STEP

```powershell
& $python "$skill\scripts\solidworks_3d_2d.py" export-step `
  --source 'C:\Work\我的工程' --extract-zips `
  --output 'C:\Work\step-review-01'
```

源路径可以是 `.SLDPRT`、`.SLDASM` 或工程目录；无需工程图模板。默认使用各模型当前配置，指定 `--configuration` 时必须是真实配置名。导出以只读方式打开源文件，检查原生保存结果、STEP 结构、配置恢复及源文件哈希。装配缺失、轻量化或抑制引用会记录复核项；重复零件及 ZIP 来源在清单中保留。FreeCAD 未实际导入验证时，批次保持 `review_required`。

## 用自然语言更新

对 Agent 说：

> 检查 solidworks-3d-2d 是否有更新。

或：

> 把 solidworks-3d-2d 更新到 GitHub 上的最新版本，保留我的工程文件，完成后检查安装状态。

Agent 会使用自带更新器，固定跟踪这个仓库的 `vm-sw-3d-2d` 分支：

```powershell
py -3 "$skill\scripts\skill_manager.py" check --target $skill
py -3 "$skill\scripts\skill_manager.py" update --target $skill --dependencies
& $python "$skill\scripts\solidworks_3d_2d.py" doctor
```

- `check` 获取远端 commit 并报告 `update_available`，不改变安装的代码。
- `update` 只接受 fast-forward；核对安装记录、origin、分支、HEAD 和工作区。
- 本地改动、非快进历史或来源不匹配会中止，不删除重装、不覆盖用户改动。
- 新代码验证失败会尝试恢复原 commit；遇到并发编辑时停止自动回滚并说明原因。
- 依赖安装失败返回 `partial/dependencies_failed`，保留已安装代码；网络恢复后重试 `update --dependencies`。
- 更新前完成正在运行的出图任务。不要同时修改 Skill 或运行两个更新器。
- 若 Git/Python 网络请求失败，使用用户已有的有效代理；不永久更改系统代理或绕过证书校验。

## 维护与验证

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m pytest tests -k 'not test_run_locked'
```

两项 `test_run_locked*` 是继承的完整 MCP 服务测试，独立包不包含该服务，故显式排除；原生跨进程锁测试继续运行。测试不等于对所有 SolidWorks 版本的制造终审。实现来源、提取范围与当前验证证据见 [架构与验证](references/architecture.md)。

MIT，保留原项目版权与许可，见 [LICENSE](LICENSE)。