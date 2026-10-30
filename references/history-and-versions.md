# 标题台账与表格版本

使用`scripts/listing_state.py`读取历史、查重和登记交付。业务状态保存在用户指定的批次或店铺台账目录，不放技能安装目录、源码或GitHub。台账表示已制作工作簿的标题占用，不代表已经发布商品；此工具不运行精制、不发布、不改Excel。

## 标题范围

同店新链接的标题跨日期、跨商品和批次查重，不只检查当前工作簿。先取得现成台账及用户指定历史上架表的范围；可只扫描指定目录当前层的`*.xlsx`，不递归猜测所有硬盘。不同店铺分别统计，`~$`临时锁文件跳过；其余坏表、未知表头或不可访问文件明报，不悄悄当作零历史。

读取SPU真实“店铺Uid*”“标题*”表头并筛选确认的UID，按Unicode NFKC、去除Cf不可见格式字符及首尾空白后比较；可见标点和内部普通空格保留。新标题拒绝Cf/Cc字符，不能靠零宽字符制造“不同”标题。批内另由`business.validate_titles`验证26～30汉字、总字符≤30及无首尾空白。脚本不能证明商品相关性、自然度或功效真实性，这些由AI依据实际素材和用户资料审阅。

```text
python scripts/listing_state.py scan --store-uid S123 --directory 历史表目录
python scripts/listing_state.py check --store-uid S123 --state 业务目录/上架状态.json --batch-id 本批标识 --titles 新标题.json --workbook 历史表.xlsx
```

`S123`等均为示例；新标题JSON是字符串数组。首次没有台账可显式给出空历史，但交付要写明检查范围，不能保证未知旧表或真实平台上的标题不重复。旧格式台账不要改字段冒充本工具schema；先保留原件，按实际历史工作簿重新导入核验。

新批制表的settings包含：

```json
{
  "title_history": {
    "state_path": "业务目录/上架状态.json",
    "workbooks": ["历史上架表.xlsx"],
    "batch_id": "本批明确标识"
  }
}
```

生产使用真实绝对路径。`business.make_rows`只读这些数据并阻止冲突；它不在生成job时写台账。旧settings未提供此字段仍兼容，但只能证明批内唯一。

## 从最新版继续改

同批改标题、SKU名称、草稿等字段时，先显式解析本批最新指针：

```text
python scripts/listing_state.py resolve --state 业务目录/上架状态.json --store-uid S123 --batch-id 本批标识
```

返回实际工作簿`path/sha256`、父版本、标题及核验报告路径/哈希。解析时重新读取工作簿和报告，文件被移动、改写、UID不符或证据过期即停止，不按文件名或修改时间猜“最新”。用户明确指定源表时以其要求为准；存在同批后续版本时先核对差异，不能在旧基底上意外丢掉已完成字段。

使用返回文件作为patch的`source`，按实际表头定位指定列，并填`expected`原值；另存明确新版。原件、每个已登记版本和相应verification报告独立保留，不能用同一路径覆盖后再拿旧SHA继续。只有明确授权才原地改业务表；已登记链的原地改写需要新的版本处理，不能绕过漂移检查。

同批升级的历史检查及登记都带当前`parent_sha256`，只允许当前父版本中同一行、同一真实链接编号继续沿用同一标题。登记及解析强制读取SPU的唯一“链接序号”表头，每个编号必须为不同的正整数；布尔值、文本和公式均拒绝。业务构造器从实际manifest传`link_ids`，CLI预检通过`--link-ids 编号数组.json`提供；没有身份信息时不能豁免历史标题。过期父版本、退休标题再用、编号换成新链接或标题互换均拒绝。不要换一个`batch_id`来绕过冲突；此工具不自动推断重排或合并关系。

## 成功后显式登记

先完成[表格执行接口](workbook-io.md)三阶段：Artifact写中间表 → 原生保真落盘 → Artifact `--verify`重开最终表、核对值并查看预览。最后一步成功才在`verification.json`写`artifact_verified=true`及当前`artifact_sha256`；只有`preserve_xlsx.py --finish`报告还不能登记。

```text
python scripts/listing_state.py register --state 业务目录/上架状态.json --store-uid S123 --batch-id 本批标识 --workbook 新版.xlsx --verification 本版核验目录/verification.json
```

升级已有批次追加`--parent-sha256 当前父版本SHA`；需要外部历史时用可重复的`--history-workbook`传入。不要把将登记的新输出本身当作历史表。登记会再次读回实际UID和标题、重算工作簿及报告SHA、在排它锁内重查标题和父版本后，原子提交**同一JSON中的标题占用与最新指针**。

新旧标题都保留历史占用。锁冲突、陈旧父版本、文件漂移、坏JSON、输出验证缺失均拒绝更新，保留已生成的工作簿和旧状态；不得宣称“登记完成”，也不自动删除不明锁。并行任务不能仅凭早先check通过就登记相同标题。状态提交失败后修复原因并重新核验登记，不重建或覆盖业务成果。

登记是版本/标题门禁，不代替人工预览、全量单元格保真比较、素材核验或真实精制试套。
