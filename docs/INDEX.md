# Devops-Glue CD Documentation

| Document | English | 中文 |
|----------|---------|------|
| User Manual | [USER_MANUAL.md](USER_MANUAL.md) | [USER_MANUAL_ZH.md](USER_MANUAL_ZH.md) |
| Admin Manual | [ADMIN_MANUAL.md](ADMIN_MANUAL.md) | [ADMIN_MANUAL_ZH.md](ADMIN_MANUAL_ZH.md) |
| FAQ | [FAQ.md](FAQ.md) | [FAQ_ZH.md](FAQ_ZH.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) | [CHANGELOG_ZH.md](CHANGELOG_ZH.md) |
| Architecture | [ARCHITECTURE.md](ARCHITECTURE.md) | [ARCHITECTURE_ZH.md](ARCHITECTURE_ZH.md) |

## Versioning / 版本管理

### 版本格式 Version Format

遵循语义化版本（SemVer）`MAJOR.MINOR.PATCH`，如 `1.5.0`。Tag 与 GitHub Release 统一加 `v` 前缀：`v1.5.0`。预发布版本追加后缀：`v1.5.0-beta`。

- `MAJOR`：不兼容的 API 变更或重大功能重构
- `MINOR`：向后兼容的新功能
- `PATCH`：向后兼容的 Bug 修复

### 分支规则 Branch Rules

| 分支 | 说明 | 示例 |
|---|---|---|
| `main` | 主干分支，功能开发的合并目标 | `main` |
| `release/X.Y.Z` | 发布分支，从 `main` 切出，发布后合回 | `release/1.5.0` |
| `feature/*`（可选） | 功能分支 | `feature/deploy-note` |
| `hotfix/*`（可选） | 紧急修复分支 | `hotfix/fix-token-expire` |

### Tag 与 Release Tag & Release

- **Tag 格式**：正式版 `vX.Y.Z`（如 `v1.5.0`）；预发布 `vX.Y.Z-{alpha|beta|rc|preview}`（如 `v1.5.0-beta`）
- **GitHub Release**：与 Tag 同名（`v1.5.0`），push Tag 后由 Actions 自动生成，包含自动整理的 Changelog
- **镜像发布**：push `vX.Y.Z` Tag 自动构建多架构镜像并推送 `ghcr.io/{owner}/devops-cd`

### 自动化流程 Automation

| Workflow | 触发条件 | 职责 |
|---|---|---|
| `ci.yml` | push 到 `main` / `release/**` / `vX.Y.Z` Tag；PR 到 `main` / `release/**` | 代码质量（ruff + pip-audit）、单元测试（Python 3.10/3.11/3.12）、前端构建 |
| `docker-publish.yml` | push `vX.Y.Z` Tag | 构建多架构镜像并推送 GHCR |
| `release.yml` | push `vX.Y.Z` Tag | 自动生成 GitHub Release（含 Changelog） |
| `security.yml` | 每次 push / PR | Python 语法检查 + 硬编码密钥扫描 |

### 变更记录 Changelog

CHANGELOG 每个版本条目使用三级格式 `## vX.Y.Z`（如 `## v1.5.0`），与 Tag、Release 保持一致。