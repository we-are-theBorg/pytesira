#!/usr/bin/env python3
"""
PeakLim DSP block — brick-wall peak limiter.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels    → int (1 — block-level only, no per-channel indexing)
  get bypass         → bool
  set bypass         → bool
  get threshold      → float  (dB, e.g. 15.0)
  set threshold      → float
  get releaseTime    → float  (ms, e.g. 100.0)
  set releaseTime    → float

NOT supported: attackTime, makeupGain, ratio, kneeWidth, gainReduction,
  lookaheadEnable/Time, level, mute, label, holdEnabled/holdTime.
  All per-channel variants (e.g. bypass 1) return "too many arguments".

Subscriptions: NONE. The DSP explicitly rejects subscribe on all three
  settable attributes ("operation subscribe not supported on attribute").
  State is queried once on init and updated only via setters.

pytesira type string discovery:
  "<id>" get BLOCKTYPE  → -ERR ... PeakLimInterface::Attributes
  → module name: PeakLim
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging


class PeakLim(Block):
    """
    Brick-wall peak limiter block.

    Single block-level controls: bypass, threshold, releaseTime.
    No subscriptions — state is only queried on init and updated via setters.
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

        # Block-level state
        self.bypass: bool = False
        self.threshold: float = 0.0    # dB
        self.release_time: float = 100.0  # ms
        self.num_channels: int = 1

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
            "threshold": self.threshold,
            "release_time": self.release_time,
            "num_channels": self.num_channels,
        }

    # ------------------------------------------------------------------
    # No subscriptions
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """PeakLim does not support subscriptions."""
        pass

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        def _get(attr):
            try:
                return self._sync_command(
                    f'"{self._block_id}" get {attr}'
                ).value
            except Exception:
                return None

        nc = _get("numChannels")
        if nc is not None:
            try:
                self.num_channels = int(nc)
            except (TypeError, ValueError):
                pass

        bypass_val = _get("bypass")
        if bypass_val is not None:
            self.bypass = bool(bypass_val)

        threshold_val = _get("threshold")
        if threshold_val is not None:
            try:
                self.threshold = float(threshold_val)
            except (TypeError, ValueError):
                pass

        release_val = _get("releaseTime")
        if release_val is not None:
            try:
                self.release_time = float(release_val)
            except (TypeError, ValueError):
                pass

    def _load_init_helper(self, helper: dict) -> None:
        self.bypass = bool(helper["bypass"])
        self.threshold = float(helper["threshold"])
        self.release_time = float(helper["release_time"])
        self.num_channels = int(helper.get("num_channels", 1))

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    def refresh_status(self) -> None:
        """Re-query all attributes from DSP."""
        self._query_attributes()

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

    def set_threshold(self, value_db: float) -> None:
        """Set limiter ceiling (dB)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set threshold {value_db}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.threshold = value_db

    def set_release_time(self, value_ms: float) -> None:
        """Set release time (ms)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set releaseTime {value_ms}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.release_time = value_ms
