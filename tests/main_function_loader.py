"""Extract only tested top-level functions; avoid parsing the app's 8MB UI templates."""
import ast
from functools import lru_cache
from pathlib import Path
import re

@lru_cache(maxsize=1)
def source():
    return (Path(__file__).parents[1]/'main.py').read_text(encoding='utf-8-sig')


def functions_tree(names):
    nodes=[]
    for name in sorted(names):
        match=re.search(r'(?ms)^def '+re.escape(name)+r'\b.*?(?=^def |^@|^class |\Z)',source())
        if not match:raise AssertionError('Production function missing: '+name)
        nodes.append(next(n for n in ast.parse(match[0]).body if isinstance(n,ast.FunctionDef) and n.name==name))
    return ast.Module(body=nodes,type_ignores=[])
