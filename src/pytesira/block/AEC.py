#!/usr/bin/env python3
"""
AEC (Acoustic Echo Cancellation) processing block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels               → int (8)
  get aecEnable {ch}            → bool
  get nlpMode {ch}              → NLPMODE_NONE | NLPMODE_LOW | NLPMODE_MEDIUM | NLPMODE_HIGH
  get nrdMode {ch}              → OFF | LOW | MED | HIGH
  get speechMode {ch}           → bool
  get hpfBypass {ch}            → bool
  get hpfCutoff {ch}            → float (Hz, 20-500)
  get limiterEnable {ch}        → bool
  get agcBypass {ch}            → bool
  get maxGain {ch}              → float (dB, 0-12)
  get maxGainAdjRate {ch}       → float (dB/s, 0-5)
  get maxAttenuation {ch}       → float (dB, 0-12)
  get minSnr {ch}               → float (dB, 10-50)
  get minThreshold {ch}         → float (dBu, -30..+10)
  get targetLevel {ch}          → float (dB, -10..+10)
  get level {ch}                → float (dB, minLevel..maxLevel)
  get minLevel {ch}             → float (-100.0)
  get maxLevel {ch}             → float (12.0)
  get mute {ch}                 → bool
  get invert {ch}               → bool
  get meters {ch}               → dict: aecEchoReturnLoss, aecEchoReturnLossEnhancement,
                                    aecAdaptiveFilterOutputLevel, aecNonLinearProcessingOutputLevel,
                                    aecNoiseReductionOutputLevel, aecInputLevel, aecReferenceLevel,
                                    agcGain, agcInputLevel, agcNoiseFloor, agcSignalNoiseRatio,
                                    agcLimiterActive, agcActive
  label                         → NOT supported (auto-generated)

Subscriptions:
  mutes                         — all-channel mute state (no channel arg)
  levels                        — all-channel level (no channel arg)
  meters {ch}                   — per-channel real-time AEC telemetry
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute import BaseLevelMute
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging


class AEC(BaseLevelMute):
    """
    Acoustic Echo Cancellation processing block.

    Extends BaseLevelMute for standard per-channel mute/level/subscription.
    Adds AEC-specific per-channel configuration attributes and a real-time
    meters subscription that delivers echo/noise/AGC telemetry.
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

        # Logger first (required by BaseLevelMute before super().__init__)
        self._logger = logging.getLogger(f"{__name__}.{block_id}")

        # BaseLevelMute handles: numChannels, minLevel, maxLevel, Channel objects,
        # mute/level subscriptions, and _init_helper["channels"].
        # label is not supported on AECInterface — BaseLevelMute handles CMD_ERROR
        # by using empty string, so no _chan_label_key override is needed.
        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # AEC-specific per-channel attributes (dicts keyed by channel index)
        self.aec_enable: dict[int, bool] = {}
        self.nlp_mode: dict[int, str] = {}
        self.nrd_mode: dict[int, str] = {}
        self.speech_mode: dict[int, bool] = {}
        self.hpf_bypass: dict[int, bool] = {}
        self.hpf_cutoff: dict[int, float] = {}
        self.limiter_enable: dict[int, bool] = {}
        self.agc_bypass: dict[int, bool] = {}
        self.max_gain: dict[int, float] = {}
        self.max_gain_adj_rate: dict[int, float] = {}
        self.max_attenuation: dict[int, float] = {}
        self.min_snr: dict[int, float] = {}
        self.min_threshold: dict[int, float] = {}
        self.target_level: dict[int, float] = {}
        self.invert: dict[int, bool] = {}

        # Real-time AEC telemetry per channel (updated by meters subscription)
        self.meters: dict[int, dict] = {ch: {} for ch in self.channels}

        # Load AEC-specific attributes from init helper or live query
        aec_init = None
        if isinstance(init_helper, dict):
            aec_init = init_helper.get("aec")

        if aec_init:
            self._load_aec_helper(aec_init)
        else:
            self._query_aec_attrs()

        # Extend the block-map helper with AEC-specific data
        self._init_helper["aec"] = {
            ch: {
                "aec_enable": self.aec_enable.get(ch),
                "nlp_mode": self.nlp_mode.get(ch),
                "nrd_mode": self.nrd_mode.get(ch),
                "speech_mode": self.speech_mode.get(ch),
                "hpf_bypass": self.hpf_bypass.get(ch),
                "hpf_cutoff": self.hpf_cutoff.get(ch),
                "limiter_enable": self.limiter_enable.get(ch),
                "agc_bypass": self.agc_bypass.get(ch),
                "max_gain": self.max_gain.get(ch),
                "max_gain_adj_rate": self.max_gain_adj_rate.get(ch),
                "max_attenuation": self.max_attenuation.get(ch),
                "min_snr": self.min_snr.get(ch),
                "min_threshold": self.min_threshold.get(ch),
                "target_level": self.target_level.get(ch),
                "invert": self.invert.get(ch),
            }
            for ch in self.channels
        }

    # ------------------------------------------------------------------
    # Subscription lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called by DSP after __init__ to set up meters subscriptions per channel.

        Uses a 2-second rate to limit the volume of AEC telemetry push data.
        The meters subscription is set up once on initial connect only — it is
        intentionally NOT renewed in _register_base_subscriptions() to avoid
        flooding the serial TTP command queue during the periodic refresh cycle.
        """
        for ch in self.channels:
            try:
                self._register_subscription(
                    subscribe_type="meters", channel=ch, rate_ms=2000
                )
            except Exception as exc:
                self._logger.debug(f"meters subscribe ch{ch} timed out (normal): {exc}")

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """Re-subscribe on reconnect: standard mute/level only.

        AEC meters subscriptions are deliberately excluded here. Re-subscribing
        8 channels × N AEC blocks every 30 seconds serialises hundreds of TTP
        commands and saturates the SSH channel, causing timeout cascades on
        other blocks. Meters data will resume naturally when the DSP reconnects
        and subscribe() is called again by the DSP init sequence.
        """
        return super()._register_base_subscriptions()

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        if response.subscription_type == "meters":
            try:
                ch = int(response.subscription_channel_id)
                if ch in self.meters:
                    val = response.value
                    self.meters[ch] = val if isinstance(val, dict) else {}
            except (ValueError, TypeError):
                pass
        # BaseLevelMute handles mutes and levels
        super().subscription_callback(response)

    # ------------------------------------------------------------------
    # AEC-specific attribute setters (send TTP set command)
    # ------------------------------------------------------------------

    def set_aec_enable(self, channel: int, value: bool) -> None:
        self._sync_command(
            f'"{self._block_id}" set aecEnable {channel} {str(value).lower()}'
        )
        self.aec_enable[channel] = value

    def set_nlp_mode(self, channel: int, value: str) -> None:
        self._sync_command(f'"{self._block_id}" set nlpMode {channel} {value}')
        self.nlp_mode[channel] = value

    def set_nrd_mode(self, channel: int, value: str) -> None:
        self._sync_command(f'"{self._block_id}" set nrdMode {channel} {value}')
        self.nrd_mode[channel] = value

    def set_speech_mode(self, channel: int, value: bool) -> None:
        self._sync_command(
            f'"{self._block_id}" set speechMode {channel} {str(value).lower()}'
        )
        self.speech_mode[channel] = value

    def set_hpf_bypass(self, channel: int, value: bool) -> None:
        self._sync_command(
            f'"{self._block_id}" set hpfBypass {channel} {str(value).lower()}'
        )
        self.hpf_bypass[channel] = value

    def set_hpf_cutoff(self, channel: int, value: float) -> None:
        self._sync_command(f'"{self._block_id}" set hpfCutoff {channel} {value}')
        self.hpf_cutoff[channel] = value

    def set_invert(self, channel: int, value: bool) -> None:
        self._sync_command(
            f'"{self._block_id}" set invert {channel} {str(value).lower()}'
        )
        self.invert[channel] = value

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_aec_attrs(self) -> None:
        for ch in self.channels:
            def _get(attr, ch=ch):
                try:
                    return self._sync_command(
                        f'"{self._block_id}" get {attr} {ch}'
                    ).value
                except Exception:
                    return None

            self.aec_enable[ch] = bool(_get("aecEnable"))
            self.nlp_mode[ch] = str(_get("nlpMode") or "NLPMODE_MEDIUM")
            self.nrd_mode[ch] = str(_get("nrdMode") or "MED")
            self.speech_mode[ch] = bool(_get("speechMode"))
            self.hpf_bypass[ch] = bool(_get("hpfBypass"))
            self.hpf_cutoff[ch] = float(_get("hpfCutoff") or 90.0)
            self.limiter_enable[ch] = bool(_get("limiterEnable"))
            self.agc_bypass[ch] = bool(_get("agcBypass"))
            self.max_gain[ch] = float(_get("maxGain") or 8.0)
            self.max_gain_adj_rate[ch] = float(_get("maxGainAdjRate") or 3.0)
            self.max_attenuation[ch] = float(_get("maxAttenuation") or 10.0)
            self.min_snr[ch] = float(_get("minSnr") or 20.0)
            self.min_threshold[ch] = float(_get("minThreshold") or -15.0)
            self.target_level[ch] = float(_get("targetLevel") or 8.0)
            self.invert[ch] = bool(_get("invert"))

    def _load_aec_helper(self, aec_init: dict) -> None:
        for ch_key, data in aec_init.items():
            ch = int(ch_key)
            if ch not in self.channels:
                continue
            self.aec_enable[ch] = data.get("aec_enable", True)
            self.nlp_mode[ch] = data.get("nlp_mode", "NLPMODE_MEDIUM")
            self.nrd_mode[ch] = data.get("nrd_mode", "MED")
            self.speech_mode[ch] = data.get("speech_mode", True)
            self.hpf_bypass[ch] = data.get("hpf_bypass", False)
            self.hpf_cutoff[ch] = data.get("hpf_cutoff", 90.0)
            self.limiter_enable[ch] = data.get("limiter_enable", False)
            self.agc_bypass[ch] = data.get("agc_bypass", False)
            self.max_gain[ch] = data.get("max_gain", 8.0)
            self.max_gain_adj_rate[ch] = data.get("max_gain_adj_rate", 3.0)
            self.max_attenuation[ch] = data.get("max_attenuation", 10.0)
            self.min_snr[ch] = data.get("min_snr", 20.0)
            self.min_threshold[ch] = data.get("min_threshold", -15.0)
            self.target_level[ch] = data.get("target_level", 8.0)
            self.invert[ch] = data.get("invert", False)
