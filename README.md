# VibeStick

[中文说明](README.zh-CN.md)

![VibeStick home screen showing Codex and Claude providers](assets/brand/home-screen-preview.png)

![VibeStick voice input flow showing StickS3 recording states and Mac HUD](assets/brand/voice-input-preview.png)

VibeStick turns an M5Stack StickS3 into a tiny desktop companion for coding agents: status, 5H/7D usage, alerts, and push-to-talk transcription into your Mac.

VibeStick targets M5Stack StickS3 hardware and is not an official M5Stack project. Third-party agent names such as Codex and Claude describe compatible local tools and integrations only.

This branch can fetch Codex and Claude 5H/7D remaining quota through Sub2-Usage on the server-side Bridge. The token never enters the firmware; StickS3 shows `account_name` when available and falls back to the configured account id.

Current integrated firmware/Bridge record: [VibeStick 0.1.6](docs/releases/0.1.6.md).

## What you'll need (prepare first)

- [ ] M5Stack StickS3 and a USB-C data cable.
- [ ] A Mac on the same network as the StickS3.
- [ ] Wi-Fi name and password. The Wi-Fi must be 2.4 GHz; StickS3 / ESP32-S3 does not support 5 GHz Wi-Fi.
- [ ] To show Claude 5H/7D usage: this feature is off by default (safer). It needs the Claude Code CLI (run `claude` then `/login` in Terminal) and `VIBE_STICK_CLAUDE_USAGE=on` in `.env`.
- [ ] An ASR API key for speech transcription. Recommended: SiliconFlow at <https://cloud.siliconflow.cn/i/7ZCoy9fU>. It works directly in China, has free quota, and is OpenAI-compatible. The demo video uses SiliconFlow. You can also use another OpenAI-compatible ASR provider's `base_url` and model name instead.

Building the firmware needs ESP-IDF v5.5.x — a one-time toolchain install (~1 GB, a few minutes). The install steps below set it up for you; no need to pre-install. Reference: Espressif's [ESP-IDF v5.5.1 ESP32-S3 guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.1/esp32s3/get-started/index.html).

## Install

You can do this manually, or hand the command steps to an AI coding agent such as Claude Code and Codex.

### Windows bridge quick start

Windows supports the Python bridge, local Codex / Claude observation, StickS3 audio uploads, and automatic paste into the focused window. The macOS HUD and Mac microphone fallback remain macOS-only; on Windows, voice input uses audio uploaded by the StickS3.

Run in PowerShell:

```powershell
git clone https://github.com/GaryGaryyy/VibeStick.git
cd VibeStick
.\scripts\setup.ps1
.\scripts\dev.ps1
```

Edit Wi-Fi values in `firmware\sticks3\include\vibe_stick_secrets.h` and the ASR key in `.env` before using the device. Allow private-network access if Windows Firewall prompts, then open `http://127.0.0.1:8765/health` to verify the bridge. Firmware builds still require ESP-IDF v5.5.x; use Espressif's Windows Installation Manager and run `idf.py build flash` from an ESP-IDF PowerShell.

> Legend: steps marked 👤 are PHYSICAL steps that need a human to act directly, such as plugging in the cable, long-pressing or short-pressing the power button, and granting macOS permissions in System Settings. AI agents should run the shell steps in order, then pause at each 👤 step and ask the user to complete it before continuing.

1. Clone the repo and create local config files:

```sh
git clone https://github.com/GaryGaryyy/VibeStick.git
cd VibeStick
./scripts/setup.sh
```

2. Fill the local config values the human prepared:

```sh
open -e firmware/sticks3/include/vibe_stick_secrets.h
open -e .env
```

In `vibe_stick_secrets.h`, set Wi-Fi SSID, Wi-Fi password, and the Mac bridge host. `scripts/setup.sh` tries to auto-fill `VIBE_STICK_BRIDGE_HOST` with the detected en0 LAN IP when the file still has the example placeholder.

In `.env`, set the ASR key and any provider choices. The default ASR example is SiliconFlow:

```sh
VIBE_STICK_ASR_PROVIDER=openai-compatible
VIBE_STICK_ASR_BASE_URL=https://api.siliconflow.cn/v1
VIBE_STICK_ASR_API_KEY=your-siliconflow-key
VIBE_STICK_ASR_MODEL=FunAudioLLM/SenseVoiceSmall
```

3. 👤 Plug the StickS3 into the Mac with the USB-C data cable.

4. 👤 Put the StickS3 into download mode: long-press the side power button until the blue LED double-blinks and the screen turns off. This is required for ESP32-S3 flashing.

5. Install ESP-IDF if it is not already present, then load it into the current shell. This is a one-time toolchain install with a large ~1 GB download and can take a few minutes. Run the load command in every new terminal before `idf.py`:

```sh
if [ ! -d "$HOME/esp/esp-idf" ]; then
  mkdir -p ~/esp && cd ~/esp
  git clone -b v5.5.1 --recursive https://github.com/espressif/esp-idf.git
  cd esp-idf && ./install.sh esp32s3
fi
. "$HOME/esp/esp-idf/export.sh"
```

Or install via Espressif's [official guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.1/esp32s3/get-started/index.html). If `install.sh` fails, ensure `git`, `python3`, and `cmake` are present, or follow the official guide. Adjust the path if ESP-IDF is installed elsewhere.

6. Build and flash the firmware:

```sh
cd firmware/sticks3
idf.py -p <port> build flash
cd ../..
```

If you do not know the port, run:

```sh
ls /dev/cu.*
```

Wait for `Hash of data verified`.

7. 👤 Short-press the power button to wake the screen. The blue LED should turn off, the screen should turn on, and you should see the VibeStick home screen. Before networking is ready, it may show offline.

8. Install the local macOS bridge and HUD:

```sh
./scripts/install.sh
```

9. 👤 When macOS prompts that `python3.14` wants Accessibility control, click "Open System Settings" and enable it. This permission is needed for paste injection.

10. Check the setup:

```sh
./scripts/doctor.sh
```

Aim for all required checks to pass. Then glance at the StickS3: Codex / Claude status and 5H / 7D usage should show real values when the corresponding local provider data is available.

If Codex works but the Claude column shows `--%`, that is expected: Claude usage is disabled by default (safer), so to display it set `VIBE_STICK_CLAUDE_USAGE=on` and make sure Claude Code is logged in via `claude` and `/login`.

11. 👤 Open any text box, long-press the front blue button, speak, and release. VibeStick should transcribe and paste the text automatically.

For development without installing LaunchAgents, run `./scripts/dev.sh` from the repository root instead of `./scripts/install.sh`.

## Troubleshooting

### `command not found: idf.py`

ESP-IDF is installed but not loaded into the current shell, or it has not been installed yet. Source ESP-IDF's `export.sh`, then run `idf.py` again:

```sh
. $HOME/esp/esp-idf/export.sh
```

Adjust the path if your ESP-IDF checkout is somewhere else. Run this once in every new terminal before using `idf.py`.

### Flashing says "Device not configured" or cannot open the serial port

Unplug and replug the USB-C data cable. Put the StickS3 into download mode again: long-press the side power button until the blue LED double-blinks and the screen turns off. Run `ls /dev/cu.*` to find the port, then retry `idf.py -p <port> build flash`.

### StickS3 cannot join Wi-Fi

Use a 2.4 GHz Wi-Fi network. StickS3 / ESP32-S3 does not support 5 GHz Wi-Fi.

### Recording transcribes but does not paste

Grant Accessibility permission to the Python runner that performs paste injection. On macOS, open System Settings -> Privacy & Security -> Accessibility, then enable `python3.14` or the terminal / launcher that runs VibeStick.

### "No transcription adapter configured"

Configure ASR in `.env`, especially `VIBE_STICK_ASR_PROVIDER`, `VIBE_STICK_ASR_BASE_URL`, and `VIBE_STICK_ASR_API_KEY`, then run:

```sh
./scripts/install.sh
```

### Cannot find `.env`

`.env` is a hidden file. Open it with:

```sh
open -e .env
```

### Transcription fails or times out with SSL/network errors

The ASR provider is usually unreachable from your current network. For users in China, try SiliconFlow at <https://cloud.siliconflow.cn/i/7ZCoy9fU>. Otherwise configure a reachable OpenAI-compatible ASR provider or your network proxy.

## Configuration

Do not commit real API keys, local tokens, Wi-Fi credentials, local logs, or generated recording files.

Empty values in `.env` generally mean "use the built-in default". `scripts/dev.sh` loads `.env` from the repository root. `scripts/install.sh` copies `.env` to `~/Library/Application Support/VibeStick/.env`, and the LaunchAgent runner loads that installed file.

### Core settings

- `VIBE_STICK_PROJECT_ROOT`: project root used for local Codex session observation.
- `VIBE_STICK_PROJECT_NAME`: optional display-name override.
- `VIBE_STICK_PROVIDER`: active provider selection, `auto`, `codex`, or `claude`; default `auto`.
- `VIBE_STICK_BRIDGE_TOKEN`: shared token required whenever the bridge binds outside loopback, such as `0.0.0.0`.
- `VIBE_STICK_MAX_RECORDING_AUDIO_BYTES`: max `/recording/audio` body size, default `2000000`.
- `VIBE_STICK_RECORDING_USE_MAC_MIC`: set to `0` to disable Mac microphone fallback.
- `VIBE_STICK_AUTO_ENTER`: set to `1` to press Return after pasting.

### StickS3 power behavior

- The screen backlight turns off after 30 seconds; this is standby, not power-off.
- On battery, 60 minutes without interaction triggers PMIC power-off. Wi-Fi, audio, LCD and the unused IMU rail are released first; deep sleep is used if the PMIC rail doesn't cut successfully.
- Double-click the side reset/power button for hardware power-off. Long-pressing it enters download mode and still consumes power while waiting to be flashed.

### ASR option 1: LAN FunASR (lowest latency)

This sends the completed StickS3 WAV to a FunASR WebSocket service on the LAN. It needs no API key and can load hotwords and normalization rules from the companion hotword API.

```sh
VIBE_STICK_ASR_PROVIDER=local-funasr
VIBE_STICK_ASR_BASE_URL=ws://192.168.31.100:10095
VIBE_STICK_FUNASR_HOTWORDS_URL=http://192.168.31.100:10096/api/hotwords
VIBE_STICK_FUNASR_CHUNK_DELAY_MS=2
VIBE_STICK_ASR_LANGUAGE=zh
```

### ASR option 2: SiliconFlow

```sh
VIBE_STICK_ASR_PROVIDER=openai-compatible
VIBE_STICK_ASR_BASE_URL=https://api.siliconflow.cn/v1
VIBE_STICK_ASR_API_KEY=your-siliconflow-key
VIBE_STICK_ASR_MODEL=FunAudioLLM/SenseVoiceSmall
VIBE_STICK_ASR_LANGUAGE=zh
VIBE_STICK_ASR_TIMEOUT_SECONDS=15
VIBE_STICK_ASR_ATTEMPTS=2
```

Audio sent to a cloud ASR provider leaves the Mac.

### ASR option 3: any OpenAI-compatible provider

Use any provider that accepts `POST {base_url}/audio/transcriptions`.

```sh
VIBE_STICK_ASR_PROVIDER=openai-compatible
VIBE_STICK_ASR_BASE_URL=https://example.com/v1
VIBE_STICK_ASR_API_KEY=your-api-key
VIBE_STICK_ASR_MODEL=provider-model-name
```

Groq is also supported as an overseas preset:

```sh
VIBE_STICK_ASR_PROVIDER=groq
VIBE_STICK_ASR_API_KEY=your-groq-key
```

The legacy aliases `VIBE_STICK_GROQ_API_KEY`, `VIBE_STICK_GROQ_MODEL`, and `VIBE_STICK_GROQ_LANGUAGE` remain supported.

### ASR option 4: local command (offline)

```sh
VIBE_STICK_TRANSCRIBE_CMD=/path/to/transcribe-command
VIBE_STICK_TRANSCRIBE_TIMEOUT_SECONDS=120
```

The command receives the recording session JSON on stdin and should print the final transcript to stdout.

### Claude usage

To see Claude 5H/7D usage, use `VIBE_STICK_PROVIDER=claude` or `VIBE_STICK_PROVIDER=auto`, set `VIBE_STICK_CLAUDE_USAGE=on`, and make sure Claude Code CLI has logged in through Terminal with `claude` and `/login`.

- `VIBE_STICK_CLAUDE_USAGE`: set to `on` to fetch real Claude Code subscription usage; default `off`.
- `CLAUDE_CODE_OAUTH_TOKEN`: optional Claude Code OAuth access token. If unset, the bridge tries local Claude Code keychain/file credentials.
- `VIBE_STICK_CLAUDE_USAGE_INTERVAL_SECONDS`: Claude usage poll cadence, default `300`, minimum `30`.

Claude usage support calls an undocumented Anthropic endpoint using the user's local Claude Code subscription credentials and client headers. It is opt-in, may break without notice, and never exposes the token or raw endpoint response through the bridge HTTP API. If no successful Claude usage snapshot has ever been captured, the StickS3 shows `--%`; after a successful snapshot, temporary usage refresh failures keep the last known values and mark them stale.

## Project layout

```text
VibeStick/
  README.md
  README.zh-CN.md
  .env.example
  docs/
  firmware/sticks3/
  bridge/src/vibe_stick/
  app/macos/VibeStickHUD/
  scripts/
  tests/
```

## Checks

```sh
python3 -m compileall -q bridge/src tests
PYTHONPATH=bridge/src python3 -m unittest discover -s tests
bash -n scripts/setup.sh scripts/doctor.sh scripts/install.sh
```

Firmware builds still require ESP-IDF:

```sh
cd firmware/sticks3
. $HOME/esp/esp-idf/export.sh
idf.py build
```

## Current limits

- This is a cleaned prototype, not a packaged Mac app or DMG.
- The firmware targets M5Stack StickS3 only.
- Codex quota is inferred from local Codex session JSONL events with `rate_limits`; it is not an official quota API.
- Claude usage comes from an undocumented Claude Code OAuth endpoint and is disabled by default.
- ASR reliability depends on microphone capture, uploaded PCM quality, provider availability, and configured model.

## Contributing & Security

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability,
see [SECURITY.md](SECURITY.md) (please report privately).

## License

VibeStick is released under the MIT License. See [LICENSE](LICENSE).
