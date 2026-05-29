#!/usr/bin/env python3
"""
AECReference DSP block — AEC far-end reference routing signal path.

Confirmed via live probe on TesiraForteX (FW 5.6.1.2):
  ALL TTP attributes return "not supported by AECReferenceInterface::Attributes".
  No level, mute, invert, label, subscriptions, or any other attributes exist.

This block routes the far-end audio reference to the AEC processing chain.
It has no user-controllable surface — it exists purely as a signal path
connection in the Tesira design. This stub module ensures pytesira loads
it silently rather than logging "Unsupported DSP block type: AECReference".
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
import logging


class AECReference(Block):
    """
    AEC Reference audio routing block.
    No attributes, no subscriptions, no controllable state.
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
        self._init_helper = {}

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": {}}
