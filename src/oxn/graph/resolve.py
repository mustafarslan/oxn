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

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from oxn.graph.manifests import (
    cargo_crates,
    cargo_roots,
    go_module,
    java_packages,
    normalize,
    tsconfig_aliases,
    workspace_packages,
)
from oxn.graph.resolve_rust import resolve_rust

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
    #: Workspace package name -> its directory, for a JS/TS monorepo. Same shape as an
    #: alias because it is the same idea: a bare specifier that names a directory in this
    #: tree rather than something in ``node_modules``.
    workspaces: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: This repository's own Go module path, from `go.mod`. Every import beginning with it
    #: names a directory in this tree rather than something in the module cache.
    go_module: str | None = None
    #: Java package -> the directory that holds it, derived from the source layout.
    java_packages: dict[str, str] = field(default_factory=dict)
    #: Crate name as an import spells it -> its directory, for a Cargo workspace.
    cargo_crates: dict[str, str] = field(default_factory=dict)
    #: Crate directory -> the file holding its root module, which `crate::` is relative to.
    crate_roots: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path, files: Iterable[str]) -> ResolutionContext:
        known = set(files)
        return cls(
            root=root,
            known=known,
            python_roots=_python_roots(known),
            ts_aliases=tsconfig_aliases(root),
            workspaces=workspace_packages(root),
            go_module=go_module(root),
            java_packages=java_packages(known),
            cargo_crates=cargo_crates(root),
            crate_roots=cargo_roots(root),
        )

    def is_own_module(self, specifier: str) -> bool:
        """Does this specifier name code inside this repository, whatever language it is?

        The question `docs/metrics.md` section 4.1 is about: "third-party and correctly
        absent" and "ours, and we failed to place it" must not look the same. Until
        2026-09-06 every Go, Rust and Java import answered *no*, so 1,131 unplaced imports on
        go-kit were filed as third-party dependencies and nothing was reported.
        """
        if self.go_module is not None and _under(specifier, self.go_module, "/"):
            return True
        # `extract_imports` normalises a Rust path to dots, so `crate::a::b` arrives as
        # `crate.a.b`. Splitting on `::` here found `super` and nothing else -- 17 of
        # ripgrep's imports instead of its real internal count.
        head = specifier.split(".", 1)[0]
        if head in {"crate", "super", "self"} or head in self.cargo_crates:
            return True
        return any(_under(specifier, package, ".") for package in self.java_packages)

    def is_workspace_specifier(self, specifier: str) -> bool:
        """Does this bare specifier name a package of this repository?

        The difference between "third-party, correctly absent" and "ours, and we failed to
        place it" -- which `depgraph` needs in order to report the second and stay quiet
        about the first.
        """
        return any(_alias_matches(specifier, name) for name in self.workspaces)


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
    elif language == "go":
        targets = _resolve_go(raw, context)
    elif language == "rust":
        targets = resolve_rust(source_path, raw, context)
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


def _resolve_ecmascript(source_path: str, raw: RawImport, context: ResolutionContext) -> str | None:
    """A relative specifier resolves against the importing file; a bare one may be an alias.

    Anything that is neither is a package, and packages are not in the tree.
    """
    specifier = raw.specifier
    if specifier.startswith("."):
        base = PurePosixPath(source_path).parent
        return _ecmascript_candidate(normalize(str(base / specifier)), context)
    # `tsconfig` first, workspaces second, and the order is not arbitrary: `paths` is an
    # explicit statement by this repository about where a name resolves, while the
    # workspace layout is an inference from it. A monorepo that declares both and disagrees
    # meant the tsconfig.
    return _resolve_ts_alias(specifier, context) or _resolve_workspace(specifier, context)


def _resolve_workspace(specifier: str, context: ResolutionContext) -> str | None:
    """Resolve a bare specifier that names one of this repository's own packages.

    **This is the part of monorepo support that `tsconfig` `paths` does not already cover**,
    and measuring first is what showed how narrow that part is. On `typescript-nest` -- a
    real npm-workspaces monorepo -- all 1,651 cross-package imports were *already*
    resolving, because nest declares every `@nestjs/*` package in `paths` and OXN has read
    `paths` since P4. What is left is the monorepo that declares `workspaces` and no
    aliases, where `@scope/pkg` is reachable only through a `node_modules` symlink that OXN
    does not follow and should not have to.

    Longest name wins, so `@scope/ui-icons` is not resolved by a package called
    `@scope/ui`. The remainder after the package name is a subpath into it, probed exactly
    as a relative import is -- which is what makes `@scope/pkg/thing.js` find `thing.ts`.
    """
    for name in sorted(context.workspaces, key=len, reverse=True):
        if not _alias_matches(specifier, name):
            continue
        found = _first_existing(
            context.workspaces[name], specifier[len(name) :].lstrip("/"), context
        )
        if found is not None:
            return found
    return None


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


# ---- Go ---------------------------------------------------------------------------------


def _under(specifier: str, prefix: str, separator: str) -> bool:
    """Is ``specifier`` ``prefix`` itself, or something beneath it?

    The check every language's "is this ours" question reduces to, with only the separator
    differing. Written once because getting it wrong the obvious way -- ``startswith(prefix)``
    -- makes ``github.com/go-kit/kitchen`` a submodule of ``github.com/go-kit/kit``.
    """
    return bool(prefix) and (specifier == prefix or specifier.startswith(prefix + separator))


def _resolve_go(raw: RawImport, context: ResolutionContext) -> tuple[str, ...]:
    """The files a Go import reaches, which is a *directory* rather than a file.

    Go imports a package, and a package is every non-test `.go` file in one directory. There
    is no per-file import and no `__init__.py` equivalent, so modelling the edge as one file
    would pick an arbitrary member; the honest target set is all of them.

    A vendored tree needs no special case: `vendor/github.com/other/dep` declares a
    *different* module path, so it never matches this module's prefix and stays external,
    which is what it is.
    """
    module = context.go_module
    if module is None or not _under(raw.specifier, module, "/"):
        return ()
    relative = raw.specifier[len(module) :].strip("/")
    return tuple(sorted(_go_package_files(context.known, relative)))


def _go_package_files(known: set[str], directory: str) -> Iterator[str]:
    """Every `.go` file directly in one directory. Not recursive: a subdirectory is a
    different package, and Go says so by making you import it separately."""
    for path in known:
        if not path.endswith(".go") or path.endswith("_test.go"):
            continue
        parent = str(PurePosixPath(path).parent)
        if (parent if parent != "." else "") == directory:
            yield path


# ---- Java -------------------------------------------------------------------------------
