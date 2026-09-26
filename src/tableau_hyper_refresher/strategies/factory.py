"""Strategy factory scaffold."""

from ..refresh_config import RefreshMode
from .base import RefreshStrategy
from .full_replace import FullReplaceStrategy
from .merge import MergeStrategy
from .upsert import UpsertStrategy


class RefreshStrategyFactory:
    """Resolve strategy implementation from table mode."""

    @staticmethod
    def for_mode(mode: RefreshMode) -> RefreshStrategy:
        if mode == "full_replace":
            return FullReplaceStrategy()
        if mode == "upsert":
            return UpsertStrategy()
        if mode == "merge":
            return MergeStrategy()
        raise ValueError(f"Unsupported refresh mode: {mode}")
