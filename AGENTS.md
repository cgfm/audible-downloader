# Repository Guidelines

## Project Structure & Module Organization

Core library sync, SQLite state, path rendering, and AAX/AAXC-to-M4B conversion live in `app/audible_service.py`. `app/web.py` provides the Flask UI and scheduler; `app/audiobookDownloader.py` is the legacy-compatible CLI entry. Server-rendered pages and CSS are under `app/templates/` and `app/static/`. Tests live in `tests/`. `Dockerfile` builds the runtime image, while `README.md` and `ARCHITECTURE.md` document setup and design. Runtime data belongs in mounted `/config` and `/audiobooks` volumes. Do not commit caches, databases, vouchers, downloaded audio, or authentication JSON.

## Build, Test, and Development Commands

- `docker build -t audible-downloader .` builds the production-style Debian image.
- `docker run --rm -v "$PWD/config:/config" -v "$PWD/audiobooks:/audiobooks" audible-downloader` runs it locally; add the environment variables documented in `README.md` when testing templates.
- `python3 -m pytest -q` runs the automated service and web tests.
- `python3 -m compileall -q app tests` performs a quick syntax check without contacting Audible.
- `docker run --rm audible-downloader ffmpeg -version` verifies that the conversion dependency is installed in the image.

Prefer container-based integration validation because the image supplies Flask, `audible-cli`, and `ffmpeg`.

## Coding Style & Naming Conventions

Use Python conventions already present in the module: four-space indentation, `snake_case` for functions and variables, leading underscores for internal helpers, and short docstrings for non-obvious behavior. Keep subprocess arguments as lists and pass `check=True` when failure must stop the operation. Build paths with `os.path.join`, sanitize metadata before using it in paths, and use parameterized SQLite queries. Keep changes focused; avoid broad rewrites of this single-file application.

## Testing Guidelines

Use `pytest`; name files `test_<behavior>.py` and tests `test_<expected_behavior>`. Every processing change should cover success and persisted failure state. Path changes should exercise single-file and multipart outputs, both `AUDIOBOOK_FOLDERS` modes where relevant, template placeholders, manual overrides, force, and overwrite behavior. Mock Audible, filesystem, and ffmpeg interactions; tests must not require a real account. Run the full suite and Docker build before submitting.

## Commit & Pull Request Guidelines

History uses short, lowercase, action-oriented subjects, commonly prefixed with `bugfix:`, `feature:`, `fix:`, or `refactor:`. Follow that pattern, for example `bugfix: preserve multipart vouchers`. Pull requests should explain the user-visible behavior, list validation commands, call out configuration or path changes, and link relevant issues. Include representative logs or before/after path examples when download organization changes; screenshots are usually unnecessary for this CLI/container project.
