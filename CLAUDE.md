# pytesira fork — we-are-theBorg

Fork of https://github.com/enp6s0/pytesira for use with the Biamp Tesira HA integration.
Companion repo: https://github.com/we-are-theBorg/ha-biamp-tesira (`C:\Dev\ha-biamp-tesira\`)

## What this fork adds vs upstream

### Block modules (src/pytesira/block/)
New or modified modules not in upstream:
- `AEC.py` — meters excluded from _register_base_subscriptions (prevents TTP queue flood)
- `AECReference.py`, `AudioDelay.py`, `AudioInput.py`, `AudioMeter.py`, `AVBPOEAmp.py`
- `BluetoothControlStatus.py`, `BluetoothInput.py`, `BluetoothOutput.py`
- `Compressor.py`, `LogicGate.py`
- `MatrixMixer.py` — NxM routing matrix with crosspoint mute/level; "ixj" channel encoding
- `PeakLim.py` — block-level only; no subscriptions (DSP rejects them)
- `UsbInputEx.py`, `UsbOutputEx.py` — extends BaseLevelMute; 2ch
- `BFMic.py` (v0.2.0) — per-channel beamtracking (2 independent channels per device)

### dsp.py fixes
- `__sync_cmd_process_loop`: `self._logger` → `self.__logger` (name mangling fix); delivers `-ERR` TTPResponse to waiting mailbox on exception
- `__device_data_refresh_loop`: each task wrapped in try/except — a command timeout can no longer crash the loop thread

## Live DSP for testing
- Host: 172.17.9.1, SSH port 22, user: default, pass: default
- TesiraForteX, firmware 5.6.1.2

## Probing protocol
**Never use pytesira DSP class for probing** — it auto-subscribes everything, flooding the SSH channel.
Use raw paramiko SSH only: send command, drain response, check for +OK/-ERR.
See probe scripts in `C:\Dev\ha-biamp-tesira\` (probe_verify.py, probe_raw.py, etc.)

## Python on this machine
`python3` does NOT work in Bash — use `/c/Windows/py.exe` or the PowerShell tool.

## Commit and push workflow
```bash
git add src/pytesira/block/SomeBlock.py
git commit -m "Description"
git remote set-url origin https://<PAT>@github.com/we-are-theBorg/pytesira.git
git push origin main
git remote set-url origin https://github.com/we-are-theBorg/pytesira.git
```
After pushing, update the `requirements` pin in `C:\Dev\ha-biamp-tesira\custom_components\biamp_tesira\manifest.json` to the new commit SHA.