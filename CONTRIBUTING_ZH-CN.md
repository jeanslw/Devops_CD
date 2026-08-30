# 贡献指南

首先，感谢你考虑为 **Devops-CD** 做出贡献！Devops-CD 是一个基于 FastAPI + Vue 3 的持续部署（CD）服务，与 [Devops-Glue](https://github.com/jeanslw/Devops-Glue)（CI 服务）配套，将 Harbor 镜像部署到 Docker 或 Kubernetes 集群。这份指南旨在帮助你顺利参与项目，无论你是报告 Bug、提出新功能，还是提交代码。

## 核心原则

在开始之前，请了解这个项目的两个核心理念：

1. **Devops-CD 只做「CD」，不做「CI」**：它负责把镜像部署到 Docker / Kubernetes，而构建、打 tag 等 CI 职责属于 Devops-Glue。任何贡献都应尊重这条边界，避免重复实现 CI 的职责。
2. **接口优先，绝不直接读表**：能通过接口获取的，优先调用现有 API，而不是直接读数据库表；建表、加索引等改动应放到 CI（Glue）侧，与 CI 负责人协商，而不是在 CD 侧改动共享数据库。

---

## 如何报告 Bug

如果发现 Bug，请在 GitHub Issues 中新建一个 Issue，并尽量包含以下信息：

- **简要描述**：清晰简洁地描述问题。
- **复现步骤**：详细的操作步骤，包括使用的版本和配置。
- **预期行为**：你希望看到什么结果。
- **实际行为**：实际发生了什么，如有错误截图或日志请一并贴上。
- **环境信息**：
    - Devops-CD 版本（或 commit hash）
    - Python 版本
    - 数据库类型（MySQL / SQLite）及版本
    - 部署模式（ssh / compose / k8s kubectl / helm / argocd / fluxcd）
    - 相关的 Devops-Glue（CI）版本

## 如何提出新功能或改进

在提出新功能前，建议先搜索已有 Issue，避免重复。提交新功能建议时，请说明：

- **这个功能解决了什么痛点？** 描述你在实际场景中遇到的问题。
- **你的建议方案是什么？** 尽量具体，如果可能，描述你设想的 API 或界面交互方式。
- **这个功能是否符合 CD 的职责边界？** 说明它如何改进部署，而不是重复造 CI 的轮子。

## 代码贡献流程

### 1. 沟通先行
如果你打算实现一个较大的功能或重构，请**先在 Issue 中讨论**，确保你的方向与项目维护者一致，避免投入大量精力后方案被拒绝。

### 2. 准备开发环境
- 确保已安装 **Python 3.10+**。
- Fork 本仓库，并将你的 Fork 克隆到本地。
- 创建虚拟环境并安装依赖：

  ```bash
  python -m venv venv
  source venv/bin/activate   # Windows: venv\Scripts\activate
  pip install -r requirements.txt
  ```

- 复制 `.env.example` 为 `.env`，配置数据库 / Harbor / CI API。
- 启动后端：`python main.py`（监听 `http://localhost:8081`）。
- 前端：`cd frontend && npm install`，开发用 `npm run dev`，发布用 `npm run build` 生成静态包。

### 3. 编写代码
- **代码风格**：Python 代码遵循 **ruff**（`ruff check .`），配置见 `pyproject.toml`。行宽尽量控制在 120 字符内。
- **测试**：在 `backend/tests/` 下新增/更新单元测试，运行 `python -m pytest backend/tests`。
- **国际化**：用户可见的日志信息统一使用 `S("deploy_log.xxx")` 助手，并同时在 `frontend/src/locales/` 的 `en` 与 `zh` 中补充条目。
- **文档**：你的贡献必须包含或更新相关文档：
    - 在 `README.md` / `README_ZH-CN.md` 或 `docs/` 下更新使用说明。
    - 如果是新的 API 接口，更新手册中的 OpenAPI / 端点列表。
    - 如果引入新配置项，更新 `.env.example` 和管理员手册。

### 4. 提交代码（Commit Message）

请使用清晰、描述性的提交信息，遵循 **Conventional Commits** 规范：

    <类型>(可选范围): <简短描述>

    <可选的详细描述>

- **常用类型**：
  - `feat`: 新功能
  - `fix`: Bug 修复
  - `docs`: 文档变更
  - `style`: 代码格式（不影响功能）
  - `refactor`: 重构（不是新功能也不是修 Bug）
  - `test`: 增加或修改测试
  - `chore`: 构建过程或辅助工具的变动

**示例**：

    feat(rollback): 新增 argocd 原生回滚

    回滚现在调用 ArgoCD rollback API，并通过 SSE 流式推送双语日志。

### 5. 发起 Pull Request (PR)

- 确保你的 PR 基于最新的 `main` 分支。
- 在 PR 描述中，清晰说明解决了什么问题，并关联相关的 Issue（如 `Closes #123`）。
- 确保 CI（如果已配置）检查通过，且分支没有冲突。

## 行为准则

本项目的参与者应遵守 [贡献者公约](https://www.contributor-covenant.org/zh-cn/version/2/0/code_of_conduct/)。我们期望所有互动都是开放、包容和尊重的。

## 获取帮助

如果你在贡献过程中有任何疑问，欢迎在 Issue 中提问，或通过邮件联系维护者（jeanslw@qq.com）。

再次感谢你的贡献！🎉
