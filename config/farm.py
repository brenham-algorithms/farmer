from typing import List

import yaml
from pydantic import BaseModel

from api.models import FarmerConfig
from config.overrides import apply_overrides


class FarmSettings(BaseModel):
    farmers: List[FarmerConfig]

    @classmethod
    def build(cls, args, overrides: list[str] | None = None) -> "FarmSettings":
        with open(args.config, "r") as f:
            raw = yaml.safe_load(f) or {}

        data = raw.get("farmers", [])

        if overrides:
            for farmer in data:
                if farmer.get("name") == args.name:
                    apply_overrides(farmer, overrides)

        return cls(farmers=data)

    @classmethod
    def set_args(cls, parser):
        parser.add_argument(
            "--config", type=str, default="config.yaml", help="Config file path"
        )

        parser.add_argument(
            "--name", type=str, help="The farmer name to run in production"
        )

        parser.add_argument(
            "--level",
            type=str,
            default="INFO",
            help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        )
