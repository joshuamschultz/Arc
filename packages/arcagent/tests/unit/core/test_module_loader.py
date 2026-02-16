"""Tests for convention-based module loader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    ModuleEntry,
)
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import ModuleBus, ModuleContext


@pytest.fixture()
def config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
    )


@pytest.fixture()
def mock_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


@pytest.fixture()
def bus(config: ArcAgentConfig, mock_telemetry: MagicMock) -> ModuleBus:
    return ModuleBus(config=config, telemetry=mock_telemetry)


@pytest.fixture()
def module_ctx(
    bus: ModuleBus,
    config: ArcAgentConfig,
    mock_telemetry: MagicMock,
    tmp_path: Path,
) -> ModuleContext:
    return ModuleContext(
        bus=bus,
        tool_registry=MagicMock(),
        config=config,
        telemetry=mock_telemetry,
        workspace=tmp_path,
        llm_config=config.llm,
    )


def _create_module_dir(
    modules_dir: Path,
    name: str,
    *,
    yaml_content: str | None = None,
) -> Path:
    """Helper to create a module directory with MODULE.yaml."""
    mod_dir = modules_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    if yaml_content is not None:
        (mod_dir / "MODULE.yaml").write_text(yaml_content)
    # Create a minimal Python module
    init_file = mod_dir / "__init__.py"
    init_file.write_text(
        f"class {name.title()}Module:\n"
        f"    @property\n"
        f"    def name(self) -> str:\n"
        f'        return "{name}"\n'
        f'    async def startup(self, ctx: "Any") -> None:\n'
        f"        pass\n"
        f"    async def shutdown(self) -> None:\n"
        f"        pass\n"
    )
    return mod_dir


class TestModuleManifest:
    """T2.1: ModuleManifest Pydantic model."""

    def test_valid_manifest(self) -> None:
        from arcagent.core.module_loader import ModuleManifest

        manifest = ModuleManifest(
            name="memory",
            entry_point="arcagent.modules.memory:MarkdownMemoryModule",
            version="0.1.0",
            description="Test module",
        )
        assert manifest.name == "memory"
        assert manifest.entry_point == "arcagent.modules.memory:MarkdownMemoryModule"

    def test_missing_name_raises(self) -> None:
        from pydantic import ValidationError

        from arcagent.core.module_loader import ModuleManifest

        with pytest.raises(ValidationError):
            ModuleManifest(
                entry_point="arcagent.modules.memory:MarkdownMemoryModule",
            )  # type: ignore[call-arg]

    def test_missing_entry_point_raises(self) -> None:
        from pydantic import ValidationError

        from arcagent.core.module_loader import ModuleManifest

        with pytest.raises(ValidationError):
            ModuleManifest(name="test")  # type: ignore[call-arg]

    def test_defaults_for_optional_fields(self) -> None:
        from arcagent.core.module_loader import ModuleManifest

        manifest = ModuleManifest(
            name="test",
            entry_point="mod:Class",
        )
        assert manifest.version == "0.0.0"
        assert manifest.description == ""
        assert manifest.dependencies == []


class TestModuleLoaderDiscover:
    """T2.2: ModuleLoader.discover()."""

    def test_discover_finds_module_yaml(self, tmp_path: Path, config: ArcAgentConfig) -> None:
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        _create_module_dir(
            modules_dir,
            "memory",
            yaml_content=(
                "name: memory\nentry_point: arcagent.modules.memory:MarkdownMemoryModule\n"
            ),
        )

        # Enable memory in config
        config_with_module = config.model_copy(
            update={"modules": {"memory": ModuleEntry(enabled=True)}}
        )

        loader = ModuleLoader()
        manifests = loader.discover(modules_dir, config_with_module)
        assert len(manifests) == 1
        assert manifests[0].name == "memory"

    def test_discover_skips_dirs_without_yaml(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        (modules_dir / "no_yaml_module").mkdir(parents=True)

        loader = ModuleLoader()
        manifests = loader.discover(modules_dir, config)
        assert len(manifests) == 0

    def test_discover_filters_disabled_modules(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        _create_module_dir(
            modules_dir,
            "memory",
            yaml_content="name: memory\nentry_point: mod:Class\n",
        )

        config_disabled = config.model_copy(
            update={"modules": {"memory": ModuleEntry(enabled=False)}}
        )

        loader = ModuleLoader()
        manifests = loader.discover(modules_dir, config_disabled)
        assert len(manifests) == 0

    def test_discover_raises_on_missing_entry_point(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        _create_module_dir(
            modules_dir,
            "bad",
            yaml_content="name: bad\n",  # missing entry_point
        )

        config_with_module = config.model_copy(
            update={"modules": {"bad": ModuleEntry(enabled=True)}}
        )

        loader = ModuleLoader()
        with pytest.raises(ConfigError):
            loader.discover(modules_dir, config_with_module)

    def test_discover_skips_disabled_even_if_not_in_config(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        """Modules not in config at all are skipped (not enabled by default)."""
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        _create_module_dir(
            modules_dir,
            "orphan",
            yaml_content="name: orphan\nentry_point: mod:Class\n",
        )

        loader = ModuleLoader()
        manifests = loader.discover(modules_dir, config)
        assert len(manifests) == 0


class TestModuleLoaderLoad:
    """T2.3: ModuleLoader.load() and load_all()."""

    def test_load_imports_and_instantiates(
        self, tmp_path: Path, module_ctx: ModuleContext, config: ArcAgentConfig
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        manifest = ModuleManifest(
            name="memory",
            entry_point="arcagent.modules.memory:MarkdownMemoryModule",
        )

        loader = ModuleLoader()
        module = loader.load(manifest, module_ctx)
        assert module is not None
        assert module.name == "memory"

    def test_load_handles_import_error(self, tmp_path: Path, module_ctx: ModuleContext) -> None:
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        manifest = ModuleManifest(
            name="nonexistent",
            entry_point="arcagent.modules.nonexistent:FakeModule",
        )

        loader = ModuleLoader()
        module = loader.load(manifest, module_ctx)
        assert module is None  # Graceful skip

    def test_load_rejects_disallowed_prefix(
        self, tmp_path: Path, module_ctx: ModuleContext
    ) -> None:
        """Modules with entry_points outside allowed prefixes are rejected (ASI-04)."""
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        manifest = ModuleManifest(
            name="evil",
            entry_point="os.path:join",
        )

        loader = ModuleLoader()
        module = loader.load(manifest, module_ctx)
        assert module is None  # Rejected by prefix check

    def test_load_all_returns_loaded_modules(
        self, tmp_path: Path, module_ctx: ModuleContext, config: ArcAgentConfig
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        _create_module_dir(
            modules_dir,
            "memory",
            yaml_content=(
                "name: memory\nentry_point: arcagent.modules.memory:MarkdownMemoryModule\n"
            ),
        )

        config_with_module = config.model_copy(
            update={"modules": {"memory": ModuleEntry(enabled=True)}}
        )
        ctx = ModuleContext(
            bus=module_ctx.bus,
            tool_registry=module_ctx.tool_registry,
            config=config_with_module,
            telemetry=module_ctx.telemetry,
            workspace=tmp_path,
            llm_config=config_with_module.llm,
        )

        loader = ModuleLoader()
        modules = loader.load_all(modules_dir, ctx)
        assert len(modules) == 1
        assert modules[0].name == "memory"


class TestModuleLoaderEdgeCases:
    """Edge cases and error paths for ModuleLoader."""

    def test_discover_nonexistent_directory_returns_empty(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        """Line 54: discover() returns [] when modules_dir doesn't exist."""
        from arcagent.core.module_loader import ModuleLoader

        nonexistent_dir = tmp_path / "no_such_dir"
        assert not nonexistent_dir.exists()

        loader = ModuleLoader()
        manifests = loader.discover(nonexistent_dir, config)
        assert manifests == []

    def test_discover_yaml_parse_error_raises(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        """Lines 69-70: ConfigError when YAML parsing fails."""
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        mod_dir = modules_dir / "broken"
        mod_dir.mkdir(parents=True)
        (mod_dir / "MODULE.yaml").write_text("{ invalid yaml [ content")

        config_with_module = config.model_copy(
            update={"modules": {"broken": ModuleEntry(enabled=True)}}
        )

        loader = ModuleLoader()
        with pytest.raises(ConfigError, match="Failed to parse MODULE.yaml"):
            loader.discover(modules_dir, config_with_module)

    def test_discover_yaml_not_dict_raises(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        """Line 77: ConfigError when parsed YAML is not a dict."""
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        mod_dir = modules_dir / "list_yaml"
        mod_dir.mkdir(parents=True)
        (mod_dir / "MODULE.yaml").write_text("- item1\n- item2\n")

        config_with_module = config.model_copy(
            update={"modules": {"list_yaml": ModuleEntry(enabled=True)}}
        )

        loader = ModuleLoader()
        with pytest.raises(ConfigError, match="not a mapping"):
            loader.discover(modules_dir, config_with_module)

    def test_discover_missing_name_raises(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        """Line 86: ConfigError when name is missing from YAML."""
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        mod_dir = modules_dir / "no_name"
        mod_dir.mkdir(parents=True)
        (mod_dir / "MODULE.yaml").write_text("entry_point: mod:Class\n")

        config_with_module = config.model_copy(
            update={"modules": {"no_name": ModuleEntry(enabled=True)}}
        )

        loader = ModuleLoader()
        with pytest.raises(ConfigError, match="missing 'name'"):
            loader.discover(modules_dir, config_with_module)

    def test_discover_pydantic_validation_error_raises(
        self, tmp_path: Path, config: ArcAgentConfig
    ) -> None:
        """Lines 109-110: ConfigError when Pydantic validation fails."""
        from arcagent.core.module_loader import ModuleLoader

        modules_dir = tmp_path / "modules"
        mod_dir = modules_dir / "invalid"
        mod_dir.mkdir(parents=True)
        # dependencies should be a list, not a string
        (mod_dir / "MODULE.yaml").write_text(
            "name: invalid\nentry_point: mod:Class\ndependencies: not_a_list\n"
        )

        config_with_module = config.model_copy(
            update={"modules": {"invalid": ModuleEntry(enabled=True)}}
        )

        loader = ModuleLoader()
        with pytest.raises(ConfigError, match="validation failed"):
            loader.discover(modules_dir, config_with_module)

    def test_load_invalid_entry_point_no_colon_returns_none(
        self, tmp_path: Path, module_ctx: ModuleContext
    ) -> None:
        """Lines 139-144: Invalid entry_point format (no colon) returns None."""
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        manifest = ModuleManifest(
            name="no_colon",
            entry_point="arcagent.modules.memory.MarkdownMemoryModule",  # No colon!
        )

        loader = ModuleLoader()
        module = loader.load(manifest, module_ctx)
        assert module is None

    def test_instantiate_failed_returns_none(
        self, tmp_path: Path, module_ctx: ModuleContext
    ) -> None:
        """Lines 170-175: Failed module instantiation returns None."""
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        # Create a module class that raises on instantiation
        mod_dir = tmp_path / "arcagent" / "modules" / "failing"
        mod_dir.mkdir(parents=True, exist_ok=True)
        (mod_dir / "__init__.py").write_text(
            """
class FailingModule:
    def __init__(self, **kwargs):
        raise RuntimeError("Instantiation failed")
"""
        )

        manifest = ModuleManifest(
            name="failing",
            entry_point="arcagent.modules.failing:FailingModule",
        )

        loader = ModuleLoader()
        # Import will succeed, but instantiation will fail
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            module = loader.load(manifest, module_ctx)
            assert module is None
        finally:
            sys.path.pop(0)

    def test_instantiate_skips_self_param(
        self, tmp_path: Path, module_ctx: ModuleContext
    ) -> None:
        """Line 200: _instantiate continues when param is 'self'."""
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        # This is implicitly tested by all successful loads, but make it explicit
        manifest = ModuleManifest(
            name="memory",
            entry_point="arcagent.modules.memory:MarkdownMemoryModule",
        )

        loader = ModuleLoader()
        module = loader.load(manifest, module_ctx)
        assert module is not None
        # 'self' param was skipped during inspection

    def test_instantiate_warns_on_required_param_no_provider(
        self, module_ctx: ModuleContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines 204: Warning when required param has no provider."""
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        # Create a dummy class with required param that won't be in available providers
        class TestModule:
            def __init__(self, config: Any, unknown_required_param: str) -> None:
                pass

        manifest = ModuleManifest(name="test", entry_point="test:Module")
        loader = ModuleLoader()

        # Call _instantiate directly with our test class
        with caplog.at_level("WARNING"):
            try:
                loader._instantiate(TestModule, manifest, module_ctx)
            except TypeError:
                pass  # Expected since unknown_required_param is missing

        # Check warning was logged
        assert any("unknown_required_param" in rec.message for rec in caplog.records)


class TestInstantiateException:
    """Lines 170-175: Exception during _instantiate returns None."""

    def test_instantiate_exception_returns_none(
        self, module_ctx: ModuleContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        class BadModule:
            def __init__(self, **kwargs: Any) -> None:
                raise RuntimeError("constructor exploded")

        manifest = ModuleManifest(
            name="bad", entry_point="arcagent.modules.memory:MarkdownMemoryModule"
        )
        loader = ModuleLoader()

        # Patch _resolve_class to return our BadModule instead
        with patch.object(loader, "_instantiate", side_effect=RuntimeError("boom")):
            module = loader.load(manifest, module_ctx)
        assert module is None


class TestLoadAllSkipsNone:
    """Line 222: load_all skips modules that return None."""

    def test_load_all_skips_failed_modules(
        self, module_ctx: ModuleContext
    ) -> None:
        from arcagent.core.module_loader import ModuleLoader, ModuleManifest

        loader = ModuleLoader()

        # Patch load to return None for first, object for second
        results = [None, object()]
        with patch.object(loader, "discover", return_value=[
            ModuleManifest(name="bad", entry_point="x:Y"),
            ModuleManifest(name="good", entry_point="x:Y"),
        ]):
            with patch.object(loader, "load", side_effect=results):
                modules = loader.load_all(Path("/fake"), module_ctx)
        assert len(modules) == 1
