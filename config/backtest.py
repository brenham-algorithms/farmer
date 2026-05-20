from typing import List

import yaml
from pydantic import BaseModel

from api.models import BacktestConfig


class BacktestSettings(BaseModel):
    backtests: List[BacktestConfig]

    @classmethod
    def build(cls, args) -> "BacktestSettings":
        with open(args.config, "r") as f:
            raw = yaml.safe_load(f) or {}

        data = raw.get("backtests", [])

        return cls(backtests=data)

    @classmethod
    def set_args(cls, parser):
        parser.add_argument(
            "--config", type=str, default="config.yaml", help="Config file path"
        )

        parser.add_argument("--name", type=str, help="The name of the backtest to run")

        parser.add_argument(
            "--level", type=str, default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
        )
