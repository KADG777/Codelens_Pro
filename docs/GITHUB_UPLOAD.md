# 将 Codelens_Pro 上传到 GitHub

本目录已经整理为独立项目。本文按你之前仓库记录里的账号 `KADG777` 和新仓库名 `Codelens_Pro` 编写。旧作业仓库 `KADG777/CodeLens` 保持独立。

## 1. 创建空仓库

登录 GitHub，打开 https://github.com/new ：

- Owner：`KADG777`。
- Repository name：`Codelens_Pro`。
- Description：`Multi-agent code review and repair workflow with visual tracing and isolated validation.`
- Public / Private：根据作业提交要求选择。
- 不勾选添加 README，不添加 .gitignore 或许可证；这些初始化选项会生成远端提交，与首次推送的本地历史冲突。

点击 **Create repository**。若同名仓库已经存在且有内容，请先核对，不要覆盖或强制推送。

## 2. 在本地文件夹打开 PowerShell

打开 `E:\大三上课程\软件工程项目实践\Codelens_Pro`，在文件管理器地址栏输入 `powershell` 并回车。

本机这次整理的目录会保留本地 Git 提交；若 `git log -1 --oneline` 能看到初始提交，可直接从 `git remote add origin` 开始。下面也给出从源码 ZIP 新解压后首次上传的完整命令；逐行执行，某一步报错时先解决，不要继续执行后续命令：

```powershell
Set-Location -LiteralPath 'E:\大三上课程\软件工程项目实践\Codelens_Pro'
git init -b main
git add .
git diff --cached --stat
git commit -m "Initial release of Codelens Pro with CI"
git remote add origin https://github.com/KADG777/Codelens_Pro.git
git push -u origin main
```

本地若已经存在首个提交，`git commit` 提示 `nothing to commit` 是正常情况，继续设置远端。若 `origin` 已存在，用 `git remote -v` 核对；地址正确就直接推送。若账号或仓库名不同，把远端地址换成新建仓库页面显示的 HTTPS 地址。

首次推送可能弹出 Git Credential Manager 的浏览器登录，使用你的 GitHub 账户完成授权。不要把 API Key、GitHub token 或密码粘贴进项目文件。若出现 `Author identity unknown`，按终端提示在本仓库设置你自己的 `user.name` 和 `user.email`，再提交。

上传的是本文件夹内的源码结构，不是把整个 ZIP 文件当成唯一内容上传。仓库首页应直接看到 `app.py`、`README.md`、`workflow/` 和 `.github/`；这样 Actions 才能找到工作流。

## 3. 确认 CI 真正运行

推送成功后打开仓库的 **Actions → Codelens Pro CI**，点击最新一次运行。

- 绿色勾：本次提交的检查、测试和打包通过。
- 红色叉：打开失败步骤查看日志；修改后再次推送会重新执行。
- 运行完成后，页面的 **Artifacts** 可下载测试报告和源码包。
- 需要重跑时，可以在工作流页面点击 **Run workflow**，选择 `main`。

CI 不需要添加 DeepSeek Secret，不会运行付费的真实模型对照实验。本地测试通过不等于 GitHub 已通过；必须以 Actions 的运行结果为准。具体检查内容见 [CI 说明](CI.md)。

## 4. 后续更新

以后在 `Codelens_Pro` 中修改，并执行：

```powershell
git status
git add .
git diff --cached --stat
git commit -m "Describe this change"
git push
```

`.gitignore` 已排除 `.env`、虚拟环境、用户上传、运行数据库和缓存；`.env.example` 是不含密钥的配置模板，可以提交。真实使用时把自己的密钥保存在本地 `.env`，无需推送到 GitHub。

官方参考：[将本地代码添加到 GitHub](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github?platform=windows)。
