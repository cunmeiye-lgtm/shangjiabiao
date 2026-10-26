# 上架表读写与保真

业务代码先产生完整的 SPU/SKU 数据。这里仅负责单元格写入和原模板保真，不决定标题、价格、店铺或 SKU 组合。每次开始需读取当前 spreadsheets 技能；使用其 loader 返回的 Node/Python 和依赖，遵守本轮 marker、渲染、核验要求。不得安装替代依赖或用 openpyxl 写工作簿。

## 输入 JSON

公共字段：`source`、`output` 是明确绝对路径；`headers` 是受影响工作表的完整第 1 行值数组，必须先从实际源表读取并与已确认字段意义核对。不能先猜列号再机械抄表头。默认只写全新输出文件。

批量：

```json
{
  "source": "源表绝对路径.xlsx",
  "output": "新表绝对路径.xlsx",
  "operation": "batch",
  "headers": {"SPU": ["实际43列标题"], "SKU": ["实际10列标题"]},
  "sheets": {
    "SPU": {"rows": [["完整43列值"]], "seed_rows": [2]},
    "SKU": {"rows": [["完整10列值"]], "seed_rows": [2,3,4,5,6]}
  },
  "previews": [{"sheet": "SKU", "range": "A1:J6"}]
}
```

以上数组是结构示意，必须填完整真实数组。数据从第 2 行开始，整体替换指定工作表全部数据行；不可用于混有尾部说明的小表。只读原始表的说明页及其他工作表保持原样。模板行循环提供格式、行高等。空值用 `null`，库存/价格用数字，编码/UID/路径用字符串。脚本拒绝写入公式；这些上架表数据不需要公式。

指定字段修改：

```json
{
  "source": "源表绝对路径.xlsx",
  "output": "修改版绝对路径.xlsx",
  "operation": "patch",
  "headers": {"SPU": ["已核对的完整表头数组"]},
  "changes": [{"sheet": "SPU", "cell": "S2", "expected": 0, "value": 1}],
  "previews": [{"sheet": "SPU", "range": "R1:T5"}]
}
```

`S2` 只是示例，必须依据实际表头定位“保存草稿箱”；不得把 S 列作为跨模板常量。`expected` 建议始终填写，值或公式必须与原表一致。重复地址、未知工作表、表头变化、合并数据单元格等均拒绝；不会替用户修正其他字段。

明确授权原地修改时才添加 `replace_input: true`，让 `output` 等于 `source`，同时提供当前文件 SHA-256 `source_sha256` 和未存在的备份路径 `backup`。源文件在快照后变化会停止。默认不关闭 Excel/WPS，不改用户打开状态。原地替换遇到占用时默认报错；若用户已经允许另存，可提供全新 `fallback_output`。不会覆盖同名旧成果。

## 执行

在任务自己的暂存目录创建 `node_modules` junction/symlink，指向 loader 的 node_modules。技能目录不放 node_modules，也不修改 runtime。路径变量来自 loader，不写死到技能。`$taskScratch` 必须是本次专用新目录；`$config` 是输入 JSON。

```powershell
& $node $skillRoot/scripts/workbook.mjs $config --work-dir $taskScratch --python $python
& $python $skillRoot/scripts/preserve_xlsx.py --finish "$taskScratch/prepared.json"
& $node $skillRoot/scripts/workbook.mjs $config --work-dir $taskScratch --python $python --verify
```

首条命令先验证源表并保存快照，再经 Artifact Tool 写出 `authored.xlsx`；第二条只把已核验的 Artifact 值放回原生 ZIP 包；第三条用 Artifact 重新打开最终文件、检查请求值并渲染。查看 before/authored/final 预览；只交付最终输出和用户需要的核验记录，暂存中间表不当作完成品。不要省略第二、三步。

## 保留范围与停止条件

- 除指定 worksheet XML 外，所有原始 ZIP 部件逐字节保留，包括样式、批注、说明页和关系；窄修改保留同页其他单元格、公式及设置。
- 批量仅重建指定表的第 2 行起数据和 dimension；使用原有 seed 样式，原先没有单元格节点的新数据也会写入。源表变化、Artifact 回读不一致会停止，避免发布错误表。
- 批量遇到数据行批注、原生表、筛选器、绘图或超链接等随行移动的对象时拒绝自动变换，交给明确的迁移处理；合并数据单元格和签名工作簿也拒绝。不要删掉对象来绕过。
- 对其他工作表的公式不重新缓存，保留其原始公式/缓存。上架表数据如有影响这些公式或名称/验证范围的额外依赖，须先评估，并在专门授权范围内处理；此适配器不会默默扩大修改。
- `verification.json` 记录实际输出路径、哈希、修改单元格数和非修改部件保留结果。最终文件被用户应用打开并再次保存后哈希可能变化，不能拿旧证据冒充当前核验。

## 测试

使用 bundled Python：`python -m unittest discover -s <skillRoot>/tests -p test_preserve_xlsx.py`。测试在临时目录生成最小 OOXML 固件，不接触生产表；真实 Artifact 集成验证使用源表快照和隔离目录。

运行真实集成测试时设置环境变量 `JINGZHI_INTEGRATION_SOURCE` 为已检查的标准上架表只读快照，`JINGZHI_NODE` 为 loader 的 Node，`JINGZHI_INTEGRATION_WORKDIR` 为专用暂存父目录（其中已建 runtime 的 node_modules junction）。执行同一 unittest 命令，会增加 Artifact 字段修改和 2 SPU／10 SKU 批量集成场景；原源表哈希必须不变。该场景需完整 SPU 43 列、SKU 10 列且有足够模板数据行。不设置变量时仅运行保真层单元测试。
