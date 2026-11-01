# 收支有度｜部署、启动与恢复

本说明适用于2026年11月1日公开交付源码。银行为模拟环境，默认仅监听本机127.0.0.1。先使用独立源码目录和数据目录，不覆盖已有账本。

## 工具模式

需要Windows、Python 3.13.7。在源码根目录执行：

```powershell
py -3.13 -m venv .venv-competition
.\.venv-competition\Scripts\python.exe -m pip install -r .\requirements-competition.lock.txt
$ReleaseData = Join-Path (Get-Location) 'runtime/public-demo'
$ReleasePort = 18979
if (Test-Path -LiteralPath $ReleaseData) { throw '首次演示请选择新的数据目录；续用原数据时按恢复步骤操作。' }
if (Get-NetTCPConnection -LocalPort $ReleasePort -State Listen -ErrorAction SilentlyContinue) { throw '端口已使用，请选择空闲端口。' }
.\scripts\start_product.ps1 -DataDir $ReleaseData -Port $ReleasePort -NoModel -WithoutHistory -NoOpenBrowser
```

打开启动器返回的地址，例如http://127.0.0.1:18979/。银行工作台在同一地址的/workbench。NoModel关闭AI聊天与模型后台解释，仍能管理模拟账本和计划；它不是只读模式，也不冻结已经就绪的确定性任务。WithoutHistory关闭独立MoneyData历史库。

出现“本次未启动AI；账本查看与确定性功能仍可使用。”说明当前处于工具模式，不是用户输入错误。刷新页面不会改变模式。

## 模型环境

模型模式另需Node.js、Docker Engine、node:22-slim镜像及DSH 0.1.1-rc.2。DSH以仓库node-runtime锁文件安装，不使用未锁定的全局版本替代。

```powershell
npm ci --prefix node-runtime --ignore-scripts --legacy-peer-deps
$env:DSH_PACKAGE_PATH = (Resolve-Path '.\node-runtime\node_modules\@deepseek-ai\dsh').Path
docker version
docker image inspect node:22-slim
```

把config/settings.example.yaml复制到独立的DSH配置目录并设置DSH_CONFIG_HOME。当前模型路线为volcengine-agent-plan / deepseek-v4.1-flash，服务地址https://ark.cn-beijing.volces.com/api/plan/v3，密钥通过ARK_AGENT_PLAN_API_KEY环境变量安全注入或专用凭据文件加载。不要将真实值写入源码、文档、截图或提交记录。

镜像标签会变化；新环境应核对实际镜像并验证隔离与真实调用。当前交付完成了同机独立目录工具模式复现，复用了既有Python依赖，尚不代表任意新机器完整安装均已验证。

## 启用AI及限额

先停止工具模式服务，再使用同一源码副本、同一DataDir启动模型模式。start_product.ps1默认模型入口可管理独立体验时段，通常为60分钟、最多8次DSH运行和32次服务商请求，后台检查也计入；重启不会重置已使用计数。具体以启动器显示的实际预算及截止为准。

运行现有项目时必须遵守其已批准的累计预算和停批状态，不能使用-NewAISession恢复已经停止的旧批次。新的演示应明确建立独立受限时段，正式评测使用-BatchBudget指向已准备的预算文件。模型运行会消耗服务商额度；未授权时只进行离线检查。

Docker、DSH、模型路由、凭据和预算均就绪后才能判断AI可用。后台进程运行不保证服务商下一次请求成功。运行失败先查作业、待确认操作和原回执，避免重复已成功交易。

## 银行与虚拟事件

首页与/workbench共享同一服务账本。工作台“模拟一笔收支”可模拟消费、收入或退款。记账成功后事件进入后续检查链；AI不可用时不能将排队状态当作解释完成。

本人账户A到B转账属于资金搬移，不减少合计资金；测试消费对买电脑计划的影响，应模拟一笔支出。通过智能体准备的转账需要用户明确确认，高风险还需独立验证，之后由银行办理并返回回执，模型不能代替用户授权。

## 独立验证、停止与备份

默认未绑定独立身份因素时，高风险转账关闭。身份登记使用scripts/competition_security_enroll.py，用户自行持有验证因素；测试夹具不得冒称真实银行身份。identity.sqlite3及所有运行数据库不得公开分发。

```powershell
.\scripts\start_product.ps1 -Action Status -DataDir $ReleaseData -NoOpenBrowser
.\scripts\start_product.ps1 -Action Stop -DataDir $ReleaseData -NoOpenBrowser
```

状态和停止命令使用同一副本与DataDir。先停止新请求并等待在途任务结束，再停止服务并一致备份整个运行目录，包括银行、目标、任务、作业、证据、身份和预算。备份含敏感信息，不能上传公开仓库。恢复前先核对实例身份和版本，不删除锁或批量结束无关进程，不通过删除预算文件重置额度。
