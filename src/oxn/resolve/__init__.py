"""Name resolution rungs L0 and L1 (ADR-0002).

**L0** is a lexical scope resolver over the CST: bindings, shadowing, parameters, closure
and comprehension scopes, `self`/`this` receivers and class member tables. It needs no
toolchain and runs in microseconds, which is why it is what the hook can afford.

**L1** stitches L0's per-file results into a project symbol table, so a name imported from
another file resolves to the entity that declares it.

Neither can do what a type checker does. The published measurement of exactly how much they
miss -- against L2 as ground truth -- is the point of building them this way round.
"""

from oxn.resolve.scopes import Binding, Scope, ScopeTree, build_scopes
from oxn.resolve.symbols import ProjectSymbols, ResolvedName, build_project_symbols

__all__ = [
    "Binding",
    "ProjectSymbols",
    "ResolvedName",
    "Scope",
    "ScopeTree",
    "build_project_symbols",
    "build_scopes",
]
