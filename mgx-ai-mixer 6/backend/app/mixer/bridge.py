"""Mixer control abstraction.

IMPORTANT — MGX16 reality check (as of mid-2026):
The MGX16 does not publish an open remote-control protocol. Unlike Yamaha's
DM3/TF/CL/QL consoles (which speak Yamaha RCP over TCP), the MGX series is
controlled from its own touchscreen/faders; Yamaha has announced future
remote operation via Elgato Stream Deck, but there is no documented API for
third-party software to set faders/EQ directly.

So the shipping control path is ADVISORY: the AI produces concrete moves and
the operator applies them on the console. This module exists so that if/when
Yamaha exposes a protocol (or you swap in a DM3/TF, which you CAN control
programmatically), only this file changes.
"""
from __future__ import annotations

from ..models import MixMove


class MixerBridge:
    """Interface for applying moves to a console."""
    capable = False

    def apply(self, move: MixMove) -> dict:
        raise NotImplementedError


class AdvisoryBridge(MixerBridge):
    """No hardware control — moves are surfaced to the human operator."""
    capable = False

    def apply(self, move: MixMove) -> dict:
        return {
            "applied": False,
            "mode": "advisory",
            "instruction": _to_instruction(move),
        }


class SoftwareDSPBridge(MixerBridge):
    """Real control, available TODAY on the MGX16: corrections are executed
    in software on the USB return path (see app/dsp/). The desk keeps
    preamps, faders, and routing; tone/dynamics live in the computer."""
    capable = True

    def __init__(self, controller):
        self.controller = controller

    def apply(self, move: MixMove) -> dict:
        result = self.controller.apply_move(move)
        result["mode"] = "software-dsp"
        result["instruction"] = _to_instruction(move)
        return result


class YamahaRCPBridge(MixerBridge):
    """Stub for consoles that speak Yamaha's remote control protocol over TCP
    (DM3, TF, CL/QL — typically port 49280). Not applicable to the MGX16
    today; kept as the extension point."""
    capable = True

    def __init__(self, host: str, port: int = 49280):
        self.host, self.port = host, port

    def apply(self, move: MixMove) -> dict:
        raise NotImplementedError(
            "RCP control requires a console that exposes the protocol "
            "(DM3/TF/CL/QL). The MGX16 does not."
        )


def _to_instruction(m: MixMove) -> str:
    verbs = {
        "fader": f"Move {m.channel_name} fader {m.amount}",
        "eq_cut": f"On {m.channel_name}, cut {m.amount} at {m.param}",
        "eq_boost": f"On {m.channel_name}, boost {m.amount} at {m.param}",
        "hpf": f"Engage HPF at {m.param} on {m.channel_name}",
        "pan": f"Pan {m.channel_name} to {m.param}",
        "comp": f"Adjust comp on {m.channel_name}: {m.param} {m.amount}",
        "gain": f"Trim input gain on {m.channel_name} by {m.amount}",
    }
    return verbs.get(m.action, f"{m.action} {m.param} {m.amount} on {m.channel_name}")
