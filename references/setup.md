# First-use setup

## Requirements

- macOS with Python 3.10 or later
- Android Platform Tools with `adb` available
- one Android device connected by USB with USB debugging authorized
- Xiaohongshu installed and already signed in
- device model and resolution matching `expected_device_model` and `expected_screen_size`; the
  shipped coordinate fallbacks were verified on `ONEPLUS A6003` at `1080x2280`

## Configuration

Copy `assets/xhs_config.example.json` to:

```text
~/.config/codex/xhs-android-publisher.json
```

Set the expected Xiaohongshu nickname and absolute local paths. Do not put the device PIN in this
JSON.

Store the PIN in macOS Keychain:

```bash
security add-generic-password \
  -a xhs-operator \
  -s codex-xhs-device-pin \
  -w
```

The command prompts securely. Never pass the PIN as a command-line argument, save it in a project,
or include it in task output.

## Device preparation

Run:

```bash
adb devices -l
scripts/xhs prepare
scripts/xhs doctor
```

`prepare` installs the bundled Chinese clipboard helper when missing and grants the image permission
where supported. `doctor` must return:

- exactly one device
- `model_supported: true`
- `xiaohongshu_installed: true`
- `input_helper_installed: true`
- `screen_supported: true`
- `ok: true`

## UI drift

The operator uses Xiaohongshu accessibility resource IDs plus a small number of coordinate fallbacks.
App updates can change them. If a control is missing, stop and capture `scripts/xhs doctor` plus
`scripts/xhs snapshot`; do not replace missing semantic checks with blind taps.
