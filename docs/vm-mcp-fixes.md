# 隔离 Windows VM 的 MCP 修复说明

本分支 `vm-sw-automation-skill` 基于上游提交
`f9d6efbecf297bcdfffc451de81ba3a7bdea6a0d`，记录远端 SolidWorks 2025 SP0
验证发现的文件读取和工程图问题。本文不包含具体部署环境的凭据、地址或会话数据。

## 修复范围

### 二进制产物读取

`ArtifactReadInput` 没有 `response_format` 字段，但原工具在成功和异常路径均访问
该字段。`cadstudio_read_artifact` 现在固定返回 JSON，其中二进制数据保持 Base64
编码，沿用现有偏移量、分块大小和哈希校验协议。

真实 HTTP 验证：15,779 字节的 STEP 文件分 4 块读取，SHA-256 与服务端记录及
`resources/read` 返回内容一致。

### 工程图纸张、布局和预览

- 修正 A4、A3、A2、A1、A0 的纸张枚举；新增页默认值明确使用 A2 常量。
- 优先通过 `ISheet.GetProperties2` / `GetProperties` 读取实际页宽高，并兼容
  `GetSize` 的返回形式。布局、标题栏区域及 PDF 尺寸匹配使用相同实际尺寸。
  读取失败时才使用纸张预设值，并标记回退来源。
- 对工程图采用 PDF 页面渲染生成 PNG，不再将三维标准相机视角应用到二维图纸。
  预览长边限制 2400 像素，并支持 PNG 空白检查。
- PDF 生成失败时，生成和检查工具跳过桌面视口回退，明确阻塞交付。

远端验证：实际 A4 纵向页面为 210×297 mm，6/6 尺寸文字匹配到 PDF；PNG 预览
可通过 MCP 图片内容块取回。这些检查不等于工程图视觉完整性或制造可用性验收。

### PDF 导出与产物状态

- 指定图纸的 `SetSheets` 枚举由 0 改为 3；名称通过 COM BSTR 数组传递，并检查
  图纸选择调用是否成功。
- PDF 导出要求 `SaveAs` 返回成功、错误码为 0、非空输出确实更新且可解析。
- 导出失败时，不把既有 PDF 报告为本次成功产物。
- 工程图检查成功时使用既有产物登记机制识别的 `outputs.pdf.path`，支持后续下载。

## 已知未解决项

在验证环境中，原生 `SaveAs` 仍返回 `False / Errors=1 / Warnings=0`。生成的
诊断 PDF 虽可解析和提取尺寸文字，仍缺少尺寸线及图框线，不可视为合格工程图。
编译的 C# 官方 interop 调用也复现该返回值，因此该问题并非仅发生于 Python 或
MCP 调用层。

已对照活动文档、view-only/只读状态、图纸编辑状态、打印机、保存后打开选项、
SaveAs2 及若干 PDF 导出选项，尚未确定根因。图形配置调整及 SW 重启对照未执行。

生成和检查工具在原生导出失败时应返回 `blocked` /
`DRAWING_FINAL_PDF_REQUIRED`；不得将“文件存在”或“6/6 文字匹配”当作导出成功。

## 回归验证

针对性测试共 93 项通过：

```powershell
python -m pytest `
  tests/test_mcp_artifact_access.py `
  tests/test_mcp_http_transport.py `
  tests/test_drawing_pdf_preview.py `
  tests/test_drawing_pdf_export.py `
  tests/test_drawing_sheet_dimensions.py `
  tests/test_mcp_drawing_tools.py `
  tests/test_drawing_subskill.py `
  tests/test_sw_drawing_structure.py `
  tests/test_drawing_legacy_api.py -q
```

覆盖真实 HTTP 分块及哈希、无效/空白 PDF、导出 API 失败、旧文件误报、纵向纸张、
失败时不调用视口预览，以及 MCP 输出契约。测试依赖使用项目现有依赖及 pytest，
本分支没有新增运行时依赖。

## 隔离部署边界

本分支不修改 VM 防火墙、路由、DNS、端口发布或宿主守卫。建议通过受控管理通道
传入离线安装包，使用 SSH 隧道访问宿主回环发布的 MCP 服务；继续保留 Bearer
认证及 Host/Origin 校验，不为安装或运行工具开放 VM 出站网络。

部署时备份上述三个运行文件，校验补丁及目标文件哈希，仅重启 MCP。SolidWorks
COM 仍需运行在已登录的交互用户会话中；SW 会话重启和图形配置调整应另行安排。
