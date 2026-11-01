# 银行 DSH 领域插件

目标：模型用自然语言理解和选择工具，银行服务用程序维护金额、权限与回执。DSH 是真实执行循环，不使用另写的模型聊天循环。

## 文件职责

| 文件 | 单一职责 |
|---|---|
| `../../scripts/dsh_v1.cjs` | stdin/环境边界、配置生成、子进程生命周期、脱敏JSON输出 |
| `boot.cjs` | 调用官方 `dsh-app-boot.boot`，加载受限 Profile，避免Windows junction权限要求 |
| `driver.cjs` | 原生创建/恢复Agent，挂 bank-v1 Preset，投递一轮消息并提取真实结果 |
| `tools.cjs` | 注册已批准的银行schema，将工具调用桥接到固定本地HTTP服务 |
| `persona.txt` | 银行领域指令、证据/日期/确认/推测边界 |

生成的 `runtime/.../home/profiles/bank-v1/` 是Profile；`home/agent-presets/bank-v1/` 是DSH原生Agent Preset。Preset仅挂领域persona和银行tools插件。base层仅保留会话、模型、工具、官方Agent循环和检查点等核心插件。每轮工具表实际验证，与服务器允许清单不一致就停止。

## 调用与数据流

完整合同在 `management/contracts/V1_DSH.md`。Python后端设置可信银行服务地址和Bearer后启动Node，stdin传用户问题。Node从DSH已有配置取被授权的Coding Plan路由；模型密钥只进入专属子进程内存/环境。模型只看到业务工具参数及返回值。

模型调用工具 → HTTP银行服务 → 结构化业务事实 → DSH继续推理 → 结果回到Web任务。用户确认入口没有注册为模型工具，模型无法通过本插件确认操作。

事件任务设置 `BANK_V1_READONLY=1`，工具表进一步缩到只读能力；后台还须核验真实成功event_context记录再完成事件任务。

会话跨进程继续使用DSH原生resume；银行安排及状态保存在银行数据库，两者不可混为一谈。进程退出不丢已持久化的会话，应用重启后通过返回的session_id恢复；被强杀的失败轮次不应当作成功结果。

## 复用依据与边界

- 本机 `@deepseek-ai/dsh@0.1.1-rc.2`，官方 AgentRegistry、Agent Presets、Session JSONL、ToolRuntime、app-boot；未修改上游包。
- 官方工具教程：https://deepseek-harness.github.io/deepseek-harness/develop/basic/tool
- 原生Web内嵌面板尚未交付；当前领域Web通过Node适配器调用真实DSH，独立浏览器面板负责可信确认。
- 不宣称操作系统沙箱：限制是受信任插件组成、模型可调用工具及HTTP权限边界。运行用户自身仍可修改本地代码和文件。

## 下一步我该怎么验证

主对话/独立验收复跑真实银行读取、准备转账未扣款、精确确认后查回执；新runtime冷启动、同session第二轮、事件只读分析、取消超时。结果对应 `management/evidence/V1/dsh/`，不由用户承担技术测试。
