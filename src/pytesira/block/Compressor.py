#!/usr/bin/env python3
"""
Compressor DSP block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels         → int (1 on this DSP, up to 32 per docs)
  get bypass              → bool   (False)
  set bypass              → bool
  toggle bypass           → bool
  get attackTime          → float  (ms, 1.0-2000.0)
  set attackTime          → float
  get releaseTime         → float  (ms, 5.0-10000.0)
  set releaseTime         → float
  get makeupGain          → float  (dB, 0.0-12.0)
  set makeupGain          → float
  get allGainReduction    → [float, ...]   all-channel GR list (read-only)
  get gainReduction {ch}  → float          per-channel GR (read-only)

All settings are block-level (no channel index). gainReduction is
per-channel and read-only (it measures compression activity).
ratio, threshold, kneeWidth are NOT supported on this firmware version.
mute, level, label → NOT supported.

Subscriptions (early-fire pattern — data arrives before +OK):
  allGainReduction  — all channels in one callback
  gainReduction {ch} — individual channel (subscribed per-channel)
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging


class Compressor(Block):
    """
    Dynamic range compressor block.

    Provides bypass, attack/release time and makeup gain control.
    Gain reduction is read-only monitoring via subscription — it reflects
    how much compression is currently being applied per channel.
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

        # Block-level settings
        self.bypass: bool = False
        self.attack_time: float = 10.0    # ms
        self.release_time: float = 50.0   # ms
        self.makeup_gain: float = 0.0     # dB

        # Monitoring (read-only, updated by subscription)
        self.num_channels: int = 1
        self.all_gain_reduction: list[float] = []   # all channels at once
        self.gain_reduction: dict[int, float] = {}  # per-channel

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
            "bypass": self.bypass,
            "attack_time": self.attack_time,
            "release_time": self.release_time,
            "makeup_gain": self.makeup_gain,
            "num_channels": self.num_channels,
        }

    # ------------------------------------------------------------------
    # Subscription lifecycle — early-fire pattern
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called by DSP after __init__ to set up GR monitoring subscriptions."""
        self._subscribe_gr()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """Called by DSP refresh loop for resubscription."""
        return self._subscribe_gr()

    def _subscribe_gr(self) -> list[TTPResponse]:
        results = []
        # Subscribe to allGainReduction (all channels at once)
        try:
            r = self._register_subscription(
                subscribe_type="allGainReduction", channel=None
            )
            results.append(r)
        except Exception as exc:
            self._logger.debug(f"allGainReduction subscribe timed out (normal): {exc}")
        # Subscribe to per-channel gainReduction
        for ch in range(1, self.num_channels + 1):
            try:
                r = self._register_subscription(
                    subscribe_type="gainReduction", channel=ch
                )
                results.append(r)
            except Exception as exc:
                self._logger.debug(
                    f"gainReduction ch{ch} subscribe timed out (normal): {exc}"
                )
        return results

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        sub_type = response.subscription_type

        if sub_type == "allGainReduction":
            val = response.value
            self.all_gain_reduction = (
                [float(v) for v in val] if isinstance(val, list) else [float(val)]
            )

        elif sub_type == "gainReduction":
            try:
                ch = int(response.subscription_channel_id)
                self.gain_reduction[ch] = float(response.value)
            except (ValueError, TypeError):
                pass

        super().subscription_callback(response)

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        def _get(attr):
            try:
                return self._sync_command(f'"{self._block_id}" get {attr}').value
            except Exception:
                return None

        # numChannels — infer from allGainReduction list length if needed
        nc = _get("numChannels")
        if isinstance(nc, int):
            self.num_channels = nc
        else:
            gr_all = _get("allGainReduction")
            self.num_channels = len(gr_all) if isinstance(gr_all, list) else 1

        self.bypass = bool(_get("bypass"))
        self.attack_time = float(_get("attackTime") or 10.0)
        self.release_time = float(_get("releaseTime") or 50.0)
        self.makeup_gain = float(_get("makeupGain") or 0.0)

        # Seed GR values
        self.all_gain_reduction = [0.0] * self.num_channels
        self.gain_reduction = {ch: 0.0 for ch in range(1, self.num_channels + 1)}

    def _load_init_helper(self, helper: dict) -> None:
        self.bypass = bool(helper["bypass"])
        self.attack_time = float(helper["attack_time"])
        self.release_time = float(helper["release_time"])
        self.makeup_gain = float(helper["makeup_gain"])
        self.num_channels = int(helper.get("num_channels", 1))
        self.all_gain_reduction = [0.0] * self.num_channels
        self.gain_reduction = {ch: 0.0 for ch in range(1, self.num_channels + 1)}

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Setters
    # ------------------------------------------------------------------

    def set_bypass(self, value: bool) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set bypass {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.bypass = value

    def set_attack_time(self, value_ms: float) -> None:
        """Set attack time (1.0-2000.0 ms)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set attackTime {value_ms}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.attack_time = value_ms

    def set_release_time(self, value_ms: float) -> None:
        """Set release time (5.0-10000.0 ms)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set releaseTime {value_ms}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.release_time = value_ms

    def set_makeup_gain(self, value_db: float) -> None:
        """Set makeup gain (0.0-12.0 dB)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set makeupGain {value_db}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.makeup_gain = value_db
