# Devops-Glue CD
FastAPI 持续部署服务，与 [Devops-Glue API](https://github.com/jeanslw/Devops-Glue.git) 配套使用，将 Harbor 镜像部署到 Docker 或 Kubernetes 集群。

> ❌ GitLab + K8s 全套？太重，养不起  
> ❌ Jenkins 裸奔？8 年前的 UI，配到崩溃  
> ❌ Gitee + Jenkins + Harbor 三头对不上？多窗口来回切，Tag 全靠人肉对齐  
>
> ✅ 现在只需一套 API，多 Git 平台 + 双 CI 通道 + Harbor → 一个面板全搞定  
> ✅ SQLite 零配置启动，MySQL（推荐）也可切换  
> ✅ 10 年运维老兵的实战结晶  
> ✅ 从 CI 构建到 CD 部署，全流程覆盖  
> ✅ 开源免费，GitHub / Gitee 双更新  
>
> **不是大厂的遥控器，是小团队的瑞士军刀。**

>**[英文版](README.md)**

![系统概览](system_info_zh.png)
![运行状态](system_running_zh.png)


## 基础

- **主页**：https://github.com/jeanslw/devops_cd.git 或 https://gitee.com/jeanslw/devops_cd.git
- **语言**：Python 3.10+
- **框架**：FastAPI + uvicorn
- **前端**：Vue 3 + Vite + Vue Router 4 + xterm.js 5.3
- **数据库**：无独立数据库，完全跟随 Devops-Glue API 的数据库实例。SQLite / MySQL 8.0+ / MariaDB 10.4+
- **端口**：8081
- **版本**：v1.2.2（Changelog / v1.2 tag 2026-08-08 更新）
- **认证**：与 Devops-Glue API 共享数据库，bcrypt + Bearer token，不可单独使用

## 快速开始

```bash
git clone https://github.com/jeanslw/devops_cd.git
cd devops_cd
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

pip install -r requirements.txt

# 配置 .env
cp .env.example .env
# DB_DRIVER=sqlite（默认，需和 Devops-Glue API 指向同一个 .db 文件，如 ../php_api/config/data/data.db）
# 或 DB_DRIVER=mysql（推荐，需和 Devops-Glue API 共用同一个库）

python main.py
# 访问 http://localhost:8081
```

### Docker Compose 部署

```bash
cp .env.example .env
docker compose up -d --build
# 访问 http://localhost:8081
```

> **SQLite 模式注意**：CD Service 和 Devops-Glue API 共用同一个 SQLite 数据库文件。容器部署时必须将数据库目录挂载为共享卷。推荐生产环境使用 MySQL 或 MariaDB 10.4+，避免 SQLite 并发写入问题。

## 文档

| 文档 | 说明 |
|------|------|
| [用户使用手册](docs/用户使用手册.md) | 功能说明、部署模式、自定义监控、API 参考 |
| [管理员配置手册](docs/管理员配置手册.md) | 环境要求、配置说明、K8s 兼容性、安全配置 |
| [常见问题](docs/常见问题.md) | 常见问题与排查指南 |
| [更新日志](docs/CHANGELOG.md) | 版本发布记录 |
| [架构全景图](docs/用户说明.md) | 系统整体架构图解 |

## 相关项目

- **Devops-Glue** — 持续集成服务，本系统依赖的系统。([https://gitee.com/jeanslw/devops_glue](https://gitee.com/jeanslw/devops_glue))

## 更新日志

- v1.0.0 | 2026-07-15 | 初始版本，衔接 CI 项目完善 CD 部署功能和部署校验，输出数据流日志；增加 SSH 单机 / Docker / K8s 集群的部署，增加主机资源监控，优化 Bot 通知模板。
- v1.1.0 | 2026-07-24 | 新增镜像制品库：数据本地落库，兼容 Harbor API v1/v2，仓库卡片 → Tag 列表 → 扫描漏洞 → 安全删除四层交互，支持定时同步和手动触发。
- v1.2.0 | 2026-07-28 | 前端重构为 Vue 3 + Vite 现代化 SPA；Web Shell 异步化，状态反馈精准；项目结构优化（app/ → backend/）；增加中英文双语支持；增加告警规则模块；增加用户权限管理（admin/deployer/viewer）；自定义监控增强（容量单位解析、诊断面板）；
- v1.2.1 | 2026-07-29 | 配合 Devops-Glue API 适配 RBAC 权限，优化部署日志；部分变量配置迁移到 `docker-compose.yml`；新增 CI 构建管理（Jenkins/GitLab HTTP API 代理）；文档更新。
- v1.2.2 | 2026-08-08 | **新增 Webhook 接收端点**（`POST /api/webhooks/receive/{token}`，32 字符 token 鉴权），支持关联 Bot 自动转发钉钉/企微/自定义通知，事件历史分页浏览、手动转发；新增 CI 构建管理 UI 与 `/api/ci/*` 代理端点；部署 ValueError 通过 SSE 实时透传到前端；全项目 Pyright 类型检查通过 + 统一 DB 接口（SQLite `?`/MySQL `%s` 自动转换）；前端 ErrorView 适配 `error_key` 国际化错误；新增公开 `/api/info` 端点；CD & Webhook 表索引优化；测试警告清理；中英文文档完整同步更新。
## 许可证

MIT

## 联系方式

- 问题与 PR：[GitHub Issues](https://github.com/jeanslw/Devops_CD/issues)
- 邮箱：jeanslw@qq.com