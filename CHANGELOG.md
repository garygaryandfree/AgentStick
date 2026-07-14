# Changelog

## v0.1.6 - 2026-07-15

- Added low-latency StickS3-to-FunASR streaming with stable segment locking, tail-only correction, silence filtering, hotwords, and URL/developer-term normalization.
- Added the Windows input-relay protocol used to stream provisional and final text into the focused local or remote-desktop application.
- Added server-side Sub2-Usage polling for Codex/Claude 5-hour and 7-day remaining quota plus account-name display; credentials remain on the Bridge.
- Restored front-button Send/Enter and Codex/Claude task completion, approval, and error sounds, including deferred playback while recording.
- Added BMI270 pickup/flip display control, 60-minute battery idle shutdown, PMIC rail cleanup, and deep-sleep fallback.
- Added Unraid Docker deployment assets, Windows PowerShell helpers, a larger firmware partition, and expanded automated tests.

See [the complete 0.1.6 release record](docs/releases/0.1.6.md).

## v0.1.4

Initial public release of VibeStick — a tiny desktop companion for coding agents on M5Stack StickS3.

- Home screen shows Codex and Claude providers with live status (running / idle / done / approval / error / offline) and independent 5-hour / 7-day usage bars.
- Opt-in real Claude Code subscription usage (5H / 7D) via an undocumented Anthropic endpoint using local credentials; disabled by default, and the token / raw responses are never logged.
- Push-to-talk voice input: record on the StickS3, transcribe via any OpenAI-compatible ASR (e.g. SiliconFlow), and paste into the focused app; a local-command / fully-offline path is also supported.
- Alerts (done / approval / error) play from whichever provider raises them, on the StickS3 speaker.
- First-run helpers (`scripts/setup.sh`, `scripts/doctor.sh`), bridge token authentication, and a bilingual README (English + 中文) with clearly-marked physical steps.

Licensed under MIT.
