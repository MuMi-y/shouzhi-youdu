# MoneyData 原始数据与署名

## 验收标准

原件可离线打开；来源、版本、许可和哈希可查；派生文件与原件分开；不得把公开数据当作本项目采集的数据。

## 来源与许可

- 数据发布者：Robert Laramee，University of Nottingham。
- 数据集：Bank Transactions Dataset，Mendeley Data，V1，2026-01-14 发布。
- 数据引用：Laramee, Robert (2026), “Bank Transactions Dataset”, Mendeley Data, V1, doi: 10.17632/dnxtg6n4rv.1。
- [数据发布页](https://data.mendeley.com/datasets/dnxtg6n4rv/1)
- [数据 DOI](https://doi.org/10.17632/dnxtg6n4rv.1)
- 发布页标示许可：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。使用和分享时保留署名、许可链接，并注明修改；不暗示作者认可本参赛项目。
- 论文：Elif E. Firat, Dharmateja Vytla, Navya Vasudeva Singh, Zhuoqun Jiang, Robert S. Laramee. *MoneyVis: Open Bank Transaction Data for Visualization and Beyond*. EuroVis 2023 Short Papers. [DOI](https://doi.org/10.2312/evs.20231052)。
- [作者提供的论文](https://people.cs.nott.ac.uk/blaramee/research/financeVis/firat23moneyVis.pdf)
- 获取日期：2026-09-29（北京时间）。

## 文件状态

原件：[laramee26openBankTransactionData.xlsx](laramee26openBankTransactionData.xlsx)，507,389 字节。下载后没有修改，SHA-256 与发布方文件元数据一致：

`716eaf9ad4a3cf60dc9c9c8a20ed9a917c545a1b5f139f40ed91d2104faa3ff7`

[下载元数据](source_files_metadata.json)保留发布方文件 ID、下载 URL、体积和哈希。[原件检查](profile.json)描述原始存储状态，其中日期范围不是已修复的交易时间范围。

## 已发现并核实的日期问题

日期列混有 3,751 个日/月/年文本值和 2,816 个 Excel 日期序号。直接按 Excel 序号解码会得到与来源说明不一致的日期。

逐行对照论文关联的作者仓库，使用固定提交 `0531fe49cd704f92ad1aaff2e0160fa100262ed9` 的 `data/data.csv`：全部 6,567 行的支出、入账和余额匹配，3,946 行日期直接匹配，另有 2,621 行在数字日期的日/月互换后匹配，没有剩余无法解释的日期差异。数字日期中日与月相同的行不会发生可见变化。

[日期对照结果](date_comparison.json)保存 URL、参考文件哈希、统计与少量日期例子。对照文件含账户标识列，本轮只在内存中核对，没有将该 CSV 原件落地或复制其标识到派生数据；研究副本的描述、类别等均取自有明确许可的 Mendeley XLSX。

两版描述并非全部相同（6,358 行相同）；不能用旧版描述覆盖匿名化版本。日期修正规则仅适用于这个哈希的原件，不能推广为所有 Excel 日期都需要互换。

## 下一步我该怎么验证

核对原件哈希；打开[研究副本与说明](../../working/moneydata_v1/README.md)，查看变换规则和余额检查结果。后续仍需制作独立的提醒标签、时间切分和用户价值评估。
