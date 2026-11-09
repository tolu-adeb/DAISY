"""YAML config loading with dotted command-line overrides (--set a.b.c=value)."""
from __future__ import annotations

import copy

import yaml


def load_config(path: str, overrides: list[str] | None = None) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    for ov in overrides or []:
        if "=" not in ov:
            raise ValueError(f"override must look like key.path=value, got {ov!r}")
        key, raw = ov.split("=", 1)
        set_path(cfg, key.strip(), yaml.safe_load(raw))
    return cfg


def set_path(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        if p not in node or not isinstance(node[p], dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


def get_path(cfg: dict, dotted: str, default=None):
    node = cfg
    for p in dotted.split("."):
        if not isinstance(node, dict) or p not in node:
            return default
        node = node[p]
    return node


def with_overrides(cfg: dict, params: dict) -> dict:
    out = copy.deepcopy(cfg)
    for k, v in params.items():
        set_path(out, k, v)
    return out
