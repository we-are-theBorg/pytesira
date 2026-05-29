#!/usr/bin/env python3
"""
MatrixMixer DSP block — NxM audio routing matrix with per-crosspoint gain.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  InputsMixer: 8 inputs × 2 outputs, labels, delay not enabled.

TOPOLOGY (no index):
  get numInputs                → int  (2-256)
  get numOutputs               → int  (1-256)
  get delayEnabled             → bool (whether delay hardware is present)

INPUT STRIPS (per input index 1..numInputs):
  get/set inputLabel {i}       → str
  get/set inputLevel {i}       → float  (dB, inputMinLevel..inputMaxLevel)
  get/set inputMinLevel {i}    → float  (-100.0..inputMaxLevel)
  get/set inputMaxLevel {i}    → float  (inputMinLevel..12.0)
  get/set inputMute {i}        → bool
  subscribe inputLevel {i}     → early-fire float
  subscribe inputMute {i}      → early-fire bool

OUTPUT STRIPS (per output index 1..numOutputs):
  get/set outputLabel {j}      → str
  get/set outputLevel {j}      → float  (dB, outputMinLevel..outputMaxLevel)
  get/set outputMinLevel {j}   → float  (-100.0..outputMaxLevel)
  get/set outputMaxLevel {j}   → float  (outputMinLevel..12.0)
  get/set outputMute {j}       → bool
  subscribe outputLevel {j}    → early-fire float
  subscribe outputMute {j}     → early-fire bool

CROSSPOINTS (per input i, output j):
  get/set crosspointLevelState {i} {j} → bool   (route enabled/disabled)
  get/set crosspointLevel {i} {j}      → float  (dB, -100..0)
  get/set crosspointDelay {i} {j}      → float  (ms, 0-250, only if delayEnabled)
  get/set crosspointDelayState {i} {j} → bool   (only if delayEnabled)
  subscribe crosspointLevelState {i} {j} → early-fire bool
  subscribe crosspointLevel {i} {j}      → early-fire float

Crosspoint subscriptions use a custom token channel_id format "ixj"
(e.g. "S_crosspointLevelState_2x1_InputsMixer") to encode both indexes
within pytesira's existing 4-part token format.

All subscriptions use the early-fire pattern — data arrives before +OK.
Timeouts are caught and ignored; routing is pre-registered.

ARCHITECTURE NOTES for Home Assistant integration:
  - Each input strip → Number (level) + Switch (mute)
  - Each output strip → Number (level) + Switch (mute)
  - Each crosspoint → Switch (routing enabled/disabled)
  - Crosspoint gains are available for advanced use via set_crosspoint_level()
  - For large matrices (>CROSSPOINT_QUERY_MAX crosspoints), routing state
    is not pre-populated — use set_crosspoint_state() and get_routing_matrix()
    on demand.
"""
from __future__ import annotations

from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging

# Threshold above which crosspoint auto-subscribe is skipped (too chatty)
CROSSPOINT_SUBSCRIBE_MAX = 64
# Threshold above which crosspoint initial query is skipped (too slow)
CROSSPOINT_QUERY_MAX = 256


class MixerInput:
    """State container for one input strip."""

    __slots__ = ["label", "level", "min_level", "max_level", "muted"]

    def __init__(
        self,
        label: str = "",
        level: float = 0.0,
        min_level: float = -100.0,
        max_level: float = 12.0,
        muted: bool = False,
    ) -> None:
        self.label = label
        self.level = level
        self.min_level = min_level
        self.max_level = max_level
        self.muted = muted

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "level": self.level,
            "min_level": self.min_level,
            "max_level": self.max_level,
            "muted": self.muted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MixerInput":
        return cls(
            label=str(data.get("label", "")),
            level=float(data.get("level", 0.0)),
            min_level=float(data.get("min_level", -100.0)),
            max_level=float(data.get("max_level", 12.0)),
            muted=bool(data.get("muted", False)),
        )


class MixerOutput:
    """State container for one output strip."""

    __slots__ = ["label", "level", "min_level", "max_level", "muted"]

    def __init__(
        self,
        label: str = "",
        level: float = 0.0,
        min_level: float = -100.0,
        max_level: float = 12.0,
        muted: bool = False,
    ) -> None:
        self.label = label
        self.level = level
        self.min_level = min_level
        self.max_level = max_level
        self.muted = muted

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "level": self.level,
            "min_level": self.min_level,
            "max_level": self.max_level,
            "muted": self.muted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MixerOutput":
        return cls(
            label=str(data.get("label", "")),
            level=float(data.get("level", 0.0)),
            min_level=float(data.get("min_level", -100.0)),
            max_level=float(data.get("max_level", 12.0)),
            muted=bool(data.get("muted", False)),
        )


class MatrixMixer(Block):
    """
    NxM audio routing matrix with per-crosspoint gain and optional delay.

    This is the primary audio routing mechanism in Tesira — it connects
    audio inputs (sources) to outputs (destinations) with individual
    gain control at each intersection. All attributes are live and
    push-updated via subscriptions.

    Key concepts:
      inputs  — numbered 1..numInputs; each has label, level, mute
      outputs — numbered 1..numOutputs; each has label, level, mute
      routing — 2D matrix: routing[i][j] = True means input i feeds output j
      crosspoint_levels — gain at each routed crosspoint (dB, -100..0)

    Crosspoint subscriptions use an "ixj" channel_id encoding that is
    transparent to pytesira's subscription routing system.
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

        # Topology (populated on init)
        self.num_inputs: int = 0
        self.num_outputs: int = 0
        self.delay_enabled: bool = False

        # Input and output strips (keyed by 1-based index)
        self.inputs: dict[int, MixerInput] = {}
        self.outputs: dict[int, MixerOutput] = {}

        # Routing matrix: routing[input_idx][output_idx] = bool
        self.routing: dict[int, dict[int, bool]] = {}
        # Crosspoint gain: crosspoint_levels[input_idx][output_idx] = float (dB)
        self.crosspoint_levels: dict[int, dict[int, float]] = {}

        # Track which crosspoints have active subscriptions
        self._subscribed_crosspoints: set[tuple[int, int]] = set()

        # Load from block-map cache or full live query
        try:
            if init_helper is not None:
                self._load_init_helper(init_helper)
            else:
                raise ValueError("no helper")
        except Exception as exc:
            self._logger.debug(f"init helper unusable: {exc}, querying DSP")
            self._query_attributes()

        self._init_helper = self._build_init_helper()

    # ------------------------------------------------------------------
    # Subscription lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called by DSP after __init__. Subscribe to input/output and crosspoint state."""
        self._subscribe_strips()
        self._subscribe_crosspoints()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """Called by DSP refresh loop for resubscription after reconnect."""
        results = []
        results += self._subscribe_strips()
        results += self._subscribe_crosspoints()
        return results

    def _subscribe_strips(self) -> list[TTPResponse]:
        """Subscribe to input/output level and mute — the high-priority signals."""
        results = []
        for i in self.inputs:
            for sub_type in ("inputLevel", "inputMute"):
                results += self._sub_early(sub_type, channel=i)
        for j in self.outputs:
            for sub_type in ("outputLevel", "outputMute"):
                results += self._sub_early(sub_type, channel=j)
        return results

    def _subscribe_crosspoints(self) -> list[TTPResponse]:
        """
        Subscribe to crosspointLevelState for each crosspoint — only if the
        matrix is small enough to be practical. Skip for very large matrices.
        """
        total = self.num_inputs * self.num_outputs
        if total > CROSSPOINT_SUBSCRIBE_MAX:
            self._logger.info(
                f"Matrix {self.num_inputs}x{self.num_outputs}={total} crosspoints "
                f"exceeds subscribe threshold ({CROSSPOINT_SUBSCRIBE_MAX}); "
                f"use get_routing_matrix() / refresh_status() for crosspoint state."
            )
            return []

        results = []
        for i in range(1, self.num_inputs + 1):
            for j in range(1, self.num_outputs + 1):
                results += self._register_crosspoint_subscription(
                    "crosspointLevelState", i, j
                )
        return results

    def _sub_early(self, sub_type: str, channel: int) -> list[TTPResponse]:
        """Register one early-fire subscription; catch timeout gracefully."""
        try:
            r = self._register_subscription(subscribe_type=sub_type, channel=channel)
            return [r]
        except Exception as exc:
            self._logger.debug(f"{sub_type} ch{channel} timed out (normal): {exc}")
            return []

    def _register_crosspoint_subscription(
        self, sub_type: str, in_idx: int, out_idx: int
    ) -> list[TTPResponse]:
        """
        Register a crosspoint subscription using the "ixj" channel_id encoding.

        Standard pytesira _register_subscription only supports a single channel
        index. Crosspoints need two indexes (input, output). We encode them as
        "{in_idx}x{out_idx}" in the channel_id slot of the token, which survives
        TTPResponse.publish_token.split("_", 3) correctly since we split max 3 times.

        Token format: S_crosspointLevelState_{i}x{j}_{block_id}
        """
        channel_id = f"{in_idx}x{out_idx}"
        sub_name = f"S_{sub_type}_{channel_id}_{self._block_id}"
        sub_cmd = (
            f'"{self._block_id}" subscribe {sub_type} '
            f'{in_idx} {out_idx} "{sub_name}"'
        )

        # Pre-register so rx_loop can dispatch before +OK arrives
        self._subscriptions[sub_name] = (self, self._block_id, sub_name, sub_cmd)
        self._subscribed_crosspoints.add((in_idx, out_idx))

        try:
            r = self._sync_command(sub_cmd)
            return [r]
        except Exception as exc:
            self._logger.debug(
                f"{sub_type} [{in_idx},{out_idx}] timed out (normal): {exc}"
            )
            return []

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        sub_type = response.subscription_type
        ch_id = response.subscription_channel_id

        if sub_type == "inputLevel":
            in_idx = _to_int(ch_id)
            if in_idx and in_idx in self.inputs:
                self.inputs[in_idx].level = _to_float(response.value)

        elif sub_type == "inputMute":
            in_idx = _to_int(ch_id)
            if in_idx and in_idx in self.inputs:
                self.inputs[in_idx].muted = bool(response.value)

        elif sub_type == "outputLevel":
            out_idx = _to_int(ch_id)
            if out_idx and out_idx in self.outputs:
                self.outputs[out_idx].level = _to_float(response.value)

        elif sub_type == "outputMute":
            out_idx = _to_int(ch_id)
            if out_idx and out_idx in self.outputs:
                self.outputs[out_idx].muted = bool(response.value)

        elif sub_type == "crosspointLevelState":
            in_idx, out_idx = _parse_crosspoint_id(ch_id)
            if in_idx and out_idx:
                self.routing.setdefault(in_idx, {})[out_idx] = bool(response.value)

        elif sub_type == "crosspointLevel":
            in_idx, out_idx = _parse_crosspoint_id(ch_id)
            if in_idx and out_idx:
                self.crosspoint_levels.setdefault(in_idx, {})[out_idx] = _to_float(
                    response.value
                )

        super().subscription_callback(response)

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        """Full live query of all mixer attributes."""
        def _get(cmd):
            try:
                return self._sync_command(f'"{self._block_id}" get {cmd}').value
            except Exception:
                return None

        self.num_inputs = int(_get("numInputs") or 0)
        self.num_outputs = int(_get("numOutputs") or 0)
        self.delay_enabled = bool(_get("delayEnabled"))

        # Query all input strips
        for i in range(1, self.num_inputs + 1):
            self.inputs[i] = MixerInput(
                label=str(_get(f"inputLabel {i}") or f"Input {i}"),
                level=_to_float(_get(f"inputLevel {i}")),
                min_level=_to_float(_get(f"inputMinLevel {i}"), -100.0),
                max_level=_to_float(_get(f"inputMaxLevel {i}"), 12.0),
                muted=bool(_get(f"inputMute {i}")),
            )

        # Query all output strips
        for j in range(1, self.num_outputs + 1):
            self.outputs[j] = MixerOutput(
                label=str(_get(f"outputLabel {j}") or f"Output {j}"),
                level=_to_float(_get(f"outputLevel {j}")),
                min_level=_to_float(_get(f"outputMinLevel {j}"), -100.0),
                max_level=_to_float(_get(f"outputMaxLevel {j}"), 12.0),
                muted=bool(_get(f"outputMute {j}")),
            )

        # Query routing matrix (skip if too large)
        total = self.num_inputs * self.num_outputs
        if total <= CROSSPOINT_QUERY_MAX:
            for i in range(1, self.num_inputs + 1):
                self.routing[i] = {}
                self.crosspoint_levels[i] = {}
                for j in range(1, self.num_outputs + 1):
                    state = _get(f"crosspointLevelState {i} {j}")
                    self.routing[i][j] = bool(state)
                    level = _get(f"crosspointLevel {i} {j}")
                    self.crosspoint_levels[i][j] = _to_float(level, 0.0)
        else:
            self._logger.info(
                f"Matrix too large ({total} crosspoints) for full routing query; "
                f"routing state initialized to unknown."
            )
            for i in range(1, self.num_inputs + 1):
                self.routing[i] = {}
                self.crosspoint_levels[i] = {}

    def _load_init_helper(self, helper: dict) -> None:
        """Load slow-changing attributes from block-map cache; query live state."""
        self.num_inputs = int(helper.get("num_inputs", 0))
        self.num_outputs = int(helper.get("num_outputs", 0))
        self.delay_enabled = bool(helper.get("delay_enabled", False))

        # Restore input/output metadata from cache
        for i_str, data in helper.get("inputs", {}).items():
            i = int(i_str)
            self.inputs[i] = MixerInput.from_dict(data)
        for j_str, data in helper.get("outputs", {}).items():
            j = int(j_str)
            self.outputs[j] = MixerOutput.from_dict(data)

        # Re-query live state (levels, mutes, routing change at runtime)
        self._refresh_live_state()

    def _refresh_live_state(self) -> None:
        """Re-query all mutable state from the DSP."""
        def _get(cmd):
            try:
                return self._sync_command(f'"{self._block_id}" get {cmd}').value
            except Exception:
                return None

        for i in self.inputs:
            self.inputs[i].level = _to_float(_get(f"inputLevel {i}"))
            self.inputs[i].muted = bool(_get(f"inputMute {i}"))
        for j in self.outputs:
            self.outputs[j].level = _to_float(_get(f"outputLevel {j}"))
            self.outputs[j].muted = bool(_get(f"outputMute {j}"))

        total = self.num_inputs * self.num_outputs
        if total <= CROSSPOINT_QUERY_MAX:
            for i in range(1, self.num_inputs + 1):
                self.routing.setdefault(i, {})
                self.crosspoint_levels.setdefault(i, {})
                for j in range(1, self.num_outputs + 1):
                    state = _get(f"crosspointLevelState {i} {j}")
                    self.routing[i][j] = bool(state)
                    level = _get(f"crosspointLevel {i} {j}")
                    self.crosspoint_levels[i][j] = _to_float(level, 0.0)

    def refresh_status(self) -> None:
        """Re-query all live state (level, mute, routing) from the DSP."""
        self._refresh_live_state()

    def _build_init_helper(self) -> dict:
        return {
            "num_inputs": self.num_inputs,
            "num_outputs": self.num_outputs,
            "delay_enabled": self.delay_enabled,
            "inputs": {str(i): inp.to_dict() for i, inp in self.inputs.items()},
            "outputs": {str(j): out.to_dict() for j, out in self.outputs.items()},
        }

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._build_init_helper()}

    # ------------------------------------------------------------------
    # Input strip setters
    # ------------------------------------------------------------------

    def set_input_level(self, input_idx: int, value_db: float) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set inputLevel {input_idx} {value_db}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if input_idx in self.inputs:
            self.inputs[input_idx].level = value_db

    def set_input_mute(self, input_idx: int, value: bool) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set inputMute {input_idx} {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if input_idx in self.inputs:
            self.inputs[input_idx].muted = value

    def set_input_label(self, input_idx: int, label: str) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set inputLabel {input_idx} "{label}"'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if input_idx in self.inputs:
            self.inputs[input_idx].label = label

    # ------------------------------------------------------------------
    # Output strip setters
    # ------------------------------------------------------------------

    def set_output_level(self, output_idx: int, value_db: float) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set outputLevel {output_idx} {value_db}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if output_idx in self.outputs:
            self.outputs[output_idx].level = value_db

    def set_output_mute(self, output_idx: int, value: bool) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set outputMute {output_idx} {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if output_idx in self.outputs:
            self.outputs[output_idx].muted = value

    def set_output_label(self, output_idx: int, label: str) -> None:
        cmd_res = self._sync_command(
            f'"{self._block_id}" set outputLabel {output_idx} "{label}"'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if output_idx in self.outputs:
            self.outputs[output_idx].label = label

    # ------------------------------------------------------------------
    # Crosspoint setters
    # ------------------------------------------------------------------

    def set_crosspoint_state(
        self, input_idx: int, output_idx: int, enabled: bool
    ) -> None:
        """Enable or disable routing from input_idx to output_idx."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointLevelState '
            f'{input_idx} {output_idx} {str(enabled).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.routing.setdefault(input_idx, {})[output_idx] = enabled

    def set_crosspoint_level(
        self, input_idx: int, output_idx: int, value_db: float
    ) -> None:
        """Set the gain (dB) at a specific crosspoint (-100.0 to 0.0)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointLevel '
            f'{input_idx} {output_idx} {value_db}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.crosspoint_levels.setdefault(input_idx, {})[output_idx] = value_db

    def set_crosspoint_delay(
        self, input_idx: int, output_idx: int, value_ms: float
    ) -> None:
        """Set delay at crosspoint (0.0-250.0 ms). Requires delayEnabled=True."""
        if not self.delay_enabled:
            raise RuntimeError(f"Delay is not enabled on {self._block_id}")
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointDelay '
            f'{input_idx} {output_idx} {value_ms}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)

    def set_crosspoint_delay_state(
        self, input_idx: int, output_idx: int, enabled: bool
    ) -> None:
        """Enable/disable delay at crosspoint. Requires delayEnabled=True."""
        if not self.delay_enabled:
            raise RuntimeError(f"Delay is not enabled on {self._block_id}")
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointDelayState '
            f'{input_idx} {output_idx} {str(enabled).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)

    # ------------------------------------------------------------------
    # Bulk routing helpers
    # ------------------------------------------------------------------

    def route(
        self, input_idx: int, output_idx: int, enabled: bool = True
    ) -> None:
        """Connect (or disconnect) a single input-to-output path."""
        self.set_crosspoint_state(input_idx, output_idx, enabled)

    def set_crosspoint_state_row(self, input_idx: int, enabled: bool) -> None:
        """Enable/disable all outputs from a given input (entire row)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointLevelStateRow '
            f'{input_idx} {str(enabled).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        if input_idx in self.routing:
            for j in self.routing[input_idx]:
                self.routing[input_idx][j] = enabled

    def set_crosspoint_state_column(self, output_idx: int, enabled: bool) -> None:
        """Enable/disable all inputs to a given output (entire column)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointLevelStateColumn '
            f'{output_idx} {str(enabled).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        for i in self.routing:
            self.routing[i][output_idx] = enabled

    def set_crosspoint_state_all(self, enabled: bool) -> None:
        """Enable/disable every crosspoint in the matrix."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set crosspointLevelStateAll {str(enabled).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        for i in self.routing:
            for j in self.routing[i]:
                self.routing[i][j] = enabled

    # ------------------------------------------------------------------
    # Routing inspection helpers
    # ------------------------------------------------------------------

    def get_inputs_for_output(self, output_idx: int) -> list[int]:
        """Return all input indexes currently routed to output_idx."""
        return [
            i for i, row in self.routing.items()
            if row.get(output_idx, False)
        ]

    def get_outputs_for_input(self, input_idx: int) -> list[int]:
        """Return all output indexes that input_idx is currently routed to."""
        return [
            j for j, enabled in self.routing.get(input_idx, {}).items()
            if enabled
        ]

    def get_routing_matrix(self) -> dict[int, dict[int, bool]]:
        """Return a copy of the full routing matrix."""
        return {i: dict(row) for i, row in self.routing.items()}

    def subscribe_crosspoint(self, input_idx: int, output_idx: int) -> None:
        """
        Explicitly subscribe to a single crosspoint's routing state.
        Useful for large matrices where auto-subscribe is disabled.
        """
        self._register_crosspoint_subscription(
            "crosspointLevelState", input_idx, output_idx
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _to_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_crosspoint_id(ch_id: str) -> tuple[int | None, int | None]:
    """Parse "ixj" crosspoint channel_id back to (input_idx, output_idx)."""
    try:
        parts = str(ch_id).split("x", 1)
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None, None
