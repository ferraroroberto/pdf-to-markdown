# Automation Monorepo

## 🧭 Context
**WHAT**: A monolithic repository containing various automation tools, scripts, and assistants.
**WHY**: To streamline personal and professional workflows across different domains (Audio, Email, Data, Video, System files.).
**STACK**: Python 3.x (primary), Batch/Shell scripts, Windows 10+, PowerShell 7+.

## 🗺️ Codebase Map
- `dbt/`: DBT projects for data transformation and analytics.
- `python/`: Python automation scripts (airflow, cloud integrations, libraries, local utilities).
- `sql/`: SQL scripts, processes, and table definitions.
- `exports/`, `import/`: Data export and import folders.
- `refactoring/`: Code refactoring tools, samples, and guided examples.
- `resources/`: Configuration files, prompts, icons, and user data.
- `scripts/`: Shell scripts for environment activation, authentication, and utilities.
- `sas/`: SAS scripts and files.
- `.venv/`: Local virtual environment (DO NOT TOUCH).

## 🚀 Workflow (The "HOW")
1.  **Plan**: Before coding, propose a brief phased implementation plan (files to change, strategy).
2.  **Approve**: Wait for user confirmation to approve the plan.
3.  **Implement**: specific, scoped changes. One change at a time, wait for approval, then the next.
4.  **Test**: Verify using the local `.venv` interpreter directly.
5.  **Commit and push**: When I ask to commit and push, use the feature branch as target.

## ⚖️ Core Principles (The "Ten Commandments")
1.  **Config First**: Use JSON for config, `.env` for secrets. No hardcoded paths/creds.
2.  **Logging**: Use `logging` info module with emojis (ℹ️, ⚠️, ❌). Never use `print()` never write logs.
3.  **Naming**: Files/Functions=`snake_case`, Classes=`PascalCase`, Constants=`UPPER_CASE`.
4.  **No Secrets**: Never commit `.env` or credentials.
5.  **Direct Execution**: Never "activate" venv. Use `& .\.venv\Scripts\python.exe`.
6.  **Scope Discipline**: Do only what is asked. No "nice-to-haves". No code bloat.
7.  **Dependencies**: Pin versions. Use existing `.venv`.
8.  **PowerShell**: Use PS 7+ syntax (`&&`, chaining).
9.  **Imports**: Standard Lib → Third Party → Local.
10. **Error Handling**: Fail fast with clear error messages.
11. **Streamlit**: Use `width='stretch'` instead of deprecated `use_container_width=True`.