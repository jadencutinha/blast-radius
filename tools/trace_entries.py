"""pytest plugin for a one-test process: record the table entries the test reads.

While sqlglot is imported, its AST is rewritten with line numbers preserved:
class-level dict literals become dicts that remember each key's defining line,
through inheritance by **spread, and reads of class attributes are recorded
against the class that defines them. Iterating a table counts as reading all of
it. Used only to build maps. The scored image never runs this.
"""

import ast
import builtins
import importlib._bootstrap_external as external
import importlib.util
import json
import os
import sys

ROOT = "/src/"
SCAN = json.load(open(os.environ["TABLE_SCAN"]))
NAMES = set(SCAN["names"])
REGISTRY = SCAN["registry"]
seen: set[str] = set()


class TrackedDict(dict):
    def _mark_all(self) -> None:
        if not self._all:
            self._all = True
            seen.update(line for line in self._origin.values() if line)

    def _mark(self, key) -> None:
        try:
            line = self._origin.get(key)
        except TypeError:
            return
        if line:
            seen.add(line)

    def __getitem__(self, key):
        self._mark(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._mark(key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        self._mark(key)
        return dict.__contains__(self, key)

    def setdefault(self, key, default=None):
        self._mark(key)
        return dict.setdefault(self, key, default)

    def pop(self, key, *default):
        self._mark(key)
        return dict.pop(self, key, *default)

    def __iter__(self):
        self._mark_all()
        return dict.__iter__(self)

    def __reversed__(self):
        self._mark_all()
        return dict.__reversed__(self)

    def __len__(self):
        self._mark_all()
        return dict.__len__(self)

    def keys(self):
        self._mark_all()
        return dict.keys(self)

    def values(self):
        self._mark_all()
        return dict.values(self)

    def items(self):
        # Expression.__init_subclass__ derives required_args here. Reads of
        # required_args are recorded against arg_types instead.
        if sys._getframe(1).f_code.co_name != "__init_subclass__":
            self._mark_all()
        return dict.items(self)

    def popitem(self):
        self._mark_all()
        return dict.popitem(self)

    def copy(self):
        self._mark_all()
        return dict(dict.items(self))

    def __eq__(self, other):
        self._mark_all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._mark_all()
        return dict.__ne__(self, other)

    __hash__ = None

    def __repr__(self):
        self._mark_all()
        return dict.__repr__(self)

    def __or__(self, other):
        self._mark_all()
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        self._mark_all()
        return other | dict(dict.items(self))

    def __reduce_ex__(self, protocol):
        self._mark_all()
        return dict, (dict(dict.items(self)),)


def build_dict(path: str, items: list) -> TrackedDict:
    result = TrackedDict()
    result._all = False
    origin: dict = {}
    for line, key, value in items:
        if line == 0:  # a ** spread
            if isinstance(value, TrackedDict):
                for inner_key, inner_value in dict.items(value):
                    dict.__setitem__(result, inner_key, inner_value)
                    origin[inner_key] = value._origin.get(inner_key)
            else:
                for inner_key, inner_value in dict(value).items():
                    dict.__setitem__(result, inner_key, inner_value)
                    origin.pop(inner_key, None)
        else:
            dict.__setitem__(result, key, value)
            origin[key] = f"{path}:{line}"
    result._origin = origin
    return result


def record_attribute(owner, name: str) -> None:
    if name == "required_args":
        table = getattr(owner, "arg_types", None)
        if isinstance(table, TrackedDict):
            table._mark_all()
        return
    if isinstance(owner, super):
        order = owner.__self_class__.__mro__
        order = order[order.index(owner.__thisclass__) + 1:]
    elif isinstance(owner, type):
        order = owner.__mro__
    else:
        if name in getattr(owner, "__dict__", ()):
            return
        order = type(owner).__mro__
    for klass in order:
        if name in klass.__dict__:
            line = REGISTRY.get(f"{klass.__module__}:{klass.__qualname__}:{name}")
            if line:
                seen.add(line)
            return


def read_attribute(owner, name: str):
    value = getattr(owner, name)
    record_attribute(owner, name)
    return value


def read_getattr(owner, name, *default):
    value = getattr(owner, name, *default)
    if isinstance(name, str) and name in NAMES:
        record_attribute(owner, name)
    return value


builtins.__blast_dict__ = build_dict
builtins.__blast_attr__ = read_attribute
builtins.__blast_getattr__ = read_getattr


class Rewriter(ast.NodeTransformer):
    def __init__(self, path: str) -> None:
        self.path = path

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in NAMES:
            call = ast.Call(ast.Name("__blast_attr__", ast.Load()),
                            [node.value, ast.Constant(node.attr)], [])
            return ast.copy_location(call, node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "getattr" and not node.keywords:
            node.func = ast.copy_location(ast.Name("__blast_getattr__", ast.Load()), node.func)
        return node

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        for item in node.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)) and isinstance(item.value, ast.Dict):
                rows = []
                for key, value in zip(item.value.keys, item.value.values):
                    if key is None:
                        row = [ast.Constant(0), ast.Constant(None), value]
                    else:
                        row = [ast.Constant(key.lineno), key, value]
                    rows.append(ast.Tuple(row, ast.Load()))
                call = ast.Call(ast.Name("__blast_dict__", ast.Load()),
                                [ast.Constant(self.path), ast.List(rows, ast.Load())], [])
                item.value = ast.copy_location(call, item.value)
        return node


original = external.SourceFileLoader.source_to_code


def source_to_code(self, data, path, *, _optimize=-1):
    text_path = str(path)
    if not text_path.startswith(ROOT + "sqlglot/"):
        return original(self, data, path, _optimize=_optimize)
    source = importlib.util.decode_source(data)
    tree = Rewriter(text_path[len(ROOT):]).visit(ast.parse(source, text_path))
    ast.fix_missing_locations(tree)
    return compile(tree, text_path, "exec", dont_inherit=True, optimize=_optimize)


external.SourceFileLoader.source_to_code = source_to_code
sys.dont_write_bytecode = True


def pytest_sessionfinish(session, exitstatus) -> None:
    with open(os.environ["ENTRY_OUTPUT"], "w") as handle:
        json.dump(sorted(seen), handle)
