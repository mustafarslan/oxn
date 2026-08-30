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
    from collections.abc import Iterable

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
    found: list[str] = []

    for extension in _PY_EXTENSIONS:
        if (path := f"{candidate}{extension}") in context.known:
            return (path,)  # a module file; imported names are symbols within it

    is_package = False
    for extension in _PY_EXTENSIONS:
        if (path := f"{candidate}/__init__{extension}") in context.known:
            found.append(path)
            is_package = True
            break

    if is_package or not candidate:
        for name in raw.names:
            for extension in _PY_EXTENSIONS:
                if (
                    path := f"{candidate}/{name}{extension}" if candidate else f"{name}{extension}"
                ) in context.known:
                    found.append(path)
                    break
            else:
                for extension in _PY_EXTENSIONS:
                    sub = (
                        f"{candidate}/{name}/__init__{extension}"
                        if candidate
                        else f"{name}/__init__{extension}"
                    )
                    if sub in context.known:
                        found.append(sub)
                        break
    return tuple(dict.fromkeys(found))


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
    """Remove ``//`` comments and trailing commas -- both legal in a tsconfig."""
    lines = []
    for line in text.splitlines():
        stripped = line.split("//")[0] if "//" in line and '"' not in line.split("//")[0] else line
        lines.append(stripped)
    joined = "\n".join(lines)
    out: list[str] = []
    for index, char in enumerate(joined):
        if char == ",":
            rest = joined[index + 1 :].lstrip()
            if rest[:1] in {"}", "]"}:
                continue
        out.append(char)
    return "".join(out)


def _resolve_ecmascript(source_path: str, raw: RawImport, context: ResolutionContext) -> str | None:
    specifier = raw.specifier
    if specifier.startswith("."):
        base = PurePosixPath(source_path).parent
        candidate = _normalize(str(base / specifier))
        return _ecmascript_candidate(candidate, context)

    for prefix, targets in context.ts_aliases.items():
        if prefix and (specifier == prefix or specifier.startswith(f"{prefix}/")):
            suffix = specifier[len(prefix) :].lstrip("/")
            for target in targets:
                candidate = "/".join(part for part in (target, suffix) if part)
                found = _ecmascript_candidate(candidate, context)
                if found is not None:
                    return found
    return None


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
    if candidate in context.known:
        return candidate

    # `./x.js` in TypeScript source names `./x.ts`: the specifier refers to the emitted
    # file, not the one on disk.
    suffix = PurePosixPath(candidate).suffix
    if suffix in _JS_TO_TS:
        stem = candidate[: -len(suffix)]
        for extension in _JS_TO_TS[suffix]:
            if (path := f"{stem}{extension}") in context.known:
                return path

    for extension in _TS_EXTENSIONS:
        if (path := f"{candidate}{extension}") in context.known:
            return path
    for index in _TS_INDEXES:
        if (path := f"{candidate}/{index}") in context.known:
            return path
    return None
