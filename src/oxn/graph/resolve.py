"""Resolve import specifiers to files inside the analysed tree.

Deliberately narrow, because this is the largest bug source in the whole metric catalogue
(``docs/metrics.md`` section 4.1). OXN resolves what it can do *correctly*:

* **Python** -- package roots (directories holding ``__init__.py``, plus a ``src/`` layout),
  absolute dotted paths, and relative imports anchored to the importing file's package.
* **TypeScript / JavaScript** -- relative paths with extension and ``index`` probing, and
  ``tsconfig.json`` ``baseUrl``/``paths`` aliases.

Everything else -- ``package.json`` ``exports`` maps, workspace globs, symlinked monorepo
packages -- is reported as **external and unresolved** rather than guessed at. A wrong edge
is worse than a missing one that says it is missing: the first corrupts every downstream
metric silently, the second shows up in ``unresolved_imports``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Iterator

    from oxn.graph.imports import RawImport

#: Probed in order when a specifier names no file extension.
_TS_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_TS_INDEXES = tuple(f"index{extension}" for extension in _TS_EXTENSIONS)

#: TypeScript's ESM convention: source imports the *emitted* name, so ``./x.js`` refers to
#: ``./x.ts``. Ignoring this loses essentially every relative import in a modern TS
#: codebase -- 402 of nest's 410, measured.
_JS_TO_TS = {
    ".js": (".ts", ".tsx", ".d.ts"),
    ".mjs": (".mts", ".ts"),
    ".cjs": (".cts", ".ts"),
    ".jsx": (".tsx", ".ts"),
}
_PY_EXTENSIONS = (".py", ".pyi")


@dataclass
class ResolutionContext:
    """What a tree's layout says about where modules live.

    Built once per run and reused for every file, because probing the filesystem for each
    import would dominate the report path.
    """

    root: Path
    #: Repo-relative POSIX paths of every file OXN analyses.
    known: set[str] = field(default_factory=set)
    #: Directories that are Python package roots, longest first.
    python_roots: tuple[str, ...] = ()
    #: ``tsconfig`` alias prefix -> candidate directories.
    ts_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path, files: Iterable[str]) -> ResolutionContext:
        known = set(files)
        return cls(
            root=root,
            known=known,
            python_roots=_python_roots(known),
            ts_aliases=_tsconfig_aliases(root),
        )


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    """An import after resolution.

    One statement can name **several** files. ``from pkg import core`` imports the package
    *and* the submodule, and Python really does execute both -- verified against grimp,
    which reports edges to ``pkg`` and ``pkg.core`` for that single line. Modelling it as one
    target silently loses an edge whenever the package is not imported elsewhere.
    """

    raw: RawImport
    #: Repo-relative paths of every file this import reaches, inside the tree.
    targets: tuple[str, ...]
    external: bool

    @property
    def resolved(self) -> bool:
        return bool(self.targets)

    @property
    def target(self) -> str | None:
        """The primary target, for callers that only need one."""
        return self.targets[0] if self.targets else None


def resolve_import(
    source_path: str, raw: RawImport, context: ResolutionContext, language: str
) -> ResolvedImport:
    """Resolve one import relative to the file that made it."""
    if not raw.specifier and raw.level == 0:
        return ResolvedImport(raw, (), external=False)

    if language == "python":
        targets = _resolve_python(source_path, raw, context)
    else:
        single = _resolve_ecmascript(source_path, raw, context)
        targets = (single,) if single is not None else ()

    if targets:
        return ResolvedImport(raw, targets, external=False)
    return ResolvedImport(raw, (), external=True)


# ---- Python -----------------------------------------------------------------------------


def _python_roots(known: set[str]) -> tuple[str, ...]:
    """Directories from which a dotted module path is spelled.

    A directory is a package root if it *contains* a package (a directory with
    ``__init__.py``) but is not itself one -- which is exactly what ``src/`` layouts and
    flat layouts both produce.
    """
    packages = {
        str(PurePosixPath(path).parent)
        for path in known
        if PurePosixPath(path).name == "__init__.py"
    }
    roots: set[str] = set()
    for package in packages:
        parent = str(PurePosixPath(package).parent)
        if parent not in packages:
            roots.add("" if parent == "." else parent)
    for path in known:
        parent = str(PurePosixPath(path).parent)
        if parent == "." and path.endswith(_PY_EXTENSIONS):
            roots.add("")
    return tuple(sorted(roots, key=len, reverse=True))


def _resolve_python(
    source_path: str, raw: RawImport, context: ResolutionContext
) -> tuple[str, ...]:
    if raw.level:
        base = _relative_base(source_path, raw.level)
        if base is None:
            return ()
        parts = [part for part in (base, raw.specifier.replace(".", "/")) if part]
        return _python_candidates("/".join(parts), raw, context)

    dotted = raw.specifier.replace(".", "/")
    for root in context.python_roots:
        candidate = f"{root}/{dotted}" if root else dotted
        found = _python_candidates(candidate, raw, context)
        if found:
            return found
    return ()


def _relative_base(source_path: str, level: int) -> str | None:
    """Package directory a relative import is anchored to.

    Level 1 is the importing file's own package, level 2 its parent, and so on.
    """
    directory = PurePosixPath(source_path).parent
    for _ in range(level - 1):
        if str(directory) in {"", "."}:
            return None
        directory = directory.parent
    text = str(directory)
    return "" if text == "." else text


def _python_candidates(
    candidate: str, raw: RawImport, context: ResolutionContext
) -> tuple[str, ...]:
    """Every file a dotted path reaches.

    A path may name a module, or a package -- and when it names a package, any imported
    name that is itself a submodule is reached too, because importing it executes both.
    """
    if module := _known_module(context, candidate):
        return (module,)  # a module file; imported names are symbols within it

    package = _known_module(context, f"{candidate}/__init__")
    if package is None and candidate:
        return ()

    found = [package] if package else []
    found.extend(_imported_submodules(candidate, raw, context))
    return tuple(dict.fromkeys(found))


def _known_module(context: ResolutionContext, stem: str) -> str | None:
    """The first extension of `stem` that exists in the tree, or None."""
    for extension in _PY_EXTENSIONS:
        if (path := f"{stem}{extension}") in context.known:
            return path
    return None


def _imported_submodules(
    candidate: str, raw: RawImport, context: ResolutionContext
) -> Iterator[str]:
    """Imported names that are themselves modules or packages under `candidate`.

    `from pkg import sub` reaches `pkg/sub.py` as well as `pkg/__init__.py`, because
    importing the name executes both.
    """
    for name in raw.names:
        base = f"{candidate}/{name}" if candidate else name
        found = _known_module(context, base) or _known_module(context, f"{base}/__init__")
        if found:
            yield found


# ---- TypeScript / JavaScript --------------------------------------------------------------


def _tsconfig_aliases(root: Path) -> dict[str, tuple[str, ...]]:
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
            _normalize("/".join(part for part in (base, str(target).rstrip("*")) if part))
            for target in targets
        )
        aliases[prefix] = resolved
    return aliases


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


def _resolve_ecmascript(source_path: str, raw: RawImport, context: ResolutionContext) -> str | None:
    """A relative specifier resolves against the importing file; a bare one may be an alias.

    Anything that is neither is a package, and packages are not in the tree.
    """
    specifier = raw.specifier
    if specifier.startswith("."):
        base = PurePosixPath(source_path).parent
        return _ecmascript_candidate(_normalize(str(base / specifier)), context)
    return _resolve_ts_alias(specifier, context)


def _resolve_ts_alias(specifier: str, context: ResolutionContext) -> str | None:
    """Try each `tsconfig` path alias whose prefix this specifier matches.

    An alias may name several targets and they are ordered by preference, so the first that
    exists wins -- which is what `tsc` does, and why a miss falls through to the next
    target rather than failing the whole lookup.
    """
    for prefix, targets in context.ts_aliases.items():
        if not prefix or not _alias_matches(specifier, prefix):
            continue
        found = _first_existing(targets, specifier[len(prefix) :].lstrip("/"), context)
        if found is not None:
            return found
    return None


def _first_existing(
    targets: tuple[str, ...], suffix: str, context: ResolutionContext
) -> str | None:
    """The first alias target that exists, which is what `tsc` picks.

    Targets are ordered by preference, so a miss falls through to the next rather than
    failing the whole lookup.
    """
    for target in targets:
        candidate = "/".join(part for part in (target, suffix) if part)
        found = _ecmascript_candidate(candidate, context)
        if found is not None:
            return found
    return None


def _alias_matches(specifier: str, prefix: str) -> bool:
    """An alias matches the whole specifier or a leading path segment of it."""
    return specifier == prefix or specifier.startswith(f"{prefix}/")


def _normalize(path: str) -> str:
    """Collapse ``.`` and ``..`` without touching the filesystem."""
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


def _ecmascript_candidate(candidate: str, context: ResolutionContext) -> str | None:
    """A specifier may name the file exactly, its source form, or a directory's index."""
    if candidate in context.known:
        return candidate
    return (
        _emitted_source(candidate, context)
        or _first_known_path(context, (f"{candidate}{ext}" for ext in _TS_EXTENSIONS))
        or _first_known_path(context, (f"{candidate}/{index}" for index in _TS_INDEXES))
    )


def _emitted_source(candidate: str, context: ResolutionContext) -> str | None:
    """`./x.js` in TypeScript source names `./x.ts`.

    The specifier refers to the *emitted* file, not the one on disk, which is the one
    ECMAScript resolution rule that cannot be expressed as "try these extensions".
    """
    suffix = PurePosixPath(candidate).suffix
    if suffix not in _JS_TO_TS:
        return None
    stem = candidate[: -len(suffix)]
    return _first_known_path(context, (f"{stem}{ext}" for ext in _JS_TO_TS[suffix]))


def _first_known_path(context: ResolutionContext, paths: Iterator[str]) -> str | None:
    return next((path for path in paths if path in context.known), None)
