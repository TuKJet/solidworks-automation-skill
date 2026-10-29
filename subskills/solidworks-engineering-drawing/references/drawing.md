# 工程图生成参考

## 标准

GB/T 路线默认第一角投影：俯视位于主视下方，右视位于主视左侧。SolidWorks 的
`SetupSheet6` / `NewSheet4` 投影参数必须和布局保持一致，并在生成后回读。

## 证据要求

- 视图对象、方向、位置和比例必须回读。
- `InsertModelAnnotations3/4` 返回值不能单独作为尺寸已插入证据，必须再次读取真实 `DisplayDimension`。
- 图框文件名只代表候选证据，不能证明模板内容符合 GB/T。
- PDF/BMP 输出必须存在且非空；PDF 文字边界只能用于风险筛查。
- `convert` 默认生成 DXF，除非 `spec.outputs.dxf` 为 `false`。DXF 必须核对原生保存返回值、输出新鲜度/非空和实体计数，并同时检查顶层实体与块内实体中的标注；导出时可临时关闭 `swDxfExportViewAsBlock`，但必须恢复原偏好。
- 复杂布局最多进行 3 次有界尝试；候选只在尺寸身份、数值和可见性与原始结构完全一致，且新鲜 PDF 的真实审查指标改善时采用。比例必须在移动前后回读一致。
- 批次共享同一 SolidWorks 会话，但每项完成后必须清理本轮文档；清理失败应停止后续项，不能带着旧文档、配置或引用进入下一件。

## API 规则

新增或未封装的 SolidWorks API 先查 `references/api-lookup.md` 和官方 API Help，确认签名、
枚举、返回值和版本差异后再实现。COM 返回 `None` 或 `False` 时返回明确状态，不伪造成功。

