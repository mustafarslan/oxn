"""Resolve a Rust `use` path to the file that holds the module it names.

Rust's module tree mirrors its directory tree, so this probes the known file set the way
`_resolve_python` does rather than walking `mod` declarations. Reading `mod` would be the
authority -- it is what `#[path = "..."]` and `#[cfg]`-gated modules need -- and it costs a
parse of every file to resolve one import. The layout is right for everything ripgrep
writes, and what it is wrong about is *named* below rather than guessed at.

Four heads, three of which are ours:

* ``crate`` -- from the root module of the crate the importing file belongs to, which is not
  always `src/lib.rs`: ripgrep's own package declares `[[bin]] path = "crates/core/main.rs"`;
* ``self`` -- from the importing file's own module;
* ``super`` -- from its parent module;
* a **workspace crate name** (`grep_searcher`) -- from that crate's root module.

Anything else is a dependency (`std`, `bstr`, `serde`) and stays external, which is correct.

**A tail is usually a type, not a module.** `crate::searcher::Searcher` names `Searcher`
inside `searcher`, so the probe drops one segment at a time until a file exists, and falls
back to the base module's own file -- `use crate::SearcherBuilder` reaches the crate root.

**What this does not follow, stated rather than discovered later.** A `pub use` re-export
(`grep::matcher::Matcher`, where `grep/src/lib.rs` says `pub use grep_matcher as matcher`)
resolves to the re-exporting crate's root rather than to the crate that defines the item;
`#[path]` attributes are ignored; a `#[cfg]`-gated module absent from this checkout cannot be
found. Each leaves a *reported* missing edge rather than a wrong one.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from oxn.graph.imports import RawImport
    from oxn.graph.resolve import ResolutionContext

#: Heads that name a position in the importing file's own crate rather than another crate.
POSITIONAL = frozenset({"crate", "self", "super"})

#: File stems that *are* their module rather than opening one below it. `foo.rs` holds module
#: `foo` and its children live in `foo/`; `foo/mod.rs` holds `foo` and its children are its
#: siblings.
_ROOT_STEMS = frozenset({"lib", "main", "mod"})


def resolve_rust(source_path: str, raw: RawImport, context: ResolutionContext) -> tuple[str, ...]:
    """Every in-tree file a `use` path reaches, which is at most one."""
    segments = [segment for segment in raw.specifier.split(".") if segment]
    if not segments:
        return ()
    head, rest = segments[0], segments[1:]

    if head in POSITIONAL:
        base = _positional_base(source_path, head, context)
    elif head in context.cargo_crates:
        base = _crate_base(context.cargo_crates[head], context)
    else:
        return ()  # a dependency, correctly external
    if base is None:
        return ()

    directory, own_file = base
    found = _probe(directory, rest, context)
    return (found,) if found else ((own_file,) if own_file else ())


def _positional_base(
    source_path: str, head: str, context: ResolutionContext
) -> tuple[str, str | None] | None:
    """``(directory children live in, the file this module is written in)``."""
    path = PurePosixPath(source_path)
    if head == "self":
        return _module_dir(path), source_path
    if head == "super":
        parent = _parent_dir(path)
        # A crate root has no parent module, so a `super` written *in* one can only come
        # from an inline `mod` -- `#[cfg(test)] mod tests { use super::GlobSet; }` -- whose
        # parent is the crate root itself. Inferred, not guessed: there is nowhere else for
        # it to mean. These were the last 3 unresolved imports on ripgrep, and they resolve
        # to the file that wrote them, which the graph drops as a self-edge.
        if parent is None:
            return _module_dir(path), source_path
        return parent, _module_file(parent, context)
    return _crate_base(_crate_of(source_path, context), context)


def _crate_base(directory: str | None, context: ResolutionContext) -> tuple[str, str | None] | None:
    """A crate directory's root module, and the directory its children live in."""
    if directory is None:
        return None
    root_file = context.crate_roots.get(directory)
    if root_file is None:
        return None
    return str(PurePosixPath(root_file).parent), root_file


def _crate_of(source_path: str, context: ResolutionContext) -> str | None:
    """The crate a file belongs to: the longest crate directory that contains it.

    Longest rather than first, because a workspace root is a crate too and is a prefix of
    every member -- `""` contains `crates/searcher/src/lib.rs` and is the wrong answer for it.
    """
    owning = [
        directory
        for directory in context.crate_roots
        if not directory or source_path.startswith(f"{directory}/")
    ]
    return max(owning, key=len) if owning else None


def _module_dir(path: PurePosixPath) -> str:
    """Where a file's child modules live: beside `mod.rs`, in `foo/` for `foo.rs`."""
    parent = "" if str(path.parent) == "." else str(path.parent)
    if path.stem in _ROOT_STEMS:
        return parent
    return f"{parent}/{path.stem}" if parent else path.stem


def _parent_dir(path: PurePosixPath) -> str | None:
    """Where `super`'s children live. `lib.rs` and `main.rs` have no parent module."""
    if path.stem in {"lib", "main"}:
        return None
    parent = PurePosixPath("" if str(path.parent) == "." else str(path.parent))
    if path.stem == "mod":
        return "" if str(parent.parent) == "." else str(parent.parent)
    return str(parent) if str(parent) != "." else ""


def _module_file(directory: str, context: ResolutionContext) -> str | None:
    """The file a module directory is written in: `d/mod.rs`, `d.rs` beside it -- or a crate
    root, when the directory *is* one.

    The third case is not an edge: it was all 54 imports still unresolved on ripgrep after
    the rest of this module worked. `crates/searcher/src/sink.rs` writing `use super::Sink`
    means the crate root, and a crate root is `lib.rs` or `main.rs` -- never `mod.rs`, and
    never `src.rs`. Every file directly beneath a crate root has this parent.
    """
    for candidate in (f"{directory}/mod.rs" if directory else "mod.rs", f"{directory}.rs"):
        if candidate in context.known:
            return candidate
    return next(
        (
            root_file
            for root_file in context.crate_roots.values()
            if str(PurePosixPath(root_file).parent) == directory
        ),
        None,
    )


def _probe(directory: str, segments: list[str], context: ResolutionContext) -> str | None:
    """The deepest prefix of `segments` that names a file, dropping the type off the tail."""
    remaining = list(segments)
    while remaining:
        stem = "/".join([directory, *remaining]) if directory else "/".join(remaining)
        for candidate in (f"{stem}.rs", f"{stem}/mod.rs"):
            if candidate in context.known:
                return candidate
        remaining.pop()
    return None
