"""Convention-based module loader — discover, validate, and load modules.

Scans arcagent/modules/*/MODULE.yaml, validates against ModuleManifest
schema, checks config allowlist, and imports entry_point classes.
Replaces the hardcoded _register_modules() approach.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from arcagent.core.config import ArcAgentConfig
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import ModuleContext

_logger = logging.getLogger("arcagent.module_loader")

# Only allow importing modules from these package prefixes (ASI-04)
_ALLOWED_MODULE_PREFIXES = ("arcagent.modules.",)


class ModuleManifest(BaseModel):
    """Validated MODULE.yaml schema."""

    name: str
    entry_point: str
    version: str = "0.0.0"
    description: str = ""
    dependencies: list[str] = []
    events: dict[str, list[str]] = {}
    cli_entry: str = ""


class ModuleLoader:
    """Convention-based module discovery and loading."""

    def discover(
        self,
        modules_dir: Path,
        config: ArcAgentConfig,
    ) -> list[ModuleManifest]:
        """Scan modules/*/MODULE.yaml, validate, filter by config.

        Returns list of validated ModuleManifest for enabled modules.
        Raises ConfigError for missing required fields (name, entry_point).
        """
        if not modules_dir.exists():
            return []

        manifests: list[ModuleManifest] = []
        for subdir in sorted(modules_dir.iterdir()):
            if not subdir.is_dir():
                continue

            yaml_path = subdir / "MODULE.yaml"
            if not yaml_path.exists():
                continue

            manifest = self._parse_module_yaml(yaml_path, subdir.name, config)
            if manifest is not None:
                manifests.append(manifest)

        return manifests

    def _parse_module_yaml(
        self,
        yaml_path: Path,
        dir_name: str,
        config: ArcAgentConfig,
    ) -> ModuleManifest | None:
        """Parse and validate a single MODULE.yaml file.

        Returns the validated manifest, or None if the module is disabled.
        Raises ConfigError for parse failures or missing required fields.
        """
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigError(
                code="CONFIG_MODULE_YAML_PARSE",
                message=f"Failed to parse MODULE.yaml for module '{dir_name}': {exc}",
                details={"module": dir_name},
            ) from exc

        if not isinstance(raw, dict):
            raise ConfigError(
                code="CONFIG_MODULE_YAML_PARSE",
                message=f"MODULE.yaml is not a mapping for module '{dir_name}'",
                details={"module": dir_name},
            )

        module_name = raw.get("name")
        if not module_name:
            raise ConfigError(
                code="CONFIG_MODULE_MISSING_NAME",
                message=f"MODULE.yaml missing 'name' for module '{dir_name}'",
                details={"module": dir_name},
            )

        if not raw.get("entry_point"):
            raise ConfigError(
                code="CONFIG_MODULE_MISSING_ENTRY_POINT",
                message=f"MODULE.yaml missing 'entry_point' for module '{module_name}'",
                details={"module": module_name},
            )

        module_entry = config.modules.get(module_name)
        if module_entry is None or not module_entry.enabled:
            _logger.debug("Module '%s' not enabled in config, skipping", module_name)
            return None

        try:
            manifest = ModuleManifest(**raw)
        except Exception as exc:
            raise ConfigError(
                code="CONFIG_MODULE_VALIDATION",
                message=f"MODULE.yaml validation failed for '{module_name}': {exc}",
                details={"module": module_name},
            ) from exc

        if not manifest.version or manifest.version == "0.0.0":
            _logger.warning(
                "Module '%s' missing version, using default 0.0.0",
                module_name,
            )

        return manifest

    def load(
        self,
        manifest: ModuleManifest,
        ctx: ModuleContext,
    ) -> Any | None:
        """Import entry_point and instantiate module.

        Returns the module instance, or None if import fails.
        Validates entry_point against _ALLOWED_MODULE_PREFIXES before
        importing (ASI-04: agentic supply chain protection).
        """
        try:
            module_path, class_name = manifest.entry_point.rsplit(":", 1)
        except ValueError:
            _logger.error(
                "Invalid entry_point format for module '%s': missing ':'",
                manifest.name,
            )
            return None

        # Validate import path against allowlist (ASI-04)
        if not any(module_path.startswith(p) for p in _ALLOWED_MODULE_PREFIXES):
            _logger.error(
                "Module '%s' entry_point '%s' not in allowed prefixes: %s",
                manifest.name,
                module_path,
                _ALLOWED_MODULE_PREFIXES,
            )
            return None

        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
        except Exception:
            _logger.exception(
                "Failed to import module '%s' from '%s'",
                manifest.name,
                manifest.entry_point,
            )
            return None

        try:
            instance = self._instantiate(cls, manifest, ctx)
        except Exception:
            _logger.exception(
                "Failed to instantiate module '%s'",
                manifest.name,
            )
            return None

        return instance

    def _instantiate(self, cls: type, manifest: ModuleManifest, ctx: ModuleContext) -> Any:
        """Construct a module with convention-based dependency injection.

        Inspects the class constructor and provides matching arguments
        from the available context. Module-specific config is looked up
        via ``ModuleEntry.config`` dict. Modules validate their own config
        internally. No special-casing needed for individual modules.
        """
        sig = inspect.signature(cls)

        # Module-specific config from [modules.X.config] in TOML
        module_entry = ctx.config.modules.get(manifest.name)
        module_config = module_entry.config if module_entry else None

        # Resources available for injection
        available: dict[str, Any] = {
            "config": module_config,
            "eval_config": ctx.config.eval,
            "llm_config": ctx.llm_config,
            "team_config": ctx.config.team,
            "telemetry": ctx.telemetry,
            "workspace": ctx.workspace,
            "agent_name": ctx.config.agent.name,
        }

        kwargs: dict[str, Any] = {}
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            # Skip *args and **kwargs — they're catch-alls, not dependencies
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if name in available and available[name] is not None:
                kwargs[name] = available[name]
            elif param.default is inspect.Parameter.empty:
                _logger.warning(
                    "Module '%s' constructor requires '%s' but no provider found",
                    manifest.name,
                    name,
                )

        return cls(**kwargs)

    def load_all(
        self,
        modules_dir: Path,
        ctx: ModuleContext,
    ) -> list[Any]:
        """Discover + load all enabled modules."""
        manifests = self.discover(modules_dir, ctx.config)
        modules: list[Any] = []
        for manifest in manifests:
            module = self.load(manifest, ctx)
            if module is not None:
                modules.append(module)
                _logger.info(
                    "Loaded module '%s' from '%s'",
                    manifest.name,
                    manifest.entry_point,
                )
        return modules
