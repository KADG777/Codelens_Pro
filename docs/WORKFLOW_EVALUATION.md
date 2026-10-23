# 公开合成任务评测

本文记录首轮开发评测的执行方法与历史结果；最新三组对照和优化复测见 [协作实验总览](AGENT_VALUE_EXPERIMENT.md)。各阶段使用的任务集、分母和预算不同。

此评测区分“流程可以运行”和“最终函数通过独立审核”。候选代码只由明确配置的隔离执行器运行；评测脚本不会在宿主进程导入或执行候选源码。真实 API 运行由用户授权后执行，默认命令使用离线固定响应。

## 任务与场景

`evals/workflow_cases.json` 定义 12 个运行场景，引用 `workflow/registry.py` 中的 10 份冻结任务合同：

| 任务 | 主要检查点 |
|---|---|
| 订单统计 | 付款筛选、所有订单字段校验、空输入、不修改输入 |
| 文本统计 | 空串、Unicode、空白、换行语义 |
| 稳定去重 | 首次出现顺序、重复值、不修改输入 |
| 数值限幅 | 上下界、相等边界、非法区间异常 |
| 游程编码 | 空输入、分组、最后一组、Unicode |
| 区间合并 | 重叠/接触端点、嵌套、非法区间、不修改输入 |
| 括号匹配 | 正确嵌套、交叉匹配、残留括号、忽略其他字符 |
| 滑动均值 | 最后完整窗口、长度不足、非法窗口与 bool |
| ASCII 标识 | 连续分隔符、首尾处理、非 ASCII 字符 |
| 英文词频 | ASCII 分词、标点和数字边界、大小写 |

订单和文本统计从需求生成；其余八个任务从带已知缺陷的合成代码进入。额外包含一个正确源码不修改对照，以及一个缺少数据却要求保证效果的停机场景。停机场景单独报告，不当作普通函数修复失败，也不混入功能通过率的分母。

所有源码均为项目自建合成内容，不使用用户上传的图像识别脚本或任何个人数据。参考实现和任务集公开且参与开发，因此结果属于开发评测，不是外部盲评或通用准确率。

## 公开验收与独立审核

工作流可以使用合同的公开验收用例获得反馈。独立审核选择 `TaskSpec.holdout_cases`，只在一次工作流结束之后对原始版本 v0 和最终当前版本分别运行。报告用 `build_harness(task_id, holdout=True)` 的 SHA-256 记录审核合同版本；便携执行器实际只把输入交给 guest，期望值留在宿主比较。审核结果不会回写运行状态，也不会发送给编码、审查或修复 Agent。

真实模式只提交任务文本、起始源码、验收合同标识与预算。参考实现、独立审核的输入输出、期望值和审核结果不进入 Agent 的模型消息。报告保存审核合同的 SHA-256、完整版本、事件、失败与实际审核结果，以便复查。

这种分离避免把“审查模型说没问题”作为效果评分。初稿本来正确时，修复角色可以跳过；报告只在实际执行编码、审查、修复三个角色时列为三角色需求生成演示，不会人为改坏模型初稿来制造这条记录。

## 运行方式

在项目目录使用已安装依赖的 Python：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluation.evaluate_workflow --mode offline_smoke --db reports/workflow-smoke.sqlite3 --output reports/workflow-smoke.json
```

`offline_smoke` 注入固定编码、审查和修复响应，明确标为离线；`actual_model_calls` 和真实 token 均为 0。它可以配合真实隔离执行器检查评测链路，但其模型产物不能算真实模型质量成绩。为复用在线角色接口，内部工作流状态使用注入模型的运行路径；应以评测报告的 `evaluation_mode` 为准，不将烟测数据库作为真实 API 记录展示。

具备可用模型配置和隔离执行环境后，真实评测命令为：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluation.evaluate_workflow --mode real_live --max-total-model-calls 60 --max-total-tokens 300000 --db reports/workflow-live.sqlite3 --output reports/workflow-live.json
```

真实模式会发送这份公开合成任务集的任务与代码并产生 API 用量，不会提交用户此前的脚本。可使用 `--select orders_generate,text_stats_generate` 选择子集。默认独立执行器为 `workflow.validation.audit_execute`；也可用 `--audit-executor trusted.module:function` 接入项目认可的隔离适配器，不能接入在宿主直接执行候选的实现。

普通运行场景使用 `functional_portable` 公开验收档位；独立审核也必须由实际隔离运行时执行。执行器、运行时或工具不可用时保存 `not_executed / error`，不把它折算为功能通过。每个已开始场景都会保存完整工作流快照；全局预算耗尽后的场景仍保留在报告中并标为未运行。

便携后端为 CPython 3.13.15 WASI / Wasmtime 49.0.0，归档 SHA-256 为 `67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d`。运行前校验归档，只预开标准库和临时工作区的只读目录，限制时间、指令、内存与输出；不继承密钥或提供网络能力。guest 返回实际值、异常类型和调用后输入，由宿主核对冻结期望，不采用源码自报的“通过”。来源、许可证与对抗边界见[便携运行时说明](../sandbox_runtime/README.md)。本机 Docker 尚未实测，不能把 WASI 结果写成 Docker 结果。

默认真实批次最多 60 次模型调用、300000 输入与输出 token；每个运行获得剩余批次预算。失败请求的已提交用量也累计，网络 HTTP 尝试数另行记录。报告的实际用量来自保存记录，若服务返回量超过预算则记录超限并停止后续场景，不隐藏该事实。

## 如何读报告

| 字段 | 含义 |
|---|---|
| `functional_audits.initial/final` | 初始与最终独立审核通过、失败、不可评估数量，以及已完成审核中的通过比例 |
| `repair_opportunities` | 初始独立审核实际失败的场景数 |
| `repair_successes` | 初始失败且最终独立审核实际通过的场景数 |
| `repair_failures` | 初始失败且最终独立审核实际失败的场景数 |
| `repair_outcome_unknown` | 初始失败但最终缺少有效审核结果，不能计成修复失败或成功 |
| `regressions` | 初始通过、最终失败的场景数 |
| `changed_clean_controls` | 正确源码对照发生文本修改的数量；文本修改本身不等于功能回归 |
| `no_progress_cases` | 无实质变化或重复历史代码而停止的场景数 |
| `safe_stop_cases` | 明确缺少效果证据的场景及其停机结果 |
| `three_role_generation_cases` | 实际从需求执行编码、审查及修复的场景 ID；离线模式仍只属于固定响应演示 |
| `actual_*` / `scripted_model_calls` | 分别记录真实模式用量与离线固定响应调用，不能相加称为真实请求 |

必须同时查看已完成数量与不可评估数量。缺失执行、服务错误和预算未运行不从计划样本中删除，也不冒充“模型修复失败”。当覆盖不完整时，`pass_fraction_among_completed` 只描述已完成审核的子集，不能描述整个计划任务集。

每个 `entries` 保存 task、原始/最终源码、完整 snapshot、两份审核检查、模型调用/token/活动时间、终止状态与角色执行情况。最终工作流可以因待确认意见而停机，同时函数通过审核；也可以流程结束但独立审核失败。两种结论必须分开。

## 首轮历史验证状态

本模块的独立报告与隔离回归测试已完成 **13 项通过，耗时 0.69 秒**，仅使用模拟服务和模拟审核回执，不调用 API、不执行候选源码。它验证审核隔离、初始/最终计分、回归、未知结果、预算、完整失败记录和离线标识。以下把真实隔离烟测与真实模型批次分别记录，不用这些模拟回执替代效果数据。

### 12 场景离线固定响应烟测

已实际执行全部 12 场景：11 个 `completed_verified`，1 个效果未知场景 `needs_attention`。11 个可评估功能场景的独立审核为初始 1 通过 / 10 失败，最终 11 通过 / 0 失败；观察到的回归为 0，正确对照没有修改。固定角色调用记为 66 次，**真实模型调用与真实 token 均为 0**。

WASI 实际执行了公开验收和独立审核，角色产物则来自固定夹具。该结果验证评测与隔离链路，不是模型真实修复正确率。报告位于本机 `reports/workflow-smoke.json`，不自动进入发行包。

### 8 场景真实模型批次

本轮从 12 场景中选取 8 场景，覆盖订单、文本统计、限幅、区间合并、滑动均值、ASCII 标识共 6 种合同；其他四类合同尚无本批次的真实模型成绩。报告中的配置模型名称均为 `deepseek-flash`，不是服务端固定版本承诺。实际选择为：

```text
orders_generate,text_stats_generate,clamp_bounds_and_exception,merge_intervals_without_mutation,moving_average_last_window_and_bool,slug_ascii_and_separator_runs,orders_clean_no_unnecessary_change,unmeasured_effect_safe_stop
```

复现时将这串 ID 作为 `--select` 参数，不能把默认 12 场景的结果与此批次直接混算。预算为最多 60 次调用 / 300000 token；实际 **46 次模型与 HTTP 调用、102848 输入 + 8273 输出 = 111121 token**，工作流有效执行时间合计 **116.766 秒**，无观察到的超限、未运行或缺失审核场景。有效执行时间不包含全部审核与环境准备耗时。

| 指标 | 实际结果 |
|---|---:|
| 普通功能场景数 | 7 |
| 初始独立审核通过 / 失败 | 3 / 4 |
| 最终独立审核通过 / 失败 | 7 / 0 |
| 初始失败后实际修复通过 | 4 / 4 |
| 初始通过后回归 | 0 |
| 正确源码对照发生修改 | 0 |
| 效果未知的停机对照 | 1，`needs_attention` |

两条需求生成首稿都通过，实际执行 CodingAgent 和 ReviewAgent，FixAgent 合法跳过；四条带缺陷上传代码执行 ReviewAgent 和 FixAgent。**本批次没有单次真实需求生成同时执行三个角色的案例**，`three_role_generation_cases=[]`。不能把离线固定响应演示或多个不同运行拼成这项证据。

所有审核后端实际记录为 `cpython-wasi`，运行时哈希一致。原始完整报告保存在本机 `reports/workflow-live.json`；只将经过审核的公开合成任务副本放入 `evals/results/` 供评测看板与复核使用，不发布用户图像识别脚本、密钥或个人运行数据。

这 8 个合成场景和参考实现参与了开发，不是独立外部盲测。最终 7/7 只适用于这组已完成审核的功能场景，不能称为通用正确率 100%；效果未知对照也没有被改写为“识别率提高”。本次没有对每条审查意见做人工精确率/召回率标注，函数通过率也不等于审查意见准确率。实际 Docker、用户识别模型效果及未选择合同的真实模型表现仍未验证。

## 同任务配对实验已补齐

2026-10-17 新增 `scripts/experiments/compare_workflows.py` 与冻结清单 `evals/paired_cases.json`。8 对真实同模型输入对照包含 4 个此前未使用的新合成合同，两组均 8/8 通过、0 回归；完整工作流成本显著更高。方法、局限、总量和分组结果见 [PAIRED_COMPARISON.md](PAIRED_COMPARISON.md)。这与上文“初稿→修复后”的开发评测是两个不同实验，不混用分母。
