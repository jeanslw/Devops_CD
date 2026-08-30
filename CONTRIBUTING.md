# Contributing Guide

First of all, thank you for considering contributing to **Devops-CD**! Devops-CD is a FastAPI + Vue 3 continuous deployment (CD) service that pairs with [Devops-Glue](https://github.com/jeanslw/Devops-Glue) (the CI service) to deploy Harbor images to Docker or Kubernetes clusters. This guide helps you get involved smoothly — whether reporting a bug, proposing a feature, or submitting code.

## Core Principles

Before we start, please understand the two core philosophies of this project:

1. **Devops-CD is the "CD" half, not the "CI" half**: It deploys images to Docker / Kubernetes, while CI responsibilities (build, tag) belong to Devops-Glue. Any contribution should respect this boundary and avoid reimplementing CI logic.
2. **Interface-first over direct table reads**: Prefer calling an existing API over reading a database table directly. Schema or index changes belong on the CI (Glue) side — coordinate with the CI owner rather than altering the shared database from CD.

---

## How to Report a Bug

If you find a bug, please open a new Issue on GitHub Issues and include as much of the following as possible:

- **Short description**: A clear and concise description of the problem.
- **Steps to reproduce**: Detailed steps, including the version and configuration used.
- **Expected behavior**: What you expected to see.
- **Actual behavior**: What actually happened, including error screenshots or logs if available.
- **Environment information**:
    - Devops-CD version (or commit hash)
    - Python version
    - Database type (MySQL / SQLite) and version
    - Deployment mode (ssh / compose / k8s kubectl / helm / argocd / fluxcd)
    - Devops-Glue (CI) version, if relevant

## How to Propose a New Feature or Improvement

Before proposing a new feature, search existing Issues to avoid duplication. When submitting a feature suggestion, please explain:

- **What pain point does this solve?** Describe the problem you're facing in real scenarios.
- **What is your proposed solution?** Be as specific as possible. If you can, describe the API or UI interaction you envision.
- **Is this feature aligned with the CD responsibility boundary?** Explain how it improves deployment, rather than duplicating CI logic.

## Code Contribution Workflow

### 1. Communication First
If you plan to implement a major feature or refactor, please **discuss it in an Issue first** to ensure your direction aligns with the maintainer's vision, avoiding wasted effort.

### 2. Set Up Development Environment
- Ensure you have **Python 3.10+** installed.
- Fork this repository and clone your fork locally.
- Create a virtual environment and install dependencies:

  ```bash
  python -m venv venv
  source venv/bin/activate   # Windows: venv\Scripts\activate
  pip install -r requirements.txt
  ```

- Copy `.env.example` to `.env` and configure the database / Harbor / CI API.
- Start the backend: `python main.py` (serves on `http://localhost:8081`).
- For the frontend: `cd frontend && npm install`, then `npm run dev` for development or `npm run build` to produce the static bundle.

### 3. Write Code
- **Coding style**: Python code follows **ruff** (`ruff check .`); the config lives in `pyproject.toml`. Keep lines within reason (120 chars where practical).
- **Testing**: Add or update unit tests under `backend/tests/` and run them with `python -m pytest backend/tests`.
- **i18n**: User-facing log messages use the `S("deploy_log.xxx")` helper and must have both `en` and `zh` entries in `frontend/src/locales/`.
- **Documentation**: Your contribution must include or update relevant documentation:
    - Update usage instructions in `README.md` / `README_ZH-CN.md` or under `docs/`.
    - If it's a new API endpoint, update the OpenAPI / endpoint list in the manuals.
    - If new configuration items are introduced, update `.env.example` and the administrator manual.

### 4. Commit Message

Please use clear, descriptive commit messages, following **Conventional Commits**:

    <type>(optional scope): <short description>

    <optional detailed description>

- **Common types**:
  - `feat`: A new feature
  - `fix`: A bug fix
  - `docs`: Documentation changes
  - `style`: Code formatting (no functional impact)
  - `refactor`: Code refactoring (neither a new feature nor a bug fix)
  - `test`: Adding or modifying tests
  - `chore`: Build process or tooling changes

**Example**:

    feat(rollback): add native rollback for argocd

    Rollback now calls the ArgoCD rollback API and streams
    internationalized logs over SSE.

### 5. Open a Pull Request (PR)

- Ensure your PR is based on the latest `main` branch.
- In the PR description, clearly explain what problem it solves and link the related Issue (e.g., `Closes #123`).
- Ensure CI checks (if configured) pass and there are no conflicts with the base branch.

## Code of Conduct

Contributors are expected to adhere to the [Contributor Covenant](https://www.contributor-covenant.org/version/2/0/code_of_conduct/). We expect all interactions to be open, inclusive, and respectful.

## Getting Help

If you have any questions, feel free to ask in an Issue or contact the maintainer via email (jeanslw@qq.com).

Thank you again for your contribution! 🎉
