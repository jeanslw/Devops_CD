# Devops-Glue CD
A FastAPI-based continuous deployment service that works alongside [Devops-Glue API](https://github.com/jeanslw/Devops-Glue.git) to deploy Harbor images to Docker or Kubernetes clusters. Crafted from 10+ years of real-world operations experience.

> **Not an enterprise-scale control plane — a battle-tested Swiss Army knife for lean teams.**
>
> One unified panel for multi-Git-platform CI pipelines, Harbor artifact management, and multi-mode CD deployment — no more switching between Gitee, Jenkins, and Harbor just to align a single image tag.

<p align="center">
  <a href="https://github.com/jeanslw/Devops_CD"><img src="https://img.shields.io/github/v/release/jeanslw/Devops_CD?style=flat-square&label=Release" alt="Release"></a>
  <a href="https://github.com/jeanslw/Devops_CD/stargazers"><img src="https://img.shields.io/github/stars/jeanslw/Devops_CD?style=flat-square&logo=github" alt="Stars"></a>
  <a href="https://github.com/jeanslw/Devops_CD/commits/main"><img src="https://img.shields.io/github/last-commit/jeanslw/Devops_CD?style=flat-square&label=Last%20Commit" alt="Last Commit"></a>
  <a href="https://github.com/jeanslw/Devops_CD/blob/main/LICENSE"><img src="https://img.shields.io/github/license/jeanslw/Devops_CD?style=flat-square" alt="License"></a>
</p>

**[Chinese](README_ZH-CN.md)**

![System Overview](system_info.png)
![System Status](system_running.png)

## Overview

- **Repository**：https://github.com/jeanslw/devops_cd.git or https://gitee.com/jeanslw/devops_cd.git
- **Language**：Python 3.10+
- **Framework**：FastAPI + uvicorn (async)
- **Frontend**：Vue 3 + Vite + Vue Router 4 + xterm.js 5.3
- **Database**：No standalone database — shares the same database instance as Devops-Glue API (SQLite / MySQL 8.0+ / MariaDB 10.4+)
- **Port**：8081
- **Version**：v1.2.2 (Changelog v1.2 updated 2026-08-08)
- **Authentication**：Shared database with Devops-Glue API; bcrypt + Bearer token. Cannot be used independently.
<details>
## <summary> Quick Start (click to expand)</summary>

```bash
git clone https://github.com/jeanslw/devops_cd.git
cd devops_cd
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

pip install -r requirements.txt

# Configure environment
cp .env.example .env
# DB_DRIVER=sqlite (default; must point to the same .db file as the PHP API, e.g. ../php_api/config/data/data.db)
# or DB_DRIVER=mysql (recommended; must share the same database with the PHP API)

python main.py
# Open http://localhost:8081
```

### Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
# Open http://localhost:8081
```
</details>
> **SQLite caveat**：The CD Service shares the same SQLite database file with the PHP API. When deploying in containers, mount the database directory as a shared volume. For production, MySQL or MariaDB 10.4+ is strongly recommended to avoid SQLite write contention.

## Documentation

- [USER_MANUAL](docs/USER_MANUAL.md)
- [ADMIN_MANUAL](docs/ADMIN_MANUAL.md)
- [FAQ](docs/FAQ.md)
- [CHANGELOG](docs/CHANGELOG.md)
- [ARCHITECTURE](docs/ARCHITECTURE.md)

## Related Projects

- **Devops-Glue** — CI service ([GitHub](https://github.com/jeanslw/Devops-Glue) | [Gitee](https://gitee.com/jeanslw/devops_glue))

## Contact
PR：[GitHub Issues](https://github.com/jeanslw/Devops_CD/issues)
For feature requests or bug reports, open an issue on the GitHub repository, or email: jeanslw@qq.com