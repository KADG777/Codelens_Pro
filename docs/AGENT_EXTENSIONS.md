# Agent 扩展合同与注册

`workflow/node_registry.py` 提供与任务合同 TaskSpec 分开的角色注册接口。内置“验收解读 Agent”已用真实 DeepSeek 模型参与 2026-10-17 的 8 项配对实验；每次检查汇合后解释风险，使用同一模型预算，不参与最终通过判定。

## 扩展一个角色

```python
from workflow.node_registry import NodeOutput, NodeSpec, default_registry
from workflow.service import WorkflowRunService

def summarize_evidence(request, context):
    validation = request.state['validation']
    return NodeOutput(
        code_hash=request.code_hash,
        summary=f"验收状态：{validation['status']}；仅解释已保存证据。",
    )

registry = default_registry()
registry.register(NodeSpec(
    id='evidence_summary', version='1.0', label='证据摘要 Agent',
    description='解释验收记录', handler=summarize_evidence,
    reads=('validation',),
))
service = WorkflowRunService('data/example.sqlite3', node_registry=registry)
run_id = service.create_run(
    task='按所选合同完成文本统计', acceptance_id='text-stats-v1',
    workflow_config={'extension_nodes': ['evidence_summary']},
)
result = service.execute(run_id)
service.close()
```

这个例子是确定性角色。模型角色可通过 `context.complete(OutputSchema, system, payload)` 调用模型，内置验收解读角色就是完整例子。只需注册和选入执行计划，无须修改 `WorkflowRunService` 的中心分发、条件边或 UI 路由代码。页面展示内置注册表；新增可选项应在 `default_registry()` 的组合入口注册。

## 输入、输出和状态权限

| 项目 | 合同 |
|---|---|
| 输入 | `NodeInput`：node_id、code_hash、task、state；声明 `reads` 后取得相应字段的深拷贝 |
| 可读字段 | review、validation、acceptance_spec、fix_round、node_outputs、input_mode |
| 输出 | `NodeOutput` 或其子类；必须包含当前 code_hash 和非空 summary，禁止未声明字段 |
| 写入 | 仅 `node_outputs[节点ID]` 和 `node_output_history`；保留节点版本与源码哈希 |
| 模型使用 | 通过统一预算包装器；调用、token、格式错误和失败均记账 |
| 路由 | 在检查汇合后、判断节点前，按 extension_nodes 顺序执行；最多 4 个扩展 |
| 恢复 | 保存 ID→版本清单；缺失或版本变化时停止，恢复原插件或新建任务 |

扩展输入中的可变对象与核心状态隔离。错误哈希或不符合输出合同会停止，不能伪造成功、覆盖源码、改写验证结果或控制 next_node。每次执行与核心节点共用开始通知、计时、错误处理、持久化交接与可视化。

插件是项目维护者安装的可信本地 Python 代码，注册机制不是不可信代码沙箱。不从上传源码动态导入插件。插件维护者必须在改变行为或合同后提升 version。当前公开扩展点是“检查后解释/分析”角色；编码、修复等有权提交源码的核心节点仍由内置适配器管理。任意 DAG 和替换核心源码写入角色不属于此版扩展范围。

`tests_workflow/test_extensions_trace.py` 实际注册独立 `test_observer`，验证无需改分发器即可运行、暂停并重开恢复；同时验证输入修改隔离、非法输出、旧哈希、缺失插件和并行交接。
