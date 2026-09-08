# Repository Guidelines

## Project Structure & Module Organization

OptiLib is a Flask application for drug-library optimization using NSGA-II.
- `webapp/app.py`: HTTP routes, pipeline orchestration, and session management.
- `webapp/core/`: optimization (`algorithm.py`), selectivity calculations (`selectivity.py`), and session defaults/reset logic (`state.py`).
- `webapp/templates/` and `webapp/static/`: HTML templates, JavaScript, CSS, and images.
- `tests/test_core.py`: regression tests for computational logic and session state.
- `MolPrice/`: molecular price prediction code and model assets.
- `scripts/`: database preparation and MolPort update utilities.
- `database/` and `webapp/output/`: ignored local databases and generated session exports.
- `Dockerfile`, `docker-compose.yml`, `nginx/`, and `DEPLOYMENT.md`: deployment configuration and instructions.

## Build, Test, and Development Commands

Run commands from the repository root. Use Python 3.12 to match the container.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python webapp/app.py
```

These commands create an environment, install dependencies, and serve the app on port 5000. Configure `.env` from `.env.example`; database-backed workflows use local ChEMBL and MolPort databases.

- `python -m unittest discover -s tests -v`: run regression tests.
- `docker compose build`: build service images.
- `docker compose up -d optilib`: start the containerized web application.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for Python functions and variables, `PascalCase` for classes, and uppercase constants. Follow existing JavaScript `camelCase` conventions. Keep numerical logic in `webapp/core/` and HTTP handling in `app.py`. No repository-wide formatter or linter configuration is present; match surrounding code and avoid unrelated formatting changes.

## Testing Guidelines

Tests use standard-library `unittest`, with `test_*.py` files and `test_*` methods. Use small NumPy fixtures and deterministic seeds for numerical regressions. The suite includes a real, short optimization run and requires no local databases. No coverage threshold is configured. Run the suite after core changes; manually check affected browser flows for UI changes.

## Commit & Pull Request Guidelines

History uses Conventional Commit prefixes such as `feat:`, `refactor:`, and `chore:` with imperative descriptions. Keep commits focused. PRs should explain the problem, resulting behavior, and validation; link relevant issues and include screenshots for visible UI changes.

## Configuration & Runtime Notes

Keep secrets in `.env`; do not commit credentials, databases, or generated exports. Preserve session isolation and CSRF handling. Production Gunicorn uses one worker with multiple threads because session state is held in memory.
