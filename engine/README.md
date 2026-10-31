# Lora 1 engine

The complete Python engine used by Vydlora is bundled here. It provides the existing REST API and WebSocket, platform downloaders, monitoring workers, Telegram integration, account profiles, and MEGA storage/sync.

Run `npm run setup` from the repository root to install dependencies in this folder’s `.venv`, then `npm start` to run the app and engine together. `npm run engine` starts the engine alone.

`bootstrap.py` resolves private project-local data before importing the API. Existing local profiles are upgraded without deleting their source databases. `app/core/compat.py` centralizes historical data/protocol keys required to keep old collections and cloud backups readable.

The engine uses the pinned versions in `requirements.txt`. Unit tests use temporary stores and mocked external services. No credentials, live databases, media, or Python environments belong in Git.
