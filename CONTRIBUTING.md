# Contributing

Follow the [setup guide](README.md), then create a branch for your change. Keep the web app and Lora 1 engine together; do not depend on neighboring checkouts.

Run the relevant checks before opening a pull request:

```sh
npm run check
npm test
npm run test:engine
```

With the app running, `npm run test:api` checks the read-only integration contract. Native browser checks are in `tests/rendering.html`; serve the repository over local HTTP to run them, without publishing your private workspace.

Use synthetic fixtures and temporary databases. Never commit `.env`, `.runtime`, databases, cookie exports, tokens, real media, sessions, generated environments, or downloaded executables. Redact logs and screenshots.

Keep changes focused, preserve local data/API compatibility, and describe behavior and verification. Retain keyboard access, responsive layouts, and reduced-motion support. New dependencies/assets need compatible licenses and required notices.
