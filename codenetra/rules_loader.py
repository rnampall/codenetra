"""Load rule definitions from YAML or Markdown.

The built-in rule set lives in `rules_data/builtin_rules.yaml`. Users can
override or extend it by passing a file (or in-memory bytes) of the same
shape — either a `.yaml`/`.yml` file, or a `.md` file that contains a
fenced ```yaml block.

Custom rules are merged onto built-ins by `key`: a custom rule with an
existing key replaces the built-in; anything else is appended. To remove
a built-in rule entirely, set `enabled: false` on it in your override.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from codenetra.rules import CHECK_PRIMITIVES, Rule


BUILTIN_RULES_PATH = Path(__file__).parent / "rules_data" / "builtin_rules.yaml"


@dataclass(frozen=True)
class LoadedRules:
    rules: tuple[Rule, ...]
    config: dict
    pre_fetch_subdirs: tuple[str, ...] = ()
    extra_root_paths: tuple[str, ...] = ()
    source_label: str = "built-in"   # "built-in", "custom YAML", "custom MD", etc.


# ---------- public API ----------

def load_builtin_rules() -> LoadedRules:
    return _build_loaded_rules(_read_yaml(BUILTIN_RULES_PATH.read_text(encoding="utf-8")), source_label="built-in")


def load_rules_from_path(path: Path) -> LoadedRules:
    """Load + merge a custom rules file from disk."""
    text = path.read_text(encoding="utf-8")
    return load_rules_from_text(text, filename=path.name)


def load_rules_from_text(
    text: str,
    filename: Optional[str] = None,
) -> LoadedRules:
    """Load + merge a custom rules file given its raw text content.

    Detects format from filename suffix; falls back to YAML if no filename.
    """
    suffix = (Path(filename).suffix.lower() if filename else "").strip()
    if suffix == ".md":
        data = _read_yaml_block_from_markdown(text)
        source_label = f"custom MD ({filename or 'uploaded'})"
    else:
        data = _read_yaml(text)
        source_label = f"custom YAML ({filename or 'uploaded'})"

    if not isinstance(data, dict):
        raise ValueError("rules file must parse to a YAML mapping at the top level")

    builtin = _read_yaml(BUILTIN_RULES_PATH.read_text(encoding="utf-8"))
    merged = _merge_rules(builtin, data)
    return _build_loaded_rules(merged, source_label=source_label)


# ---------- internals ----------

def _read_yaml(text: str) -> dict:
    return yaml.safe_load(text) or {}


_FENCED_YAML_RE = re.compile(
    r"```(?:yaml|yml)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _read_yaml_block_from_markdown(text: str) -> dict:
    match = _FENCED_YAML_RE.search(text)
    if not match:
        raise ValueError(
            "rules.md must contain a fenced ```yaml block with rule definitions"
        )
    return _read_yaml(match.group(1))


def _merge_rules(builtin: dict, custom: dict) -> dict:
    """Merge custom rules onto built-ins.

    - config: shallow merge (custom keys override built-in keys)
    - rules: keyed by `key`; custom rule replaces same-keyed built-in,
             else appended in declaration order; custom `enabled: false`
             drops the rule.
    """
    out = {
        "version": custom.get("version", builtin.get("version", 1)),
        "config": {**(builtin.get("config") or {}), **(custom.get("config") or {})},
    }
    builtin_rules = builtin.get("rules") or []
    custom_rules = custom.get("rules") or []
    custom_by_key = {r["key"]: r for r in custom_rules if "key" in r}

    merged_rules: list[dict] = []
    seen: set[str] = set()

    for r in builtin_rules:
        key = r.get("key")
        if not key:
            continue
        if key in custom_by_key:
            override = custom_by_key[key]
            if override.get("enabled") is False:
                seen.add(key)
                continue
            merged_rules.append({**r, **override})
        else:
            merged_rules.append(r)
        seen.add(key)

    for r in custom_rules:
        key = r.get("key")
        if key and key not in seen:
            if r.get("enabled") is False:
                continue
            merged_rules.append(r)

    out["rules"] = merged_rules
    return out


def _build_loaded_rules(data: dict, source_label: str) -> LoadedRules:
    config = (data.get("config") or {}).copy()
    rules_data = data.get("rules") or []

    # Tier ordering — the declared list in `config.tiers` wins. Any tier we
    # encounter on a rule that wasn't declared in config gets appended in the
    # order it first appears, so custom rules that introduce new tiers still
    # render in a stable place.
    declared_tiers = list(config.get("tiers") or [])
    tier_index: dict[str, int] = {t: i for i, t in enumerate(declared_tiers)}

    rules: list[Rule] = []
    for sl_no, entry in enumerate(rules_data, start=1):
        if entry.get("enabled") is False:
            continue
        rule = _build_rule(entry)
        tier = entry.get("tier") or "Custom"
        if tier not in tier_index:
            tier_index[tier] = len(tier_index)
        # Re-create the Rule with tier/order/sl_no filled in (Rule is frozen,
        # so we replace via dataclasses.replace).
        from dataclasses import replace
        rule = replace(
            rule,
            tier=tier,
            tier_order=tier_index[tier],
            sl_no=sl_no,
        )
        rules.append(rule)

    # After all rules are built, re-number sl_no sequentially among the
    # *enabled* set so disabled rules don't leave gaps.
    from dataclasses import replace
    rules = [replace(r, sl_no=i) for i, r in enumerate(rules, start=1)]

    scanner_config = (config.get("scanner") or {}) if isinstance(config.get("scanner"), dict) else {}
    pre_fetch = tuple(scanner_config.get("pre_fetch_subdirs") or ())
    extra_root_paths = tuple(scanner_config.get("extra_root_paths") or ())

    return LoadedRules(
        rules=tuple(rules),
        config=config,
        pre_fetch_subdirs=pre_fetch,
        extra_root_paths=extra_root_paths,
        source_label=source_label,
    )


def _build_rule(entry: dict) -> Rule:
    """Construct a Rule from a YAML mapping using the appropriate primitive."""
    required_top = ("key", "title", "check", "fix_hint")
    missing = [k for k in required_top if k not in entry]
    if missing:
        raise ValueError(
            f"rule {entry.get('key', '<unknown>')!r} is missing required keys: {missing}"
        )

    primitive = entry["check"]
    if primitive not in CHECK_PRIMITIVES:
        valid = ", ".join(sorted(CHECK_PRIMITIVES))
        raise ValueError(
            f"rule {entry['key']!r} uses unknown check primitive {primitive!r}. "
            f"Valid: {valid}"
        )

    factory = CHECK_PRIMITIVES[primitive]
    # Pass everything except the top-level metadata fields as kwargs.
    metadata_keys = {"key", "title", "check", "fix_hint", "tags", "enabled", "tier"}
    params = {k: v for k, v in entry.items() if k not in metadata_keys}

    try:
        check_fn = factory(**params)
    except TypeError as e:
        raise ValueError(
            f"rule {entry['key']!r}: invalid params for primitive {primitive!r}: {e}"
        ) from e

    return Rule(
        key=entry["key"],
        title=entry["title"],
        fix_hint=entry["fix_hint"],
        tags=tuple(entry.get("tags") or ()),
        check=check_fn,
    )


# ---------- helpers for the CLI / web layers ----------

def detect_format(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".md":
        return "markdown"
    return "unknown"
