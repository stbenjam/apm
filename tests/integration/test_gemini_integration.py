"""Offline integration tests for Gemini CLI target.

Verifies that ``apm install`` correctly deploys skills, commands,
instructions, and MCP config to ``.gemini/`` without requiring
network access or API tokens.
"""

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
import toml

from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.integration import (
    KNOWN_TARGETS,
    InstructionIntegrator,
    PromptIntegrator,
    SkillIntegrator,
)
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)


def _make_package_info(
    package_dir: Path,
    name: str = "test-pkg",
    package_type: PackageType = None,
) -> PackageInfo:
    """Build a minimal ``PackageInfo`` for offline tests."""
    package = APMPackage(name=name, version="1.0.0", package_path=package_dir)
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        package_type=package_type,
    )


@pytest.mark.integration
class TestGeminiCommandIntegration:
    """Commands: .prompt.md -> .gemini/commands/*.toml"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / ".gemini").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_prompt(self, name: str, description: str, body: str) -> Path:
        pkg = self.root / "apm_modules" / "test-pkg"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        prompt = pkg / f"{name}.prompt.md"
        prompt.write_text(f"---\ndescription: {description}\n---\n{body}\n")
        return pkg

    def test_deploys_toml_with_prompt_and_description(self):
        pkg = self._create_prompt("greet", "Say hello", "Hello $ARGUMENTS")
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        result = CommandIntegrator().integrate_commands_for_target(
            target, info, self.root
        )

        assert result.files_integrated == 1
        toml_path = self.root / ".gemini" / "commands" / "greet.toml"
        assert toml_path.exists()

        doc = toml.loads(toml_path.read_text())
        assert "prompt" in doc
        assert "description" in doc
        assert doc["description"] == "Say hello"
        assert "{{args}}" in doc["prompt"]
        assert "$ARGUMENTS" not in doc["prompt"]

    def test_positional_args_get_args_prefix(self):
        pkg = self._create_prompt("run", "Run stuff", "Do $1 then $2")
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        CommandIntegrator().integrate_commands_for_target(target, info, self.root)

        doc = toml.loads(
            (self.root / ".gemini" / "commands" / "run.toml").read_text()
        )
        assert doc["prompt"].startswith("Arguments: {{args}}")

    def test_no_description_omits_key(self):
        pkg = self.root / "apm_modules" / "test-pkg"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        prompt = pkg / "bare.prompt.md"
        prompt.write_text("Just a prompt body\n")
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        CommandIntegrator().integrate_commands_for_target(target, info, self.root)

        doc = toml.loads(
            (self.root / ".gemini" / "commands" / "bare.toml").read_text()
        )
        assert "prompt" in doc
        assert "description" not in doc


@pytest.mark.integration
class TestGeminiInstructionIntegration:
    """Instructions: .instructions.md -> .gemini/rules/*.md (frontmatter stripped)"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / ".gemini").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_instruction(self, name: str, apply_to: str, body: str) -> Path:
        pkg = self.root / "apm_modules" / "test-pkg"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True, exist_ok=True)
        inst = inst_dir / f"{name}.instructions.md"
        inst.write_text(
            f"---\napplyTo: '{apply_to}'\ndescription: test rule\n---\n{body}\n"
        )
        return pkg

    def test_deploys_md_with_frontmatter_stripped(self):
        body = "Always use snake_case for variables."
        pkg = self._create_instruction("naming", "**/*.py", body)
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        result = InstructionIntegrator().integrate_instructions_for_target(
            target, info, self.root
        )

        assert result.files_integrated == 1
        rule_path = self.root / ".gemini" / "rules" / "naming.md"
        assert rule_path.exists()

        content = rule_path.read_text()
        assert "---" not in content
        assert "applyTo" not in content
        assert body in content

    def test_body_content_preserved(self):
        body = "## Heading\n\n- bullet one\n- bullet two\n\nParagraph."
        pkg = self._create_instruction("style", "**/*.ts", body)
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        InstructionIntegrator().integrate_instructions_for_target(
            target, info, self.root
        )

        content = (self.root / ".gemini" / "rules" / "style.md").read_text()
        assert "## Heading" in content
        assert "- bullet one" in content
        assert "Paragraph." in content


@pytest.mark.integration
class TestGeminiSkillIntegration:
    """Skills: package dir -> .gemini/skills/{name}/SKILL.md (verbatim copy)"""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / ".gemini").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deploys_skill_verbatim(self):
        skill_content = "# My Skill\n\nDo something useful."
        pkg = self.root / "apm_modules" / "my-skill"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text(
            "name: my-skill\nversion: 1.0.0\ntype: skill\n"
        )
        (pkg / "SKILL.md").write_text(skill_content)

        info = _make_package_info(pkg, name="my-skill", package_type=PackageType.HYBRID)
        target = KNOWN_TARGETS["gemini"]

        result = SkillIntegrator().integrate_package_skill(
            info, self.root, targets=[target]
        )

        assert result.skill_created
        skill_md = self.root / ".gemini" / "skills" / "my-skill" / "SKILL.md"
        assert skill_md.exists()
        assert skill_md.read_text() == skill_content


@pytest.mark.integration
class TestGeminiMCPIntegration:
    """MCP: update_config merges into .gemini/settings.json preserving other keys."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.gemini_dir = self.root / ".gemini"
        self.gemini_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_adds_server_preserving_existing_keys(self, monkeypatch):
        monkeypatch.chdir(self.root)

        settings = self.gemini_dir / "settings.json"
        settings.write_text(json.dumps({
            "mcpServers": {},
            "theme": "dark",
            "tools": {"enabled": True},
        }))

        adapter = GeminiClientAdapter.__new__(GeminiClientAdapter)
        adapter.update_config({
            "my-server": {
                "command": "npx",
                "args": ["-y", "@mcp/test-server"],
                "env": {"KEY": "val"},
            }
        })

        result = json.loads(settings.read_text())
        assert "my-server" in result["mcpServers"]
        assert result["mcpServers"]["my-server"]["command"] == "npx"
        assert result["theme"] == "dark"
        assert result["tools"] == {"enabled": True}

    def test_creates_mcp_servers_key_if_missing(self, monkeypatch):
        monkeypatch.chdir(self.root)

        settings = self.gemini_dir / "settings.json"
        settings.write_text(json.dumps({"theme": "light"}))

        adapter = GeminiClientAdapter.__new__(GeminiClientAdapter)
        adapter.update_config({"srv": {"command": "echo"}})

        result = json.loads(settings.read_text())
        assert "mcpServers" in result
        assert "srv" in result["mcpServers"]
        assert result["theme"] == "light"


@pytest.mark.integration
class TestGeminiOptInBehavior:
    """Gemini target is opt-in: nothing deployed when .gemini/ doesn't exist."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_package_with_content(self) -> Path:
        pkg = self.root / "apm_modules" / "test-pkg"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        (pkg / "hello.prompt.md").write_text("---\ndescription: hi\n---\nHello\n")
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True, exist_ok=True)
        (inst_dir / "rule.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\nBe nice.\n"
        )
        return pkg

    def test_commands_not_deployed_without_gemini_dir(self):
        pkg = self._create_package_with_content()
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        result = CommandIntegrator().integrate_commands_for_target(
            target, info, self.root
        )

        assert result.files_integrated == 0
        assert not (self.root / ".gemini").exists()

    def test_instructions_not_deployed_without_gemini_dir(self):
        pkg = self._create_package_with_content()
        info = _make_package_info(pkg)
        target = KNOWN_TARGETS["gemini"]

        result = InstructionIntegrator().integrate_instructions_for_target(
            target, info, self.root
        )

        assert result.files_integrated == 0
        assert not (self.root / ".gemini").exists()

    def test_mcp_update_noop_without_gemini_dir(self, monkeypatch):
        monkeypatch.chdir(self.root)

        adapter = GeminiClientAdapter.__new__(GeminiClientAdapter)
        adapter.update_config({"srv": {"command": "echo"}})

        assert not (self.root / ".gemini").exists()


@pytest.mark.integration
class TestGeminiMultiTargetCoexistence:
    """Both .github/ and .gemini/ present: files deploy to each target."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / ".github").mkdir()
        (self.root / ".gemini").mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_full_package(self) -> Path:
        pkg = self.root / "apm_modules" / "test-pkg"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        (pkg / "review.prompt.md").write_text(
            "---\ndescription: Code review\n---\nReview $ARGUMENTS\n"
        )
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True, exist_ok=True)
        (inst_dir / "style.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\ndescription: Style guide\n---\nUse black.\n"
        )
        return pkg

    def test_prompts_deployed_to_both_targets(self):
        pkg = self._create_full_package()
        info = _make_package_info(pkg)
        copilot = KNOWN_TARGETS["copilot"]
        gemini = KNOWN_TARGETS["gemini"]

        r_copilot = PromptIntegrator().integrate_prompts_for_target(
            copilot, info, self.root
        )
        r_gemini = CommandIntegrator().integrate_commands_for_target(
            gemini, info, self.root
        )

        assert r_copilot.files_integrated == 1
        assert r_gemini.files_integrated == 1

        assert (self.root / ".github" / "prompts" / "review.prompt.md").exists()
        assert (self.root / ".gemini" / "commands" / "review.toml").exists()

    def test_instructions_transformed_differently_per_target(self):
        pkg = self._create_full_package()
        info = _make_package_info(pkg)
        copilot = KNOWN_TARGETS["copilot"]
        gemini = KNOWN_TARGETS["gemini"]

        inst = InstructionIntegrator()
        inst.integrate_instructions_for_target(copilot, info, self.root)
        inst.integrate_instructions_for_target(gemini, info, self.root)

        copilot_path = (
            self.root / ".github" / "instructions" / "style.instructions.md"
        )
        gemini_path = self.root / ".gemini" / "rules" / "style.md"
        assert copilot_path.exists()
        assert gemini_path.exists()

        copilot_content = copilot_path.read_text()
        gemini_content = gemini_path.read_text()

        assert "applyTo" in copilot_content
        assert "applyTo" not in gemini_content
        assert "Use black." in copilot_content
        assert "Use black." in gemini_content
