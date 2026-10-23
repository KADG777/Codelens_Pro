# GitHub CI

配置文件：`.github/workflows/ci.yml`，名称 **Codelens Pro CI**。

README 顶部的徽章读取 `main` 分支的实时工作流状态。通过后显示绿色 `passing`；点击可进入工作流页面。徽章与某个提交旁的检查图标是两个入口，不需要填写模型密钥。

## 触发与检查

每次 push、Pull Request 和手动运行都会在 `windows-latest` / Python 3.12 中依次执行：

1. 创建独立虚拟环境，按 `requirements-dev.txt` 安装，并用 `requirements-lock.txt` 约束已验证版本，检查依赖冲突。
2. 对整个项目执行 Ruff 检查，包括评测脚本和 CI 辅助脚本。
3. 检查发布文件选择、必需文档和 WASI 归档 SHA-256；缺少运行时或 Wasmtime 直接失败，不能把跳过功能验收测试当作通过。
4. 执行 `tests/` 和 `tests_workflow/` 的完整 pytest 测试，并生成 JUnit XML。
5. 生成 `dist/Codelens_Pro.zip`，检查归档路径、文件清单、逐文件 SHA-256 和必需文件。
6. 检查源文件、Git 暂存内容、完整提交历史和源码包中的疑似模型密钥；命中就失败，输出只包含文件名，不打印密钥或匹配行。
7. 上传测试报告；前面全部通过时上传源码包。Artifacts 保留 14 天。

采用 Windows runner 是为了实际覆盖中文路径、CMD / PowerShell 启动器和本地运行环境。当前 CI 不宣称覆盖 Linux、macOS 或所有 Python 版本。

工作流只有读取仓库内容权限；不持久化检出凭证，不配置模型 Secret。同一分支的新提交会取消旧的未完成 CI，单次运行最多 20 分钟。

## 模型与验收边界

CI 使用固定角色响应或模拟模型接口测试协作逻辑。功能用例会在真实 CPython WASI 中执行，但不调用付费模型。已有的真实对照报告是历史评测证据，不会在每次推送时重新生成。

CI 通过证明当前提交通过这些检查，不代表任意用户代码正确，也不代表多 Agent 的准确率一定高于单模型。

## 本地复现

在项目根目录打开 PowerShell，逐行执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe scripts/check_distribution.py
.\.venv\Scripts\python.exe scripts/check_secrets.py --history
.\.venv\Scripts\python.exe -m pytest tests tests_workflow -q --junitxml=reports/tests.xml
.\.venv\Scripts\python.exe package_release.py --output dist/Codelens_Pro.zip
.\.venv\Scripts\python.exe scripts/check_distribution.py --archive dist/Codelens_Pro.zip
.\.venv\Scripts\python.exe scripts/check_secrets.py --archive dist/Codelens_Pro.zip
```

安装依赖需要网络；后续测试无需模型服务。不要把 `data/`、`reports/`、`.venv/` 或 `dist/` 强制加入 Git。

## 在 GitHub 查看结果

仓库 **Actions → Codelens Pro CI → 某次运行** 中可查看每个步骤。Artifacts 包含：

- `test-results-windows-python312`：JUnit 测试报告，便于记录失败与通过情况。
- `Codelens_Pro-source`：验证后的源码 ZIP，内部顶层目录为 `Codelens_Pro/`。

以当前提交对应的 Actions 记录为准，本地检查不能替代云端结果。

使用的官方 Actions：[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[upload-artifact](https://github.com/actions/upload-artifact)。

## 验证记录与密钥检查

当前测试范围及最近结果见 [验证记录](VALIDATION.md)，旧版本过程可从 [Git 提交历史](https://github.com/KADG777/Codelens_Pro/commits/main/) 查看。模型复测单独运行，不能把历史 API 结果当作本次 CI 的模型测试。

本地指定 `--basetemp` 时先创建父目录；CI 使用默认临时目录。

凭证检查无需向 GitHub Secrets 提交真实密钥。本机存在 `DEEPSEEK_API_KEY` 时额外做实际值匹配；CI 在空密钥环境中进行格式规则检测。`.env`、运行数据、缓存和私人上传文件仍由发布规则排除。
