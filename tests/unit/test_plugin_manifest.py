"""Manifest + integrity tests for the ``machina-agent-builder`` Claude Code plugin.

The plugin (Form C of the self-description spine) carries no runtime logic, so
its tests guard *integrity*, not behaviour:

* the manifest is valid and every component Claude Code would discover
  (``commands/``, ``skills/``, ``agents/``) is well-formed;
* every component entry file points at the live spine (``machina describe`` or
  ``machina://v1/capabilities``) and no plugin file copies a capability or
  connector list into prose that would drift from the code;
* every repository path the plugin text cites exists (``#anchors`` included);
* the commands state their safety rules (sandbox and confirmations on,
  ``${VAR}`` credentials, no secrets in chat, a preview before writing).

Each check is a function over a plugin root (or, for the phrase pins, over a
file's text), so the negative tests at the end run it against a planted
violation.
"""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from pathlib import Path

import yaml

from machina.connectors.capabilities import Capability
from machina.introspect import describe

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "plugin"
_MANIFEST = _PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
_BUILD_COMMAND = _PLUGIN_ROOT / "commands" / "build-machina-agent.md"
_EXTEND_COMMAND = _PLUGIN_ROOT / "commands" / "extend-machina.md"

# An entry file must point at one of the live spine surfaces.
_SPINE_REFERENCES = ("machina describe", "machina://v1/capabilities")

# A single mention is fine; more reads as a list that drifts from the code.
_MAX_CAPABILITY_VALUES = 1
# Connector types double as plain words ("SQL"), so allow an example or two.
_MAX_CONNECTOR_TYPES = 2

# Files the build command generates in the user's project: named in the text,
# but not repository paths.
_GENERATED_FILE_NAMES = frozenset(
    {"config.yaml", "agent.py", ".env", ".env.example", ".gitignore"}
)
_PATH_SUFFIXES = (".md", ".py", ".json", ".yaml", ".yml", ".txt", ".toml")
# Path-search bases, in order: the repo root, then the package root (the seam
# manifest prints package-relative paths such as ``mcp/tools.py``). A bare file
# name resolves against the repo root only: ``runtime.py`` is ambiguous.
_PATH_BASES = (Path(), Path("src", "machina"))

# Rules shared by both commands, pinned as phrases.
_SECRET_RULE = "Never ask for, accept or repeat a secret value"
_PREVIEW_RULE = "Preview before writing"
# The build command's R6 safe defaults.
_SAFE_DEFAULT_PHRASES = (
    "sandbox: true",
    "confirmations: true",
    "${VAR}",
    ".env.example",
    "--live",
    _SECRET_RULE,
    _PREVIEW_RULE,
)
_EXTEND_PHRASES = ("${VAR}", _SECRET_RULE, _PREVIEW_RULE)

_FENCED_BLOCK = re.compile(r"^[ \t]*```.*?^[ \t]*```[^\n]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PLACEHOLDER = re.compile(r"[{<*]")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_HEADING_ID = re.compile(r"\{[ \t]*#([\w-]+)[^}]*\}$")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _entry_files(plugin_root: Path) -> list[Path]:
    """Component entry files Claude Code discovers: commands, skills, agents."""
    return sorted(
        [
            *plugin_root.glob("commands/**/*.md"),
            *plugin_root.glob("skills/*/SKILL.md"),
            *plugin_root.glob("agents/**/*.md"),
        ]
    )


def _markdown_files(plugin_root: Path) -> list[Path]:
    """Every Markdown file in the plugin, entry files and references alike."""
    return sorted(plugin_root.rglob("*.md"))


def _rel(path: Path, plugin_root: Path) -> str:
    return path.relative_to(plugin_root).as_posix()


def _strip_fenced_blocks(text: str) -> str:
    return _FENCED_BLOCK.sub("", text)


# ---------------------------------------------------------------------------
# Checks: each returns a list of human-readable violations
# ---------------------------------------------------------------------------


def _layout_violations(plugin_root: Path) -> list[str]:
    """A skill is a folder holding a SKILL.md; anything else is never loaded."""
    skills = plugin_root / "skills"
    if not skills.is_dir():
        return []
    violations = []
    for child in sorted(skills.iterdir()):
        if child.is_file():
            violations.append(f"skills/{child.name}: a skill must be a folder with a SKILL.md")
        elif not (child / "SKILL.md").is_file():
            violations.append(f"skills/{child.name}/: missing SKILL.md")
    return violations


def _frontmatter_violations(plugin_root: Path) -> list[str]:
    """Entry files open with YAML frontmatter carrying the required keys."""
    violations = []
    for path in _entry_files(plugin_root):
        rel = _rel(path, plugin_root)
        required = ("description",) if rel.startswith("commands/") else ("name", "description")
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if not text.startswith("---") or len(parts) < 3:
            violations.append(f"{rel}: missing YAML frontmatter")
            continue
        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            violations.append(f"{rel}: frontmatter is not valid YAML")
            continue
        if not isinstance(frontmatter, dict):
            violations.append(f"{rel}: frontmatter is not a mapping")
            continue
        for key in required:
            value = frontmatter.get(key)
            if not (isinstance(value, str) and value.strip()):
                violations.append(f"{rel}: frontmatter lacks {key}")
    return violations


def _spine_reference_violations(plugin_root: Path) -> list[str]:
    """Every entry file reads the live spine, not a copied snapshot."""
    return [
        f"{_rel(path, plugin_root)}: references neither `machina describe` "
        "nor `machina://v1/capabilities`"
        for path in _entry_files(plugin_root)
        if not any(ref in path.read_text(encoding="utf-8") for ref in _SPINE_REFERENCES)
    ]


def _mentioned(words: list[str], text: str, *, prose_forms: bool = False) -> list[str]:
    """The distinct ``words`` that appear in ``text`` as whole tokens.

    With ``prose_forms``, matching ignores case and lets each ``_`` also match a
    space, a hyphen or nothing, so ``sap_pm`` matches "SAP PM" and
    ``generic_cmms`` matches "GenericCmms".
    """
    found = []
    for word in words:
        parts = [re.escape(part) for part in word.split("_")]
        body = r"[_\s-]?".join(parts) if prose_forms else re.escape(word)
        flags = re.IGNORECASE if prose_forms else 0
        if re.search(rf"(?<![\w-]){body}(?![\w-])", text, flags):
            found.append(word)
    return sorted(found)


def _capability_list_violations(plugin_root: Path) -> list[str]:
    """No plugin file enumerates capability wire-strings.

    A file that lists capabilities inline goes stale when the Capability enum
    changes — exactly the failure the spine exists to prevent.
    """
    values = [c.value for c in Capability]
    violations = []
    for path in _markdown_files(plugin_root):
        found = _mentioned(values, path.read_text(encoding="utf-8"))
        if len(found) > _MAX_CAPABILITY_VALUES:
            violations.append(
                f"{_rel(path, plugin_root)}: hardcodes capability values {found}; "
                "read them from `machina describe` / docs/capabilities.md instead"
            )
    return violations


@functools.cache
def _connector_types() -> tuple[str, ...]:
    return tuple(sorted({conn.type for conn in describe().connectors}))


def _connector_list_violations(plugin_root: Path) -> list[str]:
    """No plugin file enumerates the registered connector types."""
    types = list(_connector_types())
    violations = []
    for path in _markdown_files(plugin_root):
        found = _mentioned(types, path.read_text(encoding="utf-8"), prose_forms=True)
        if len(found) > _MAX_CONNECTOR_TYPES:
            violations.append(
                f"{_rel(path, plugin_root)}: lists connector types {found}; "
                "read them from `machina describe` instead"
            )
    return violations


def _looks_like_path(word: str) -> bool:
    if "://" in word or word.startswith(("/", "$", "-", "~")):
        return False  # URIs, slash commands, variables, flags
    if any(ch in word for ch in "()\"'=[]"):
        return False  # code expressions, not paths
    base = word.partition("#")[0]
    return "/" in base or base.endswith(_PATH_SUFFIXES)


def _cited_paths(text: str) -> list[str]:
    """Path-like words inside inline code spans (fenced blocks are examples)."""
    return [
        word
        for span in _INLINE_CODE.findall(_strip_fenced_blocks(text))
        for word in span.split()
        if _looks_like_path(word)
    ]


def _resolve(rel: str, repo_root: Path) -> Path | None:
    bases = _PATH_BASES if "/" in rel else _PATH_BASES[:1]
    for base in bases:
        candidate = repo_root / base / rel
        if candidate.exists():
            return candidate
    return None


def _slugify(heading: str) -> str:
    """The anchor MkDocs' ``toc`` extension gives a heading."""
    text = unicodedata.normalize("NFKD", heading.replace("`", ""))
    text = re.sub(r"[^\w\s-]", "", text.encode("ascii", "ignore").decode("ascii"))
    return re.sub(r"[-\s]+", "-", text.strip().lower())


def _heading_anchors(path: Path) -> set[str]:
    anchors = set()
    for heading in _HEADING.findall(_strip_fenced_blocks(path.read_text(encoding="utf-8"))):
        explicit = _HEADING_ID.search(heading)
        anchors.add(explicit.group(1) if explicit else _slugify(heading))
    return anchors


def _cited_path_violations(plugin_root: Path, repo_root: Path) -> list[str]:
    """Every repository path the plugin cites exists, ``#anchor`` included.

    Paths resolve against the repo root, then ``src/machina/`` (bare file names:
    the repo root only). A template such as ``connectors/{category}/{name}.py``
    is checked up to its literal directory prefix. Names of files the build
    command generates are skipped.
    """
    violations = []
    for path in _markdown_files(plugin_root):
        rel = _rel(path, plugin_root)
        for token in _cited_paths(path.read_text(encoding="utf-8")):
            target, _, anchor = token.partition("#")
            if target in _GENERATED_FILE_NAMES:
                continue
            placeholder = _PLACEHOLDER.search(target)
            if placeholder:
                prefix = target[: placeholder.start()].rpartition("/")[0]
                if prefix and _resolve(prefix, repo_root) is None:
                    violations.append(f"{rel}: `{token}`: directory `{prefix}/` does not exist")
                continue
            resolved = _resolve(target, repo_root)
            if resolved is None:
                violations.append(f"{rel}: cites `{token}`, which does not exist in the repo")
            elif anchor and anchor not in _heading_anchors(resolved):
                violations.append(f"{rel}: `{token}`: `{target}` has no heading #{anchor}")
    return violations


def _missing_phrases(text: str, phrases: tuple[str, ...]) -> list[str]:
    """The pinned ``phrases`` absent from ``text``."""
    return [phrase for phrase in phrases if phrase not in text]


# ---------------------------------------------------------------------------
# The shipped plugin
# ---------------------------------------------------------------------------


def test_manifest_is_valid() -> None:
    """plugin.json parses, carries the required fields, a semver and the licence."""
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "machina-agent-builder"
    for field in ("name", "version", "description"):
        assert isinstance(data[field], str) and data[field].strip(), field
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"]), data["version"]
    assert data.get("license") == "Apache-2.0"


def test_ships_at_least_one_command() -> None:
    assert any(p.parts[-2] == "commands" for p in _entry_files(_PLUGIN_ROOT))


def test_component_layout_is_discoverable() -> None:
    assert _layout_violations(_PLUGIN_ROOT) == []


def test_entry_files_have_frontmatter() -> None:
    assert _frontmatter_violations(_PLUGIN_ROOT) == []


def test_entry_files_read_the_live_spine() -> None:
    assert _spine_reference_violations(_PLUGIN_ROOT) == []


def test_no_plugin_file_hardcodes_a_capability_list() -> None:
    assert _capability_list_violations(_PLUGIN_ROOT) == []


def test_no_plugin_file_hardcodes_a_connector_list() -> None:
    assert _connector_list_violations(_PLUGIN_ROOT) == []


def test_every_cited_path_exists() -> None:
    assert _cited_path_violations(_PLUGIN_ROOT, _REPO_ROOT) == []


def test_build_command_states_the_safe_defaults() -> None:
    """The build command writes sandbox and confirmations on, credentials as ${VAR}."""
    text = _BUILD_COMMAND.read_text(encoding="utf-8")
    assert _missing_phrases(text, _SAFE_DEFAULT_PHRASES) == []


def test_extend_command_states_the_secret_and_preview_rules() -> None:
    text = _EXTEND_COMMAND.read_text(encoding="utf-8")
    assert _missing_phrases(text, _EXTEND_PHRASES) == []


# ---------------------------------------------------------------------------
# Negative tests: each check fires on a planted violation
# ---------------------------------------------------------------------------

_VALID_COMMAND = "---\ndescription: Demo.\n---\nRun `machina describe` first.\n"
_VALID_COMPONENT = "---\nname: demo\ndescription: Demo.\n---\nRead `machina describe` first.\n"


def _plant(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def test_discovery_covers_commands_skills_and_agents(tmp_path: Path) -> None:
    root = _plant(
        tmp_path,
        {
            "commands/a.md": _VALID_COMMAND,
            "commands/ns/b.md": _VALID_COMMAND,
            "skills/s/SKILL.md": _VALID_COMPONENT,
            "skills/s/references/r.md": "Reference notes.\n",
            "agents/g.md": _VALID_COMPONENT,
        },
    )
    assert [_rel(p, root) for p in _entry_files(root)] == [
        "agents/g.md",
        "commands/a.md",
        "commands/ns/b.md",
        "skills/s/SKILL.md",
    ]
    assert "skills/s/references/r.md" in [_rel(p, root) for p in _markdown_files(root)]
    assert _frontmatter_violations(root) == []
    assert _spine_reference_violations(root) == []


def test_layout_check_flags_a_misplaced_skill(tmp_path: Path) -> None:
    root = _plant(tmp_path, {"skills/loose.md": _VALID_COMPONENT})
    (root / "skills" / "empty").mkdir()
    assert _layout_violations(root) == [
        "skills/empty/: missing SKILL.md",
        "skills/loose.md: a skill must be a folder with a SKILL.md",
    ]


def test_frontmatter_check_flags_missing_or_incomplete_frontmatter(tmp_path: Path) -> None:
    root = _plant(
        tmp_path,
        {
            "agents/bare.md": "Read `machina describe` first.\n",
            "skills/s/SKILL.md": _VALID_COMMAND,  # a skill also needs a name
            "commands/broken.md": "---\ndescription: [unclosed\n---\n`machina describe`\n",
            "commands/list.md": "---\n- description\n---\n`machina describe`\n",
        },
    )
    assert _frontmatter_violations(root) == [
        "agents/bare.md: missing YAML frontmatter",
        "commands/broken.md: frontmatter is not valid YAML",
        "commands/list.md: frontmatter is not a mapping",
        "skills/s/SKILL.md: frontmatter lacks name",
    ]


def test_spine_check_flags_a_component_that_skips_the_spine(tmp_path: Path) -> None:
    root = _plant(
        tmp_path,
        {
            "skills/s/SKILL.md": "---\nname: s\ndescription: Demo.\n---\nGuess.\n",
            "agents/g.md": "---\nname: g\ndescription: Demo.\n---\nRead machina://v1/capabilities.\n",
        },
    )
    assert _spine_reference_violations(root) == [
        "skills/s/SKILL.md: references neither `machina describe` nor `machina://v1/capabilities`"
    ]


def test_capability_check_flags_an_inline_list(tmp_path: Path) -> None:
    listed = ", ".join(c.value for c in list(Capability)[:3])
    root = _plant(
        tmp_path,
        {"agents/g.md": _VALID_COMPONENT + listed, "skills/s/references/r.md": listed},
    )
    violations = _capability_list_violations(root)
    assert [v.split(":")[0] for v in violations] == ["agents/g.md", "skills/s/references/r.md"]


def test_connector_check_flags_an_inline_list(tmp_path: Path) -> None:
    listed = " / ".join(t.upper() for t in _connector_types()[: _MAX_CONNECTOR_TYPES + 1])
    # Prose spellings of underscored types count too.
    prose = "Pick a source: SAP PM, Generic CMMS or a document-store.\n"
    root = _plant(
        tmp_path,
        {
            "commands/a.md": _VALID_COMMAND + f"Pick one: {listed}.\n",
            "commands/b.md": _VALID_COMMAND + prose,
            "commands/ok.md": _VALID_COMMAND + "For example, a SQL database.\n",
        },
    )
    violations = _connector_list_violations(root)
    assert [v.split(":")[0] for v in violations] == ["commands/a.md", "commands/b.md"]
    assert "['document_store', 'generic_cmms', 'sap_pm']" in violations[1]


def test_path_check_flags_missing_paths_and_anchors(tmp_path: Path) -> None:
    text = (
        _VALID_COMMAND + "Missing: `docs/no-such-guide.md`, `docs/yaml-config.md#no-such-heading`,"
        " `src/machina/no_such_dir/{name}.py`, `python scripts/no_such_script.py`,"
        " `runtime.py` (bare names resolve at the repo root only).\n"
        "Present: `docs/yaml-config.md#environment-variables`, `src/machina/runtime.py`,"
        " `connectors/capabilities.py`, `docs/connectors/`, `config.yaml`,"
        " `machina://v1/capabilities`, `${VAR}`, `--live`.\n"
        "```text\n`docs/inside-a-fence.md`\n```\n"
        "- an item:\n\n  ```text\n  `docs/inside-an-indented-fence.md`\n  ```\n"
    )
    root = _plant(tmp_path, {"commands/a.md": text})
    assert _cited_path_violations(root, _REPO_ROOT) == [
        "commands/a.md: cites `docs/no-such-guide.md`, which does not exist in the repo",
        "commands/a.md: `docs/yaml-config.md#no-such-heading`: "
        "`docs/yaml-config.md` has no heading #no-such-heading",
        "commands/a.md: `src/machina/no_such_dir/{name}.py`: "
        "directory `src/machina/no_such_dir/` does not exist",
        "commands/a.md: cites `scripts/no_such_script.py`, which does not exist in the repo",
        "commands/a.md: cites `runtime.py`, which does not exist in the repo",
    ]


def test_phrase_checks_flag_commands_without_the_safety_rules() -> None:
    unsafe = "Write `sandbox: false` and paste the API key into config.yaml.\n"
    assert _missing_phrases(unsafe, _SAFE_DEFAULT_PHRASES) == list(_SAFE_DEFAULT_PHRASES)
    assert _missing_phrases(unsafe, _EXTEND_PHRASES) == list(_EXTEND_PHRASES)


def test_slugify_matches_mkdocs_anchors() -> None:
    assert _slugify("When to Use YAML vs Python") == "when-to-use-yaml-vs-python"
    assert _slugify("Path A — Configure `GenericCmmsConnector`") == (
        "path-a-configure-genericcmmsconnector"
    )
