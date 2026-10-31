# Lora 1 integration

The engine’s runtime source, requirements, and relevant regression tests are included under `engine/`. Native desktop UI, installers, build output, virtual environments, personal maintenance fixtures, credentials, and databases were not imported into the source tree. The original source folder was preserved.

Vydlora creates its own Python environment during setup. The application no longer imports code, executes a Python environment, or reads assets from a neighboring project. Its runtime dependencies are Node.js, Python, installed engine packages, and optional external services/tools selected by the user.

## Lifecycle

`server.mjs` starts `EngineHost` when the configured local engine is not already available. The host launches `engine/bootstrap.py` using `engine/.venv`, supplies the local data root and a private token, and waits for readiness. Existing separately managed engines are attached without taking over their process.

Owned engines stop gracefully with Vydlora. After an account switch, the server restarts its owned engine so the selected profile opens before monitoring and storage resume. Separately managed processes must be restarted by their owner.

## Compatibility

User-facing UI, API identity, logs, configuration documentation, and setup commands use **Lora 1** or **Lora 1 engine**. A few historical names remain only as compatibility keys for database upgrades, old environment settings, authentication headers/cookies, legacy media locations, and the encrypted cloud snapshot namespace. Renaming those external data identifiers would disconnect existing collections or backups.

Database upgrades use SQLite’s backup API, including pending WAL contents, verify the new copy, and retain the old file. Existing media paths and cloud account information are preserved. New profiles use `lora1.db` and the Vydlora storage defaults.

## GitHub preparation

The public repository URL is `https://github.com/wavrix/vydlora`. Runtime data, private configuration, cookies, session files, logs, generated environments, caches, and build/test artifacts are ignored. The imported source was checked for external project dependencies. Credentials are configured after cloning; there is no seeded account or sample database in the application.

The repository contains CI checks, an MIT source license, and the Inter font's original license. The app runs locally without a Vydlora account or subscription.
