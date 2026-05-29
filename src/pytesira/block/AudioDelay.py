#!/usr/bin/env python3
"""
AudioDelay DSP block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get bypass        → bool  (True = bypassed)
  set bypass        → bool
  toggle bypass     → bool
  get delay         → float (current value in active units, read-only raw)
  get maxDelay      → int   (5 | 10 | 50 | 100 | 500 | 1000 | 2000 ms)
  get units         → MILLISECOND | CENTIMETER | METER | INCH | FOOT
  get unitsDelay    → {"units": str, "delay": float}
  set unitsDelay    → {"units": str, "delay": float}  OR  <float> (in current units)

No channel index — AudioDelay is a block-level entity with no per-channel split.
No working subscriptions confirmed; entities should use should_poll=True.

Note: numChannels is technically 1 but not a useful attribute for this block type.
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging

VALID_UNITS = frozenset({"MILLISECOND", "CENTIMETER", "METER", "INCH", "FOOT"})
VALID_MAX_DELAYS = frozenset({5, 10, 50, 100, 500, 1000, 2000})


class AudioDelay(Block):
    """
    Audio Delay DSP block.

    Delays audio by a configurable amount (0 to maxDelay in chosen units).
    Can be bypassed. No per-channel split; operates on the whole signal path.
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

        # Load from block-map cache or live query
        try:
            if init_helper is not None:
                self._load_init_helper(init_helper)
            else:
                raise ValueError("no helper")
        except Exception as exc:
            self._logger.debug(f"init helper unusable: {exc}, querying DSP")
            self._query_attributes()

        # Persist to block-map cache
        self._init_helper = {
            "bypass": self.bypass,
            "delay": self.delay,
            "max_delay": self.max_delay,
            "units": self.units,
        }

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        def _get(attr):
            try:
                return self._sync_command(f'"{self._block_id}" get {attr}').value
            except Exception:
                return None

        self.bypass: bool = bool(_get("bypass"))
        self.delay: float = float(_get("delay") or 0.0)
        self.max_delay: int = int(_get("maxDelay") or 10)
        self.units: str = str(_get("units") or "MILLISECOND")

    def _load_init_helper(self, helper: dict) -> None:
        self.bypass = bool(helper["bypass"])
        self.delay = float(helper["delay"])
        self.max_delay = int(helper["max_delay"])
        self.units = str(helper["units"])

    def refresh_status(self) -> None:
        """Re-query delay and bypass state (for polling entities)."""
        try:
            ud = self._sync_command(f'"{self._block_id}" get unitsDelay').value
            if isinstance(ud, dict):
                self.delay = float(ud.get("delay", self.delay))
                self.units = str(ud.get("units", self.units))
        except Exception:
            pass
        try:
            self.bypass = bool(
                self._sync_command(f'"{self._block_id}" get bypass').value
            )
        except Exception:
            pass

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Setters
    # ------------------------------------------------------------------

    @property
    def delay_ms(self) -> float:
        """Current delay converted to milliseconds regardless of display units."""
        conversions = {
            "MILLISECOND": 1.0,
            "CENTIMETER": 1000.0 / 34300.0,
            "METER": 1000.0 / 343.0,
            "INCH": 1000.0 / 13504.0,
            "FOOT": 1000.0 / 1125.33,
        }
        factor = conversions.get(self.units, 1.0)
        return round(self.delay * factor, 6)

    def set_bypass(self, value: bool) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set bypass {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.bypass = value

    def set_delay_ms(self, value_ms: float) -> None:
        """Set delay in milliseconds, converting to the block's active display units."""
        conversions = {
            "MILLISECOND": 1.0,
            "CENTIMETER": 34300.0 / 1000.0,
            "METER": 343.0 / 1000.0,
            "INCH": 13504.0 / 1000.0,
            "FOOT": 1125.33 / 1000.0,
        }
        factor = conversions.get(self.units, 1.0)
        converted = value_ms * factor
        cmd_res = self._sync_command(
            f'"{self._block_id}" set unitsDelay {converted}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.delay = converted

    def set_units_delay(self, units: str, delay: float) -> None:
        """Set delay and units together using the combined unitsDelay attribute."""
        if units not in VALID_UNITS:
            raise ValueError(f"Invalid units '{units}', must be one of {VALID_UNITS}")
        cmd_res = self._sync_command(
            f'"{self._block_id}" set unitsDelay {{"units":"{units}" "delay":{delay}}}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.units = units
        self.delay = delay
