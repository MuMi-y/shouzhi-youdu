# 收支有度

**让每一笔收支，照顾你想要的生活。**

参赛队伍：从容计划。

收支有度是一款面向生活目标的财务智能体演示系统。它把账户流水、未来收入与支出安排、生活目标放在同一时间窗口中，帮助用户比较选择，并通过独立确认和银行回执办理受控操作。

## 可以体验什么

- 查看账本、账户余额及未来现金流，区分消费、退款和本人账户转账。
- 在“我的计划”查看、修改和取消计划；计划记录与智能体工具共享，保存计划不等于付款。
- 比较消费与不消费对未来资金和具名目标的影响。
- 通过确认与验证流程办理虚拟本人转账、查询或撤销银行代扣。
- 使用事件队列、后台检查、去重和有限重试机制持续核对变化；AI解释需要模型与Worker在线。

所有银行账户和资金办理均为模拟。未接入真实银行，不用于真实资金交易。

## 快速开始

目标环境：Windows、Python 3.13.7。请从本仓库根目录在PowerShell中执行：

```powershell
py -3.13 -m venv .venv-competition
.\.venv-competition\Scripts\python.exe -m pip install -r .\requirements-competition.lock.txt
.\scripts\start_product.ps1 -NoModel -WithoutHistory -Port 18979 -DataDir .\runtime\public-demo
```

首页：http://127.0.0.1:18979/ ；同账本银行工作台：http://127.0.0.1:18979/workbench 。端口和数据目录应独立且未被其他实例占用。NoModel模式可以管理账本和计划，AI聊天与自动AI解释关闭。

完整模型配置、停止与备份见[部署说明](docs/COMPETITION_RUNBOOK.md)。模型模式另需DSH、Node.js、Docker、模型凭据及受限运行配置。仓库不包含密钥、身份库、用户运行数据库或个人日志。

## 项目结构

- `src/bank_v1`：模拟银行、资金与权限控制。
- `src/bank_requests`、`src/bank_evidence`：请求约束与事实证据。
- `src/bank_tasks`、`src/bank_goals`、`src/bank_insights`：多步骤任务、目标比较与周期候选。
- `src/competition_web`：产品网页与宿主。
- `node-runtime`、`plugins`：锁定的DSH依赖声明及工具插件。
- `tests`、`scripts`：测试、评测与部署入口。
- `data`、`third_party`：合成数据、公开历史数据及第三方许可。
- `docs`：部署、使用方式与能力范围说明。

## 测试与范围

三类核心业务已有分项实证，人工计划窗口支持创建、修改和取消，确定性现金流按日期计算。复杂跨轮理解、连续目标旅程、部分异常链审计及完整隔离覆盖仍需完善。当前分项结果及待测范围见[测试范围说明](docs/COMPETITION_AI_STATUS.md)，不以页面截图替代真实AI验证。

```powershell
.\.venv-competition\Scripts\python.exe -m unittest discover -s tests/competition_eval -v
.\.venv-competition\Scripts\python.exe scripts/competition_acceptance_live.py --phase both
```

第二条命令默认仅列出测试计划；真实模型执行需要显式参数和独立测试预算，会消耗服务商额度。本仓库提供可独立分发的测试子集；依赖本地历史会话的六个证据回归不随包公开，名单见测试范围说明。

## 数据与许可

MoneyData的来源与CC BY 4.0署名见[data/external/moneydata/SOURCE.md](data/external/moneydata/SOURCE.md)。其账户归属和币种不等同于本项目虚构人民币账户，不并入模拟资金。第三方许可保留在相应目录；项目自身未声明额外开源许可证，公开可读不等同于授予任意再许可权。

本仓库仅维护项目代码、数据与依赖许可、部署和解释性文档。PPT、答辩讲稿、报名PDF及其他参赛材料在本地单独交付，不纳入仓库当前版本。当前包含2026-11-01只读查询与单账户账单修复，详见[更新说明](docs/HOTFIX_20261101.md)。`release_manifest.json`记录当前仓库文件哈希，便于核对文件完整性。
