# Devops-Glue 架构全景图

## 整体数据流

```
┌─────────────────────────────────────────────────────────────┐
│                        CODE PUSH                            │
│  GitLab / Gitee / GitHub / Gitea  →  Webhook Trigger       │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                     CI 层：Devops-Glue API (PHP)             │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐   ┌──────────────┐     │
│  │   Jenkins     │  │  GitLab CI   │   │   自定义 CI   │     │
│  │ BuildProvider │  │ BuildProvider │   │ BuildProvider │     │
│  └──────┬───────┘  └──────┬───────┘   └──────┬───────┘     │
│         └─────────────────┼─────────────────┘              │
│                           ↓                                 │
│              Build → Docker Image → Harbor Registry         │
│                           ↓                                 │
│              scan-sync → ci_pipeline_tags                   │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                     CD 层：cd_service (Python)               │
│                                                             │
│   选择 Project + Tag  ──→  部署执行                         │
│                                                             │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│   │  SSH 脚本     │  │Docker Compose│  │  Kubernetes   │     │
│   │  Ansible     │  │  SFTP + up   │  │ kubectl/Helm  │     │
│   │              │  │              │  │ ArgoCD/FluxCD │     │
│   └──────────────┘  └──────────────┘  └──────────────┘     │
│                           ↓                                 │
│              cd_deploy_logs (部署记录)                       │
│                           ↓                                 │
│              钉钉 / 企业微信 Webhook 通知                    │
└─────────────────────────────────────────────────────────────┘
```

## 组件关系

```
┌──────────────────────────────────────┐
│          共享数据库 (SQLite/MySQL)      │
│                                      │
│  ci_job_git_map        ← CI 只读    │
│  ci_pipeline_tags      ← CI 写 / CD 读 │
│  cd_servers            ← CD 维护    │
│  cd_deploy_logs        ← CD 写      │
│  cd_bots               ← CD 维护    │
│  admin_users           ← 共享       │
└──────────┬───────────────────────────┘
           │
    ┌──────┴──────┐
    ↓             ↓
┌────────┐   ┌────────┐
│ PHP CI │   │Python CD│
│:8080   │   │:8081    │
└────────┘   └────────┘
```

## 部署模式矩阵

| 部署类型 | 模式 | 底层实现 |
|----------|------|---------|
| SSH 单机 | 自定义命令 | Shell 脚本，支持 `{image}` `{tag}` `{project}` 占位符 |
| SSH 单机 | Ansible Playbook | `ansible-playbook -e image={image} -e tag={tag}` |
| Docker Compose | 远程 YAML | `cd {path} && docker compose up -d` |
| Docker Compose | 在线编写 | SFTP 上传 compose YAML → 自动建目录 → 启动 |
| K8s kubectl | SSH apply | SSH 到 master 执行 `kubectl apply -f` |
| K8s Helm | SSH kubectl | `helm upgrade --install` + 版本验证 |
| K8s Argo CD | REST API | PATCH image → sync → 轮询 Healthy |
| K8s Flux CD | SSH kubectl | PATCH 资源 → wait ready |

## 设计模式

- **Strategy 模式**：`BuildProviderInterface`（PHP CI）/ `Deployer`（Python CD）抽象基类 + Registry 注册表
- **工厂模式**：`GitProviderFactory` 按 URL 自动匹配 Git 平台适配器
- **双驱动数据库**：SQLite / MySQL 统一接口，一套代码两种模式

## 解决问题

中小型企业 DevOps 工具链碎片化：
- Git 平台（GitLab/Gitee/GitHub/Gitea）→ 统一对接
- CI 引擎（Jenkins/GitLab CI）→ 双通道统一
- 镜像仓库（Harbor）→ 扫描同步
- 部署目标（SSH/Docker/K8s）→ 统一执行
- 通知（钉钉/企微）→ 自动推送

**一句话：把散落一地的 DevOps 工具，用一层胶水粘起来。**
