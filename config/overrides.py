from typing import Dict, List


def apply_overrides(config: Dict, overrides: List[str]) -> Dict:
    i = 0
    while i < len(overrides):
        if not overrides[i].startswith("--"):
            i += 1
            continue

        key = overrides[i].lstrip("-")
        if i + 1 >= len(overrides):
            raise ValueError(f"Override {overrides[i]} has no value")
        raw_value = overrides[i + 1]

        value = _infer_type(raw_value)
        _set_nested(config, key.split("."), value)
        i += 2

    return config


def _infer_type(raw: str):
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _set_nested(d: Dict, keys: List[str], value):
    for key in keys[:-1]:
        if key not in d:
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value
