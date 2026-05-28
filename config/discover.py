from typing import List

import yaml
from pydantic import BaseModel

from api.models import QueryConfig
from config.overrides import apply_overrides


class DiscoverSettings(BaseModel):
    queries: List[QueryConfig]

    @classmethod
    def build(cls, args, overrides: list[str] | None = None) -> "DiscoverSettings":
        with open(args.config, "r") as f:
            raw = yaml.safe_load(f) or {}

        data = raw.get("discover", {})

        if overrides:
            for query in data.get("queries", []):
                if query.get("name") == args.query:
                    apply_overrides(query, overrides)

        return cls(**data)

    @classmethod
    def set_args(cls, parser):
        parser.add_argument(
            "--config", type=str, default="config.yaml", help="Config file path"
        )

        parser.add_argument(
            "--query", type=str, help="The query name for which to run discovery"
        )

        parser.add_argument(
            "--level",
            type=str,
            default="INFO",
            help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        )
