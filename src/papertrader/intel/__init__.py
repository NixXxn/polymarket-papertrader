"""World-intel style overlays for papertrader entry gates.

Inspired by https://github.com/marc-shade/world-intel-mcp free-API domains
(macro, BTC technicals, event risk). Uses the same public endpoints directly
so the trading process stays HTTP-simple (no MCP stdio on the hot path).
"""

from papertrader.intel.gates import GateDecision, evaluate_entry_gate
from papertrader.intel.service import EventRisk, IntelService, IntelSnapshot, get_intel_service

__all__ = [
    "EventRisk",
    "GateDecision",
    "IntelService",
    "IntelSnapshot",
    "evaluate_entry_gate",
    "get_intel_service",
]
