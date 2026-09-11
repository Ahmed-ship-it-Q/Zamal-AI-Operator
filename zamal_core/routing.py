"""Deterministic administrative preferences, capability/policy/availability filters."""
from dataclasses import dataclass
import time

from .store import Denied
from .providers import CAPABILITY, STAGES

PREFERENCES = {
    'planner': ('openai', 'claude', 'gemini', 'codex'),
    'coder': ('codex', 'claude', 'openai', 'gemini'),
    'verifier': ('claude', 'openai', 'gemini', 'codex'),
}


@dataclass(frozen=True)
class RoutingPolicy:
    allowed_providers: tuple = ('openai', 'codex', 'claude', 'gemini')
    max_context_bytes: int = 200000
    max_cost_class: int = 1
    version: int = 1

    def __post_init__(self):
        if (type(self.allowed_providers) is not tuple or not self.allowed_providers
            or len(set(self.allowed_providers)) != len(self.allowed_providers)
            or set(self.allowed_providers) - set(PREFERENCES['planner'])
            or type(self.max_context_bytes) is not int or not 1 <= self.max_context_bytes <= 2000000
            or type(self.max_cost_class) is not int or self.max_cost_class < 0 or self.version != 1):
            raise Denied('Invalid routing policy')

    def data(self):
        return {'allowed_providers': list(self.allowed_providers), 'max_context_bytes': self.max_context_bytes,
                'max_cost_class': self.max_cost_class, 'version': self.version}

    @classmethod
    def from_data(cls, value):
        return cls(**{**value, 'allowed_providers': tuple(value['allowed_providers'])})


def manifest(registry, policy):
    return [{**vars(registry.adapters[name].spec), 'capabilities': list(registry.adapters[name].spec.capabilities)}
            for name in sorted(policy.allowed_providers) if name in registry.adapters]


class Router:
    def __init__(self, registry, policy=None):
        self.registry, self.policy = registry, policy or RoutingPolicy()

    def evaluate(self, snapshot, stage, excluded=()):
        if stage not in STAGES:
            raise Denied('Unknown routing stage')
        size, now = len(snapshot.json.encode()), time.time()
        choices, audit = [], []
        for rank, name in enumerate(PREFERENCES[stage]):
            adapter = self.registry.adapters.get(name)
            reason = 'eligible'
            if name not in self.policy.allowed_providers:
                reason = 'policy_disallowed'
            elif name in excluded:
                reason = 'already_unavailable_this_stage'
            elif adapter is None:
                reason = 'not_registered'
            elif CAPABILITY[stage] not in adapter.spec.capabilities:
                reason = 'capability_mismatch'
            elif adapter.spec.cost_class > self.policy.max_cost_class:
                reason = 'cost_policy'
            elif size > min(adapter.spec.max_context_bytes, self.policy.max_context_bytes):
                reason = 'context_budget'
            else:
                availability = adapter.availability()
                if availability.retry_after > now:
                    reason = 'rate_limited'
                elif not availability.available:
                    reason = 'unavailable:' + availability.reason
            audit.append({'provider': name, 'preference': rank, 'reason': reason})
            if reason == 'eligible':
                choices.append(name)
        return {'stage': stage, 'capability': CAPABILITY[stage], 'context_hash': snapshot.fingerprint,
                'context_bytes': size, 'policy': self.policy.data(), 'candidates': choices,
                'selected': choices[0] if choices else None, 'decisions': audit}
