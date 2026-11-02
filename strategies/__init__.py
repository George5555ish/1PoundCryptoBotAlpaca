"""Rule-based paper strategies. Each module exposes evaluate()."""

from __future__ import annotations

from importlib import import_module
from typing import Callable, Mapping

import config

EvaluateFn = Callable[[str, list[Mapping[str, object]], Mapping[str, object]], dict]


def load_evaluate(strategy_id: str) -> EvaluateFn:
    if strategy_id not in config.MULTI_STRATEGY_IDS:
        raise ValueError(f"unknown strategy_id {strategy_id}")
    module = import_module(f"strategies.{strategy_id}")
    evaluate = getattr(module, "evaluate", None)
    if not callable(evaluate):
        raise ValueError(f"{strategy_id} is missing evaluate()")
    return evaluate


def enabled_ids(selected: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not selected:
        return config.MULTI_STRATEGY_IDS
    unknown = [item for item in selected if item not in config.MULTI_STRATEGY_IDS]
    if unknown:
        raise ValueError(f"unknown strategy_id values: {', '.join(unknown)}")
    return selected
