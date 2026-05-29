#!/usr/bin/env python3
"""
AudioMeter (Peak or RMS Meter) DSP block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels         → int (2)
  get levels              → [float, float, ...]  all-channel dB readings
  get level {ch}          → float (dB, e.g. -54.6)
  get label {ch}          → str  ("Chan 1")        — readable AND writable
  set label {ch}          → str
  get holdEnabled {ch}    → bool (False)
  set holdEnabled {ch}    → bool
  get holdTime {ch}       → float (ms, 0-10000)
  set holdTime {ch}       → float
  get indefiniteHold {ch} → bool (False)
  set indefiniteHold {ch} → bool
  mute / minLevel / maxLevel / invert → NOT supported

Subscriptions:
  levels  (no channel) — fires immediately (before +OK); must register token
                         first, catch timeout gracefully. Delivers list of dB floats.
  level {ch}           — per-channel variant; same early-fire pattern.

This is a read-only meter block for level monitoring. Hold settings are the
only writable configuration surface.
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging


class AudioMeter(Block):
    """
    Audio meter (Peak or RMS) block.

    Exposes real-time per-channel level readings via a levels subscription
    (all channels at once). Hold settings are readable and writable.
    Level values are read-only — this block measures signal, it does not control it.
    """

    VERSION = "0.1.0"

    def __init__(
        self,
        block_id: str,
        exit_flag: Event,
        connected_flag: Event,
        command_queue: Queue,
        subscriptions: dict,
        init_helper: str | None = None,
    ) -> None:

        self._logger = logging.getLogger(f"{__name__}.{block_id}")

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # Per-channel state
        self.levels: dict[int, float] = {}       # real-time dB readings
        self.labels: dict[int, str] = {}         # channel labels (writable)
        self.hold_enabled: dict[int, bool] = {}
        self.hold_time: dict[int, float] = {}
        self.indefinite_hold: dict[int, bool] = {}

        # Load from cache or live query
        try:
            if init_helper is not None:
                self._load_init_helper(init_helper)
            else:
                raise ValueError("no helper")
        except Exception as exc:
            self._logger.debug(f"init helper unusable: {exc}, querying DSP")
            self._query_attributes()

        self._init_helper = {
            ch: {
                "label": self.labels.get(ch, ""),
                "hold_enabled": self.hold_enabled.get(ch, False),
                "hold_time": self.hold_time.get(ch, 0.0),
                "indefinite_hold": self.indefinite_hold.get(ch, False),
            }
            for ch in self.levels
        }

    # ------------------------------------------------------------------
    # Subscriptions — AudioMeter fires data before +OK (same as BFMic)
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called by DSP after __init__ to subscribe to level updates."""
        self._subscribe_levels()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """Called by DSP refresh loop for resubscription after reconnect."""
        return self._subscribe_levels()

    def _subscribe_levels(self) -> list[TTPResponse]:
        results = []
        try:
            r = self._register_subscription(subscribe_type="levels", channel=None)
            results.append(r)
        except Exception as exc:
            # AudioMeter sends subscription data before +OK — timeout is expected
            self._logger.debug(f"levels subscribe timed out (normal): {exc}")
        return results

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        if response.subscription_type == "levels":
            vals = response.value if isinstance(response.value, list) else [response.value]
            for i, v in enumerate(vals):
                ch = i + 1
                if ch in self.levels:
                    try:
                        self.levels[ch] = float(v)
                    except (TypeError, ValueError):
                        pass
        super().subscription_callback(response)

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        # Discover channel count from levels list
        try:
            vals = self._sync_command(f'"{self._block_id}" get levels').value
            num_channels = len(vals) if isinstance(vals, list) else 2
        except Exception:
            num_channels = 2

        for ch in range(1, num_channels + 1):
            def _get(attr, ch=ch):
                try:
                    return self._sync_command(
                        f'"{self._block_id}" get {attr} {ch}'
                    ).value
                except Exception:
                    return None

            self.levels[ch] = float(_get("level") or -100.0)
            self.labels[ch] = str(_get("label") or f"Ch {ch}")
            self.hold_enabled[ch] = bool(_get("holdEnabled"))
            self.hold_time[ch] = float(_get("holdTime") or 0.0)
            self.indefinite_hold[ch] = bool(_get("indefiniteHold"))

    def _load_init_helper(self, helper: dict) -> None:
        for ch_key, data in helper.items():
            ch = int(ch_key)
            self.levels[ch] = -100.0  # populated by first subscription callback
            self.labels[ch] = str(data.get("label", f"Ch {ch}"))
            self.hold_enabled[ch] = bool(data.get("hold_enabled", False))
            self.hold_time[ch] = float(data.get("hold_time", 0.0))
            self.indefinite_hold[ch] = bool(data.get("indefinite_hold", False))

    def refresh_status(self) -> None:
        """Re-query current levels (useful if subscription has not started)."""
        try:
            vals = self._sync_command(f'"{self._block_id}" get levels').value
            if isinstance(vals, list):
                for i, v in enumerate(vals):
                    ch = i + 1
                    if ch in self.levels:
                        self.levels[ch] = float(v)
        except Exception:
            pass

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Setters (hold configuration only — level is read-only)
    # ------------------------------------------------------------------

    def set_label(self, channel: int, value: str) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set label {channel} "{value}"'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.labels[channel] = value

    def set_hold_enabled(self, channel: int, value: bool) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set holdEnabled {channel} {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.hold_enabled[channel] = value

    def set_hold_time(self, channel: int, value_ms: float) -> None:
        """Set peak hold duration (0-10000 ms)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set holdTime {channel} {value_ms}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.hold_time[channel] = value_ms

    def set_indefinite_hold(self, channel: int, value: bool) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set indefiniteHold {channel} {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.indefinite_hold[channel] = value
