"""Command integration functionality for APM packages.

Integrates .prompt.md files as commands for any target that supports the
``commands`` primitive (e.g. ``.claude/commands/``, ``.opencode/commands/``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
import frontmatter

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile

logger = logging.getLogger(__name__)


def _extract_input_names(input_spec: Any) -> List[str]:
    """Extract argument names from an APM 'input' front-matter value.

    Handles both formats:
      - Simple list:  input: [name, category]
      - Object list:  input:
                        - feature_name: "desc"
                        - feature_description: "desc"

    Args:
        input_spec: The raw value of the 'input' front-matter key.

    Returns:
        List[str]: Ordered list of argument names, or empty list.
    """
    if input_spec is None:
        return []

    if isinstance(input_spec, list):
        names: List[str] = []
        for item in input_spec:
            if isinstance(item, str):
                if item.strip():
                    names.append(item)
            elif isinstance(item, dict):
                names.extend(k for k in item.keys() if k.strip())
        return names

    if isinstance(input_spec, str):
        return [input_spec] if input_spec.strip() else []

    if isinstance(input_spec, dict):
        return [k for k in input_spec.keys() if k.strip()]

    return []


# Re-export for backward compat (tests import CommandIntegrationResult)
CommandIntegrationResult = IntegrationResult


class CommandIntegrator(BaseIntegrator):
    """Handles integration of APM package prompts into .claude/commands/.
    
    Transforms .prompt.md files into Claude Code custom slash commands
    during package installation, following the same pattern as PromptIntegrator.
    """
    
    def find_prompt_files(self, package_path: Path) -> List[Path]:
        """Find all .prompt.md files in a package."""
        return self.find_files_by_glob(
            package_path, "*.prompt.md", subdirs=[".apm/prompts"]
        )
    
    def _transform_prompt_to_command(
        self, source: Path,
    ) -> Tuple[str, frontmatter.Post, List[str]]:
        """Transform a .prompt.md file into Claude command format.

        Args:
            source: Path to the .prompt.md file

        Returns:
            Tuple of (command_name, post, warnings).
        """
        warnings: List[str] = []

        post = frontmatter.load(source)

        # Extract command name from filename
        filename = source.name
        if filename.endswith('.prompt.md'):
            command_name = filename[:-len('.prompt.md')]
        else:
            command_name = source.stem

        # Build Claude command frontmatter (preserve existing, add Claude-specific)
        claude_metadata = {}

        # Map APM frontmatter to Claude frontmatter
        if 'description' in post.metadata:
            claude_metadata['description'] = post.metadata['description']

        if 'allowed-tools' in post.metadata:
            claude_metadata['allowed-tools'] = post.metadata['allowed-tools']
        elif 'allowedTools' in post.metadata:
            claude_metadata['allowed-tools'] = post.metadata['allowedTools']

        if 'model' in post.metadata:
            claude_metadata['model'] = post.metadata['model']

        if 'argument-hint' in post.metadata:
            claude_metadata['argument-hint'] = post.metadata['argument-hint']
        elif 'argumentHint' in post.metadata:
            claude_metadata['argument-hint'] = post.metadata['argumentHint']

        # Map APM 'input' to Claude 'arguments' and 'argument-hint'
        input_names = _extract_input_names(post.metadata.get('input'))
        if input_names:
            claude_metadata['arguments'] = input_names
            if 'argument-hint' not in claude_metadata:
                claude_metadata['argument-hint'] = " ".join(
                    f"<{name}>" for name in input_names
                )

        # Convert APM input references to Claude $name placeholders
        content = post.content
        if input_names:
            content = re.sub(
                r'\$\{\{?\s*input\s*:\s*([\w-]+)\s*\}?\}',
                r'$\1',
                content,
            )

        # Create new post with Claude metadata
        new_post = frontmatter.Post(content)
        new_post.metadata = claude_metadata

        return (command_name, new_post, warnings)
    
    def integrate_command(
        self,
        source: Path,
        target: Path,
        package_info: Any,
        original_path: Path,
        *,
        diagnostics: Any = None,
    ) -> int:
        """Integrate a prompt file as a Claude command (verbatim copy with format conversion).

        Args:
            source: Source .prompt.md file path
            target: Target command file path in .claude/commands/
            package_info: PackageInfo object with package metadata
            original_path: Original path to the prompt file
            diagnostics: Optional DiagnosticCollector for surfacing warnings.

        Returns:
            int: Number of links resolved
        """
        # Transform to command format
        command_name, post, warnings = self._transform_prompt_to_command(source)

        # Resolve context links in content
        post.content, links_resolved = self.resolve_links(post.content, source, target)

        # Defense-in-depth: scan compiled command before writing
        compiled = frontmatter.dumps(post)
        try:
            from apm_cli.security.gate import WARN_POLICY, SecurityGate
            verdict = SecurityGate.scan_text(compiled, str(target), policy=WARN_POLICY)
            if verdict.has_critical:
                warnings.append(
                    f"{target.name}: {verdict.critical_count} critical, "
                    f"{verdict.warning_count} warning(s) hidden character finding(s) "
                    f"-- run 'apm audit --file {target}' to inspect"
                )
            elif verdict.has_findings:
                warnings.append(
                    f"{target.name}: {verdict.warning_count} warning(s) hidden "
                    f"character finding(s) -- run 'apm audit --file {target}' to inspect"
                )
        except (ImportError, OSError, ValueError) as exc:
            warnings.append(
                f"{target.name}: security scan skipped due to scan error: {exc}"
            )

        # Surface any collected warnings
        pkg_name = getattr(
            getattr(package_info, "package", None), "name", "",
        )
        for warning in warnings:
            if diagnostics and hasattr(diagnostics, "security"):
                diagnostics.security(
                    message=warning,
                    package=pkg_name,
                    detail=warning,
                    severity="warning",
                )
            else:
                logger.warning(warning)

        # Ensure target directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write the command file
        with open(target, 'w', encoding='utf-8') as f:
            f.write(compiled)

        return links_resolved
    
    # ------------------------------------------------------------------
    # Target-driven API (data-driven dispatch)
    # ------------------------------------------------------------------

    def integrate_commands_for_target(
        self,
        target: "TargetProfile",
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate prompt files as commands for a single *target*.

        Reads deployment paths from *target*'s ``commands`` primitive
        mapping, applying the opt-in guard when ``auto_create`` is
        ``False``.
        """
        mapping = target.primitives.get("commands")
        if not mapping:
            return IntegrationResult(0, 0, 0, [], 0)

        effective_root = mapping.deploy_root or target.root_dir
        target_root = project_root / effective_root
        if not target.auto_create and not (project_root / target.root_dir).is_dir():
            return IntegrationResult(0, 0, 0, [], 0)

        prompt_files = self.find_prompt_files(package_info.install_path)
        if not prompt_files:
            return IntegrationResult(0, 0, 0, [], 0)

        self.init_link_resolver(package_info, project_root)

        commands_dir = target_root / mapping.subdir
        files_integrated = 0
        files_skipped = 0
        target_paths: List[Path] = []
        total_links_resolved = 0

        for prompt_file in prompt_files:
            filename = prompt_file.name
            if filename.endswith('.prompt.md'):
                base_name = filename[:-len('.prompt.md')]
            else:
                base_name = prompt_file.stem

            target_path = commands_dir / f"{base_name}{mapping.extension}"
            rel_path = portable_relpath(target_path, project_root)

            if self.check_collision(
                target_path, rel_path, managed_files, force,
                diagnostics=diagnostics,
            ):
                files_skipped += 1
                continue

            if mapping.format_id == "gemini_command":
                self._write_gemini_command(prompt_file, target_path)
                links_resolved = 0
            else:
                links_resolved = self.integrate_command(
                    prompt_file, target_path, package_info, prompt_file,
                    diagnostics=diagnostics,
                )
            files_integrated += 1
            total_links_resolved += links_resolved
            target_paths.append(target_path)

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=total_links_resolved,
        )

    def sync_for_target(
        self,
        target: "TargetProfile",
        apm_package,
        project_root: Path,
        managed_files: set = None,
    ) -> Dict:
        """Remove APM-managed command files for a single *target*."""
        mapping = target.primitives.get("commands")
        if not mapping:
            return {"files_removed": 0, "errors": 0}
        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        legacy_dir = project_root / effective_root / mapping.subdir
        return self.sync_remove_files(
            project_root,
            managed_files,
            prefix=prefix,
            legacy_glob_dir=legacy_dir,
            legacy_glob_pattern="*-apm.md",
            targets=[target],
        )

    # ------------------------------------------------------------------
    # Gemini CLI Commands (.toml format)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_gemini_command(source: Path, target: Path) -> None:
        """Transform a ``.prompt.md`` file to Gemini CLI ``.toml`` format.

        Parses YAML frontmatter for ``description``, uses the markdown
        body as the ``prompt`` field.  Replaces ``$ARGUMENTS`` with
        ``{{args}}`` (Gemini CLI's argument interpolation syntax).

        Ref: https://geminicli.com/docs/cli/gemini-md/
        """
        import toml as _toml

        post = frontmatter.load(source)

        description = post.metadata.get("description", "")
        prompt_text = post.content.strip()
        prompt_text = prompt_text.replace("$ARGUMENTS", "{{args}}")

        if re.search(r'(?<!\d)\$\d+', prompt_text):
            prompt_text = f"Arguments: {{{{args}}}}\n\n{prompt_text}"

        doc = {"prompt": prompt_text}
        if description:
            doc = {"description": description, "prompt": prompt_text}

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_toml.dumps(doc), encoding="utf-8")

    # ------------------------------------------------------------------
    # Legacy per-target API (DEPRECATED)
    #
    # These methods hardcode a specific target and bypass scope
    # resolution.  Use the target-driven API (*_for_target) with
    # profiles from resolve_targets() instead.
    #
    # Kept for backward compatibility with external consumers.
    # Do NOT add new per-target methods here.
    # ------------------------------------------------------------------

    # DEPRECATED: use integrate_commands_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def integrate_package_commands(self, package_info, project_root: Path,
                                    force: bool = False,
                                    managed_files: set = None,
                                    diagnostics=None) -> IntegrationResult:
        """Integrate prompt files as Claude commands (.claude/commands/).

        Legacy compat: ensures ``.claude/`` exists so the target-driven
        method does not skip.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS
        (project_root / ".claude").mkdir(parents=True, exist_ok=True)
        return self.integrate_commands_for_target(
            KNOWN_TARGETS["claude"], package_info, project_root,
            force=force, managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def sync_integration(self, apm_package, project_root: Path,
                          managed_files: set = None) -> Dict:
        """Remove APM-managed command files from .claude/commands/."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        return self.sync_for_target(
            KNOWN_TARGETS["claude"], apm_package, project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def remove_package_commands(self, package_name: str, project_root: Path,
                                managed_files: set = None) -> int:
        """Remove APM-managed command files."""
        stats = self.sync_integration(None, project_root,
                                       managed_files=managed_files)
        return stats["files_removed"]

    # DEPRECATED: use integrate_commands_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def integrate_package_commands_opencode(self, package_info, project_root: Path,
                                            force: bool = False,
                                            managed_files: set = None,
                                            diagnostics=None) -> IntegrationResult:
        """Integrate prompt files as OpenCode commands (.opencode/commands/)."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        return self.integrate_commands_for_target(
            KNOWN_TARGETS["opencode"], package_info, project_root,
            force=force, managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def sync_integration_opencode(self, apm_package, project_root: Path,
                                  managed_files: set = None) -> Dict:
        """Remove APM-managed command files from .opencode/commands/."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        return self.sync_for_target(
            KNOWN_TARGETS["opencode"], apm_package, project_root,
            managed_files=managed_files,
        )
