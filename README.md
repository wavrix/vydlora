# Vydlora

An open-source, local media workspace powered by the bundled **Lora 1 engine**. Download media, follow creators, manage your queue, and browse your collection from one web interface.

**No Vydlora account, subscription, daily download quota, or paid feature tier.** The app runs on your computer. Platform access rules, rate limits, available disk space, and your own settings still apply.

## Features

- Individual and batch downloads from TikTok, Instagram, and X.
- Creator monitoring, manual scans, missed-post recovery, and supported stories.
- A live queue with pause, resume, retry, cancel, and progress details.
- A searchable media library with thumbnails, photo galleries, and video playback.
- Analytics, activity, notifications, logs, and live engine output.
- Optional Telegram delivery and your own MEGA storage.
- Dark/light themes, keyboard shortcuts, and an optional local browser PIN.

Some platform features require your own signed-in session. Only collect content you have permission to access and save.

## One-command install and run

**Windows 10/11:** open PowerShell and paste:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/wavrix/vydlora/main/install.ps1)))
```

**macOS / Debian / Ubuntu:** open Terminal and paste:

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/wavrix/vydlora/main/install.sh)"
```

The installer gets Vydlora, installs missing Node.js, Python, Git and FFmpeg, prepares the engine packages and Chromium, then starts the app and opens your browser. Compatible installed tools are reused. Windows may show a permission prompt; macOS/Linux may ask for your computer password to install system packages. Linux needs `curl` to retrieve the script.

Windows installs in `%LOCALAPPDATA%\Programs\Vydlora`; macOS/Linux installs in `~/vydlora`. Rerunning the command updates a clean installation and starts it again. It preserves private data and refuses to overwrite source edits or unrelated folders. MEGA and Telegram remain optional connections you configure yourself.

Keep the terminal open while the app runs; use **Ctrl+C** to stop it. To start an existing installation without updating/reinstalling, use the restart command printed by the installer, or `npm start` from the install folder if Node is on your PATH.

Advanced options: `-Directory`, `-NoUpdate`, `-NoStart`, `-NoBrowser` on Windows; `--directory`, `--no-update`, `--no-start`, `--no-browser` on macOS/Linux. Download the script to pass these options. Use the manual steps below for other Linux distributions.

## Manual setup requirements

Use a **Windows, macOS, or Linux computer** with:

1. [Node.js](https://nodejs.org/en/download) **22+**. Node 24 is used for local verification.
2. [Python](https://www.python.org/downloads/) **3.12 recommended** (3.11 minimum). Select **Add Python to PATH** in the Windows installer.
3. [Git](https://git-scm.com/downloads), or download this repository's ZIP and extract it.
4. Internet access for installation and media downloads.

Install [FFmpeg](https://ffmpeg.org/download.html) for the most complete media support. Lora 1 also checks its Python-provided FFmpeg fallback. [MEGAcmd](https://mega.io/cmd) is needed only for MEGA integration.

The interface adapts to phone/tablet screens, but the engine needs a computer. By default, the app is available only on that computer.

## Manual install and run

Open PowerShell on Windows or Terminal on macOS/Linux:

```sh
git clone https://github.com/wavrix/vydlora.git
cd vydlora
npm run setup
npm start
```

Open **[http://127.0.0.1:5173](http://127.0.0.1:5173)** in your browser. Keep the terminal open while using Vydlora. Press **Ctrl+C** there to stop the app and its managed engine.

Setup creates `engine/.venv`, installs the pinned Python packages, and downloads Chromium for creator discovery. First setup can take several minutes. There is no frontend build step and no `npm install` step is needed.

For a ZIP download, open a terminal inside the extracted folder and start at `npm run setup`.

### If Python or Chromium needs help

Select your Python explicitly, for example:

```sh
npm run setup -- --python python3.12
```

On Windows, you can pass the full path to `python.exe` in quotes. After setup, Vydlora uses its own environment automatically; activating it is unnecessary.

On Linux, if Chromium reports missing system libraries, run:

```sh
./engine/.venv/bin/python -m playwright install --with-deps chromium
```

Your system may ask for administrator access to install browser libraries. If Python reports that `venv` is missing, install your distribution's package for your Python version, such as `python3-venv`, then retry setup.

## First use

1. **New:** paste a complete media link, or one link per line for a batch.
2. **Downloads:** view, play, filter, or save collected media.
3. **Monitoring:** add creators and adjust schedules in **Settings → Monitoring**. Enable Instagram/X monitoring when their sessions are configured.
4. **Settings → Cookies:** optionally import your own Netscape-format cookie file for the relevant platform. Session exports can grant access to your social account: keep them private, never commit them, and remove them when no longer needed.
5. **Settings → Telegram / Storage & MEGA:** connect optional services using your own credentials. Neither is required to start Vydlora.

The optional PIN locks this browser's interface. It does not encrypt your disk or make the app a public multiuser service.

## Local files and configuration

The default private workspace is **`.runtime/lora1/`**. It contains the database, media working files, cookies, logs, and related state. MEGA account profiles, when used, have separate data within that workspace. Your library may also refer to media imported from elsewhere.

Runtime data, credentials, generated environments, and test output are excluded from Git. Back up the workspace and separately stored media while Vydlora is stopped. Never upload `.runtime`, `.env`, cookie exports, databases, or account sessions to an issue or public repository.

Defaults work without configuration. To customize them, copy `.env.example` to `.env`:

```dotenv
PORT=5173
LORA1_ENGINE_URL=http://127.0.0.1:8000
LORA1_DATA_DIR=.runtime/lora1
LORA1_MANAGE_ENGINE=1
LORA1_AUTH_TOKEN=
```

Both servers bind to loopback. The managed engine gets a random private token; browser JavaScript never receives it. To attach to an engine you manage yourself, set `LORA1_MANAGE_ENGINE=0`, configure its address/token privately, and restart. Do not expose the local server directly to the public internet.

## Update

Stop Vydlora and back up your private workspace, then run:

```sh
git pull --ff-only
npm run setup
npm start
```

Updating does not require deleting the workspace. Save or commit any source edits before pulling.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| `node` or `npm` is not recognized | Install Node.js and open a new terminal. |
| PowerShell blocks `npm.ps1` | Use `npm.cmd run setup` and `npm.cmd start`, or Command Prompt. |
| Python is missing | Install Python and pass its executable with `--python`. |
| Chromium is missing | Rerun setup; on Linux use the browser dependency command above. |
| Port already in use | Close the old instance, or choose unused web/engine ports in `.env`. |
| Engine unavailable | Check the terminal, rerun setup, and inspect **Settings → Connection**. |
| A platform requests authentication or fails | Check its cookie session, platform availability, and **Logs**. |
| MEGA is unavailable | Install MEGAcmd and connect it in Settings. Other features work without it. |

For bug reports, include your OS, Node/Python versions, and a redacted error. Remove cookies, tokens, email addresses, and personal media links first.

## Development

```sh
npm run check
npm test
npm run test:engine
```

With the app running, `npm run test:api` performs read-only API/media checks. See [CONTRIBUTING.md](CONTRIBUTING.md) and [engine notes](docs/engine.md).

```text
public/              Browser interface, styles, icons, and fonts
engine/app/          Lora 1 API, downloaders, monitoring, and storage
engine/bootstrap.py  Private workspace and engine startup
engine/tests/        Isolated engine tests
lib/                 Engine lifecycle and local security helpers
scripts/             Setup, startup, and checks
tests/               Web transport, security, and rendering tests
server.mjs           Local web server and API/WebSocket proxy
```

## License

Source: [MIT License](LICENSE). Bundled Inter fonts retain their SIL Open Font License. Dependencies and optional tools retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Vydlora is independent and is not affiliated with the media platforms or Topaz Labs.
