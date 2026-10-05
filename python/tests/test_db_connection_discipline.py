from __future__ import annotations

import ast
from pathlib import Path

PY = Path(__file__).resolve().parent.parent

ALLOWED_AWAIT_BLOCKS: set[tuple[str, str]] = set()

_DB_CTX = {"get_db", "_conn"}


def _is_db_ctx(item: ast.withitem) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id in _DB_CTX
    if isinstance(fn, ast.Attribute):
        return fn.attr in _DB_CTX
    return False


class _AwaitsHere(ast.NodeVisitor):

    def __init__(self) -> None:
        self.hits: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node): return
    def visit_AsyncFunctionDef(self, node): return
    def visit_Lambda(self, node): return

    def visit_Await(self, node):
        self.hits.append(("await", node.lineno))
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self.hits.append(("async for", node.lineno))
        self.generic_visit(node)

    def visit_AsyncWith(self, node):
        self.hits.append(("async with", node.lineno))
        self.generic_visit(node)


class _Scan(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.awaits: list[tuple[str, str, int, str, int]] = []
        self.nested: list[tuple[str, int, int]] = []
        self._open: list[int] = []
        self._fn: list[str] = []

    def _func(self, node):
        self._fn.append(node.name)
        self.generic_visit(node)
        self._fn.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def _with(self, node):
        if not any(_is_db_ctx(i) for i in node.items):
            self.generic_visit(node)
            return
        if self._open:
            self.nested.append((self.module, self._open[-1], node.lineno))
        finder = _AwaitsHere()
        for stmt in node.body:
            finder.visit(stmt)
        where = self._fn[0] if self._fn else "<module>"
        for kind, line in finder.hits:
            self.awaits.append((self.module, where, node.lineno, kind, line))
        self._open.append(node.lineno)
        for stmt in node.body:
            self.visit(stmt)
        self._open.pop()

    visit_With = _with
    visit_AsyncWith = _with


def _modules() -> list[Path]:
    return sorted(p for p in PY.glob("*.py") if not p.name.startswith("_"))


def _scan_all() -> tuple[list, list]:
    awaits, nested = [], []
    for path in _modules():
        scan = _Scan(path.name)
        scan.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        awaits += scan.awaits
        nested += scan.nested
    return awaits, nested


def test_scan_actually_reaches_the_backend():
    names = {p.name for p in _modules()}
    assert {"db.py", "trader.py", "scanner.py", "service.py"} <= names
    assert len(names) > 20


def test_no_new_transaction_held_across_an_await():
    unexpected = [
        hit for hit in _scan_all()[0]
        if (hit[0], hit[1]) not in ALLOWED_AWAIT_BLOCKS
    ]
    assert not unexpected, (
        "a db transaction is held open across a suspension point:\n"
        + "\n".join(
            f"  {mod}:{w} in {fn}() -> {kind} at line {a}"
            for mod, fn, w, kind, a in unexpected
        )
        + "\n\nAnother coroutine resuming inside that window shares the "
          "transaction on a reused connection, and its block exit commits the "
          "suspended one's half-written rows. Fetch outside the block, or "
          "split it into two blocks around the await."
    )


def test_allow_list_has_no_stale_entries():
    live = {(mod, fn) for mod, fn, _w, _k, _a in _scan_all()[0]}
    stale = sorted(ALLOWED_AWAIT_BLOCKS - live)
    assert not stale, (
        f"ALLOWED_AWAIT_BLOCKS entries no longer hold a transaction across an "
        f"await (fixed, or renamed): {stale}. Delete them — and if the set is "
        "then empty, db.get_db() can reuse its connection on the loop thread "
        "too (drop the _reuse_ok() guard in db.py)."
    )


def test_no_nested_get_db_blocks():
    nested = _scan_all()[1]
    assert not nested, (
        "a `with get_db()` block opens another one inside itself:\n"
        + "\n".join(f"  {m}: outer line {o} -> inner line {i}" for m, o, i in nested)
    )
