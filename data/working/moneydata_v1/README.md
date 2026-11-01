# MoneyData 研究用规范化副本

## 验收标准与当前状态

本目录应能离线打开数据，追溯原件和每次变换，并重复得到相同结果。当前已完成结构检查、日期对照和相邻余额核对；尚未形成智能体评测集，没有提醒正确性或产品效果结论。

数据：[transactions.csv](transactions.csv)。检查结果：[validation.json](validation.json)。原件及署名：[SOURCE.md](../../external/moneydata/SOURCE.md)。

## 已完成的变换

1. 读取原件第一张工作表，忽略 1,747 个无日期且内容为空的格式行；保留 6,567 条交易。
2. 文本日期按日/月/年解析；数字日期按 Excel 1900 日期系统解码后互换日/月。这一规则有固定来源版本的逐行对照支持，2,621 行实际日期发生变化。
3. 金额用十进制定点运算四舍六入五成双到两位小数，输出整数最小单位，消除 XLSX 浮点尾数；原始金额仍保留在原件中。
4. 保留原始交易编号、描述、分类和源顺序，没有生成用户偏好、订阅合同、未来计划或提醒标签。
5. 没有从参考 CSV 引入账户号码等字段；没有凭地点替每一笔交易填币种。

许可沿用原数据 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，署名见原件说明。本项目对数据所作日期和格式变换已在此列明。

## 字段

| 字段 | 含义 |
|---|---|
| source_transaction_number | 发布版中的记录编号，用于追溯，不是银行交易 ID |
| transaction_date | 经对照规则规范化的日期，ISO 年-月-日格式；没有时分秒 |
| source_date_raw / date_transform | 原始存储值与采用的日期变换，便于复查 |
| transaction_type_raw / description_raw | 发布版交易类型与描述，未做新的语义识别 |
| debit_minor / credit_minor | 原金额乘 100 后的整数；空值保留为空，不当作遗漏交易 |
| balance_minor | 原余额乘 100 后的整数 |
| category_raw | 来源分类，存在空白与命名不一致；不是“是否需要提醒”的标签 |
| location_city_raw / location_country_raw | 来源附加地点，未验证为实时位置或每次真实消费地点 |
| currency | 原件没有独立币种字段，当前留空；两位精度不证明币种已核实 |

## 已运行的检查

- 6,567 条记录；规范化日期为 2015-07-27 至 2022-07-25。
- 原件按编号排列的日期非递增；没有日期逆序异常。
- 6,566 组相邻余额均满足“较新余额 = 较旧余额 − 较新支出 + 较新入账”，采用整数金额核对。
- 分类空白 24 条，交易类型空白 61 条；未擅自补全。
- 没有足够信息验证交易是否完整覆盖个人全部账户、每条分类是否正确、每项重复支出是否仍需继续。

注意：这不是银行接口沙盒，没有确认、转账执行、幂等或授权事件。真实静态流水和操作接口测试应分别准备。

## 下一步我该怎么验证

在项目根目录打开 PowerShell，运行以下命令即可离线重建本目录的数据和检查结果，无须安装第三方库：

```powershell
python research/normalize_moneydata.py data/external/moneydata/laramee26openBankTransactionData.xlsx data/external/moneydata/date_comparison.json data/working/moneydata_v1
```

查看输出应为 `rows: 6567`、`balance_mismatches: 0`。重建会覆盖本目录派生文件，因此不要手动编辑派生 CSV；改动应通过脚本并记录版本。
