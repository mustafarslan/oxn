"""What a project's own manifests say about where its code lives.

Every language ships a file that answers "which of these names are ours": `tsconfig.json`
`paths`, `package.json` `workspaces`, `go.mod` `module`, `Cargo.toml` `[workspace] members`.
Java ships none and answers with its directory layout instead, which is the same question.

Separated from `resolve.py` when that file crossed its 500-line ceiling on the Go work. The
seam is real rather than convenient: nothing here parses an import or touches a specifier --
these read files off disk once per run and hand back tables, and `resolve.py` decides what an
import means with them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: A quoted TOML scalar. Both quote styles, because `Cargo.toml` in the wild uses both.
_TOML_STRING = re.compile(r"""["']([^"']*)["']""")


def normalize(path: str) -> str:
    """Collapse ``.`` and ``..`` without touching the filesystem.

    Here rather than in `resolve.py` because both modules need it and `resolve` already
    imports this one -- putting it the other way round would make them import each other.
    """
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _read(path: Path) -> str:
    """A manifest's text, and ``""`` when it is absent or unreadable. A half-written file is
    a thing that happens mid-edit, and a gate that crashes on one is off exactly when
    somebody is working."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _toml_field(text: str, table: str, key: str) -> list[str]:
    """The string values of ``key`` inside ``[table]``, from a deliberately small reader.

    Not `tomllib`: that is 3.11 and OXN supports 3.10, which CI runs -- `mypy` caught the
    import before the 3.10 job did. Not a dependency either (ADR-0001).

    Section-aware, because that is the entire difficulty. `crates/matcher/Cargo.toml`
    declares `name` twice -- under `[package]` and under a test target two lines later -- so
    a scan that ignores headers takes the wrong one. `[[test]]` reduces to `test` and cannot
    collide with `package`. A `#` inside a quoted value would be mistaken for a comment;
    neither field this reads is a string that contains one.
    """
    values: list[str] = []
    current = ""
    collecting = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if collecting:
            collecting = "]" not in stripped
            values.extend(_TOML_STRING.findall(stripped))
        elif stripped.startswith("["):
            current = stripped.strip("[]").strip()
        elif current == table and stripped.partition("=")[0].strip() == key:
            raw = stripped.partition("=")[2]
            collecting = raw.lstrip().startswith("[") and "]" not in raw
            values.extend(_TOML_STRING.findall(raw))
    return values


def tsconfig_aliases(root: Path) -> dict[str, tuple[str, ...]]:
    """``paths`` aliases from ``tsconfig.json``, joined to ``baseUrl``.

    Parsed leniently: a tsconfig may contain comments and trailing commas, and a malformed
    one must degrade to "no aliases" rather than fail the run.
    """
    config = root / "tsconfig.json"
    if not config.exists():
        return {}
    try:
        raw = _strip_jsonc(config.read_text(encoding="utf-8", errors="replace"))
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}

    options = data.get("compilerOptions", {}) or {}
    base = str(options.get("baseUrl", "") or "").strip("./")
    aliases: dict[str, tuple[str, ...]] = {}
    for pattern, targets in (options.get("paths", {}) or {}).items():
        prefix = pattern.rstrip("*").rstrip("/")
        resolved = tuple(
            normalize("/".join(part for part in (base, str(target).rstrip("*")) if part))
            for target in targets
        )
        aliases[prefix] = resolved
    return aliases


def workspace_packages(root: Path) -> dict[str, tuple[str, ...]]:
    """Every package this repository declares as its own, name -> directory.

    Read from the two declarations that actually exist in the wild: npm/yarn's
    `package.json#workspaces` (a list, or `{"packages": [...]}` in yarn's older form) and
    `pnpm-workspace.yaml#packages`. `lerna.json` is read only when neither is present,
    because its `packages` field almost always duplicates one of them.

    The *name* comes from each matched directory's own `package.json`, never from the
    directory name: `packages/common` calls itself `@nestjs/common`, and it is the name that
    appears in an import.

    Out of scope, deliberately, and stated here rather than discovered later: pnpm's
    `catalog:` and yarn's `workspace:` protocol specifiers, and `exports` maps. The first
    two name versions rather than paths; the third points at built output (`./dist/*.js`)
    that is not in the source tree OXN measures, so honouring it would resolve imports to
    files this repository does not contain.
    """
    globs = _workspace_globs(root)
    if not globs:
        return {}
    packages: dict[str, tuple[str, ...]] = {}
    for pattern in globs:
        for manifest in sorted(root.glob(f"{pattern.rstrip('/')}/package.json")):
            name = _package_name(manifest)
            directory = manifest.parent.relative_to(root).as_posix()
            if name and name not in packages:
                packages[name] = (directory,)
    return packages


def _workspace_globs(root: Path) -> list[str]:
    """The declared workspace patterns, from whichever file declares them.

    One reader per format rather than one function with three branch chains: npm/yarn, pnpm
    and lerna are three unrelated file formats that happen to answer the same question, and
    the version of this that inlined all three scored 23 against a ceiling of 12 -- caught
    by OXN's own hook while being written.

    Order is precedence. `lerna.json`'s `packages` almost always duplicates one of the
    others, so it is consulted only when neither is present.
    """
    for reader in (_npm_globs, _pnpm_globs, _lerna_globs):
        found = reader(root)
        if found:
            return found
    return []


def _npm_globs(root: Path) -> list[str]:
    """`package.json#workspaces`: a list, or yarn's older `{"packages": [...]}`."""
    declared = _json_file(root / "package.json").get("workspaces")
    if isinstance(declared, dict):
        declared = declared.get("packages")
    return _string_list(declared)


def _pnpm_globs(root: Path) -> list[str]:
    """`pnpm-workspace.yaml#packages`."""
    path = root / "pnpm-workspace.yaml"
    if not path.is_file():
        return []
    import yaml  # lazily: only a pnpm repo pays the ~15 ms

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    except yaml.YAMLError:
        return []
    return _string_list(loaded.get("packages") if isinstance(loaded, dict) else None)


def _lerna_globs(root: Path) -> list[str]:
    """`lerna.json#packages`, the legacy spelling."""
    return _string_list(_json_file(root / "lerna.json").get("packages"))


def _string_list(value: object) -> list[str]:
    """A list of strings, or nothing. Never a partial list of whatever happened to be one."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _package_name(manifest: Path) -> str:
    name = _json_file(manifest).get("name")
    return name if isinstance(name, str) else ""


def _json_file(path: Path) -> dict[str, object]:
    """A JSON object, or an empty one. A malformed manifest must never fail the run."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(_strip_jsonc(path.read_text(encoding="utf-8", errors="replace")))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _strip_jsonc(text: str) -> str:
    """Remove comments and trailing commas from JSON-with-comments.

    A ``tsconfig.json`` is JSONC, not JSON: it legally contains ``//`` and ``/* */``
    comments and trailing commas. Stripping those needs a real scan rather than a
    line-level heuristic, because a comment marker can appear *inside a string*
    (``"paths": {"@x/*": ["http://example.com/*"]}``) and a comment can follow a value on
    the same line (``"baseUrl": "./src", // where sources live``). Getting either wrong
    silently discards every path alias in the file.
    """
    return _Jsonc(text).strip()


@dataclass
class _Jsonc:
    """A two-state scanner: inside a string literal, or outside it.

    Which state it is in decides the meaning of every character that follows -- `//` is a
    comment outside a string and four ordinary bytes of a URL inside one -- so the states
    are two methods rather than a flag consulted at each of six branches.
    """

    text: str
    out: list[str] = field(default_factory=list)
    in_string: bool = False

    def strip(self) -> str:
        index = 0
        while index < len(self.text):
            index = self._inside(index) if self.in_string else self._outside(index)
        return "".join(self.out)

    def _inside(self, index: int) -> int:
        """Copy verbatim until the closing quote. Nothing in here is syntax."""
        char = self.text[index]
        self.out.append(char)
        if char == "\\" and index + 1 < len(self.text):
            self.out.append(self.text[index + 1])  # an escape consumes the next character
            return index + 2
        if char == '"':
            self.in_string = False
        return index + 1

    def _outside(self, index: int) -> int:
        char = self.text[index]
        if char == '"':
            self.in_string = True
            self.out.append(char)
            return index + 1
        if self.text.startswith("//", index):
            newline = self.text.find("\n", index)
            return len(self.text) if newline == -1 else newline
        if self.text.startswith("/*", index):
            close = self.text.find("*/", index + 2)
            return len(self.text) if close == -1 else close + 2
        if char == "," and self._is_trailing(index):
            return index + 1
        self.out.append(char)
        return index + 1

    def _is_trailing(self, index: int) -> bool:
        """A trailing comma is legal in JSONC and fatal to `json.loads`."""
        return self.text[index + 1 :].lstrip()[:1] in {"}", "]"}


def go_module(root: Path) -> str | None:
    """This tree's own module path, from the first `module` line of `go.mod`.

    A nested `go.mod` in a subdirectory declares a *separate* module whose imports are
    genuinely external to this one, and is deliberately not read: go-kit has none, and
    guessing at multi-module layouts unmeasured is the mistake this ADR's monorepo amendment
    was written about.
    """
    manifest = root / "go.mod"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped[len("module ") :].strip().strip('"')
    return None


def java_packages(known: set[str]) -> dict[str, tuple[str, ...]]:
    """Java package -> **every file in it**, derived from the source layout.

    Maven and Gradle both put `org.example.Thing` at `<source root>/org/example/Thing.java`,
    so the package is the directory path with `/` swapped for `.`. Reading the `package`
    declaration out of every file would be exact and costs a parse of the whole tree on a
    path budgeted in milliseconds; where the two disagree the file is in the wrong place and
    will not compile.

    **Files rather than directories**, which is what makes resolution a lookup instead of a
    scan: `import pkg.*` is the value as it stands, and `import pkg.Type` is one filter over
    it. Keyed on the package alone and holding files from *both* source roots, because
    `src/main/java` and `src/test/java` hold the same packages -- 5 of petclinic's 6 are in
    both -- and a mapping that kept one directory would have put half of every resolved
    import in the wrong root.
    """
    packages: dict[str, set[str]] = {}
    for path in known:
        if not path.endswith(".java"):
            continue
        segments = str(PurePosixPath(path).parent).split("/")
        for index, segment in enumerate(segments):
            if segment == "java" and index + 1 < len(segments):
                packages.setdefault(".".join(segments[index + 1 :]), set()).add(path)
                break
    return {package: tuple(sorted(found)) for package, found in packages.items()}


# ---- Rust -------------------------------------------------------------------------------


def cargo_crates(root: Path) -> dict[str, str]:
    """Crate name **as an import spells it** -> its directory, across a Cargo workspace.

    A crate declared as `grep-searcher` is imported as `grep_searcher`: Rust identifiers hold
    no hyphens, so cargo substitutes. Keying on the declared name finds nothing.

    `[workspace] members` may glob, and the workspace root is frequently a package itself --
    ripgrep is both. Read with `tomllib` rather than a line scan because
    `crates/matcher/Cargo.toml` declares `name` twice, once under `[package]` and once under
    a test target, and the wrong one is two lines further down.

    **Visibility only.** Knowing `grep_searcher` is ours is not knowing which file
    `grep_searcher::sinks::UTF8` lives in: that needs `mod` declarations walked across
    `x.rs`, `x/mod.rs` and `#[path]`, plus re-exports, and it has no pinned measurement yet.
    """
    manifests = _cargo_manifests(root)

    crates: dict[str, str] = {}
    for manifest in manifests:
        for name in _toml_field(_read(manifest), "package", "name"):
            crates[name.replace("-", "_")] = _relative_dir(manifest.parent, root)
    return crates


def _cargo_manifests(root: Path) -> list[Path]:
    """The workspace root's manifest and every member's, in that order."""
    manifests = [root / "Cargo.toml"]
    for member in _toml_field(_read(manifests[0]), "workspace", "members"):
        manifests.extend(sorted(root.glob(f"{member}/Cargo.toml")))
    return manifests


#: Where a crate's root module lives when the manifest does not say. Rust's own convention,
#: and the fallback order `cargo` itself uses.
_CARGO_DEFAULT_ROOTS = ("src/lib.rs", "src/main.rs")


def cargo_roots(root: Path) -> dict[str, str]:
    """Crate directory -> the file holding its root module, which `crate::` is relative to.

    **Not always `src/lib.rs`.** ripgrep's own package declares
    `[[bin]] path = "crates/core/main.rs"`, so a `crate::` path written in `crates/core/`
    resolves against a directory three levels from the manifest that declares it. Assuming
    the convention would have silently failed on the binary crate of the pinned corpus while
    working for its ten libraries.

    `[lib]` wins over `[[bin]]` because a package that is both is imported as the library;
    `[[test]]` and `[[bench]]` declare a `path` too and are not root modules, which is why
    this reads tables by name rather than scanning for `path =`.
    """
    roots: dict[str, str] = {}
    for manifest in _cargo_manifests(root):
        text = _read(manifest)
        if not text:
            continue
        directory = manifest.parent
        declared = _toml_field(text, "lib", "path") or _toml_field(text, "bin", "path")
        candidates = [*declared, *_CARGO_DEFAULT_ROOTS]
        found = next((c for c in candidates if (directory / c).is_file()), None)
        if found is not None:
            roots[_relative_dir(directory, root)] = _relative_dir(directory / found, root)
    return roots


def _relative_dir(directory: Path, root: Path) -> str:
    try:
        relative = directory.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - a manifest outside the tree
        return ""
    return "" if relative == "." else relative
