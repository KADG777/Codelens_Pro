# 工具入口

在项目根目录运行以下命令。`start.cmd` / `install.cmd` 仍是应用启动和安装入口；工具脚本按用途整理，避免堆在仓库首页。

| 目录 | 用途 | 文件 |
|---|---|---|
| `evaluation/` | 审查计分、追问评测、工作流评测、故障注入 | `evaluate.py`、`evaluate_followup.py`、`score_evaluation.py`、`evaluate_workflow.py`、`evaluate_recovery.py` |
| `experiments/` | 同任务模型对照、优化复测、恢复与定点验证 | `compare_workflows.py`、`compare_agent_value.py`、`compare_optimized.py`、`resume_comparison.py`、`verify_oracle_guard.py`、`benchmark_review.py` |
| `demo/` | 无密钥流程演示、真实审查过程记录 | `run_demo.py`、`record_demo.py` |
| 本目录 | 打包、发布文件检查、凭证扫描 | `package_release.py`、`check_distribution.py`、`check_secrets.py` |

## 无模型费用的常用命令

```powershell
.\.venv\Scripts\python.exe -m scripts.demo.run_demo
.\.venv\Scripts\python.exe -m scripts.evaluation.evaluate_workflow --mode offline_smoke --db reports/workflow-smoke.sqlite3 --output reports/workflow-smoke.json
.\.venv\Scripts\python.exe -m scripts.package_release --output dist/Codelens_Pro.zip
.\.venv\Scripts\python.exe -m scripts.check_distribution --archive dist/Codelens_Pro.zip
.\.venv\Scripts\python.exe -m scripts.check_secrets --archive dist/Codelens_Pro.zip
```

流程演示使用预设角色；离线工作流评测使用固定角色和实际隔离验收。两者都不能作为真实模型质量成绩。故障注入工具 `python -m scripts.evaluation.evaluate_recovery` 同样不调用模型。

## 真实模型工具

实验工具、审查评测和 `record_demo` 会使用本地 DeepSeek 配置并产生费用，应明确任务范围、输出路径和费用预算后运行。任务源码会发送到配置的服务。完整方法见 [实验总览](../docs/AGENT_VALUE_EXPERIMENT.md)、[优化实验](../docs/OPTIMIZATION.md) 和 [工作流评测](../docs/WORKFLOW_EVALUATION.md)。

例如，历史两组对照的新记录入口为：

```powershell
.\.venv\Scripts\python.exe -m scripts.experiments.compare_workflows --output reports/new-paired-trial.json --db reports/new-paired-trial.sqlite3 --max-calls 96 --max-tokens 220000
```

`resume_comparison` 与 `verify_oracle_guard` 是历史实验的专用验证工具，需要原私有检查点，已有公开报告时会拒绝再次运行。`benchmark_review` 对照两份审查引擎，需要另行提供基线源码。它们不参与日常启动或 CI。

本次目录整理没有重新调用真实模型，也没有修改冻结任务或实验 JSON。历史报告中的实现路径、SHA-256 对应采集时的版本；可从 Git 历史取得当时源码，不能用整理后的文件重新计算并覆盖历史指纹。
