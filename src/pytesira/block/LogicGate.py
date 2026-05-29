#!/usr/bin/env python3
"""
LogicGate DSP block.

Confirmed via exhaustive live probe on TesiraForteX (FW 5.6.1.2):
  get numInputs   → int  (varies by gate: 1 for NOT/BUFFER, 2+ for AND/OR/etc.)
  get numOutputs  → int  (always 1)
  All other attributes → 'not supported by LogicGateInterface::Attributes'

The LogicGate is a design-time construct in Tesira Designer that combines
logic signals using a fixed gate type (AND, OR, NOR, NAND, XOR, NOT, BUFFER).
The gate type and input/output states are NOT accessible via TTP — this block
has no runtime-controllable or readable state beyond its topology.

This module loads the block silently and stores the gate topology (num_inputs,
num_outputs) so callers can infer the probable gate type:
  1 input  → NOT or BUFFER
  2 inputs → AND, OR, NAND, NOR, XOR
  3+ inputs → multi-input AND/OR/etc.
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
import logging


class LogicGate(Block):
    """
    Logic gate signal processing block (design-time only).

    No runtime attributes are accessible via TTP. Stores gate topology
    (num_inputs, num_outputs) from initial query. No subscriptions or setters.
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

        # Gate topology — only readable attributes
        self.num_inputs: int = 1
        self.num_outputs: int = 1

        # Load from cache or query
        try:
            if init_helper is not None:
                self.num_inputs = int(init_helper.get("num_inputs", 1))
                self.num_outputs = int(init_helper.get("num_outputs", 1))
            else:
                raise ValueError("no helper")
        except Exception:
            try:
                self.num_inputs = int(
                    self._sync_command(f'"{self._block_id}" get numInputs').value
                )
            except Exception:
                pass
            try:
                self.num_outputs = int(
                    self._sync_command(f'"{self._block_id}" get numOutputs').value
                )
            except Exception:
                pass

        self._init_helper = {
            "num_inputs": self.num_inputs,
            "num_outputs": self.num_outputs,
        }

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}
