"""Turn-loop assembly and middleware."""
from enum import Enum

from harness.repair import HealingOutcome


class AssemblyProfile(str, Enum):
    TURN = "turn"


class NoAutomaticRepair:
    def checkpoint_state(self) -> dict:
        return {"schema_version": 1, "policy": "no_automatic_repair"}

    def attempt(self, request, failure, registry, environment) -> HealingOutcome:
        return HealingOutcome(False, False, None, ())
