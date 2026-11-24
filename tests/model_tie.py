"""モデルを、それが写す関数の本文に結ぶ（tests/test_oprun_model.py・tests/test_task_model.py・tests/test_sweep_model.py）。

ハッシュは、関数の本文（デコレータを含む）と、本文が読むモジュールの水準の定数の代入の文から、コメント・先頭の docstring・空行・
行末の空白を除いたものに掛ける（Python の版によらない）。意味の同じ書式の変更（括弧・引用符・改行の位置）でも変わる。そのときも
ハッシュを進める。モジュールの水準（if・try などの中を含む）の def・class・代入・import の文で、その名前を 2 か所で束ねると落ちる
（代入式 := や globals()・setattr による動的な束ね直しは見ない）。
ハッシュは、写す関数が変わったことを知らせて、モデルを見直させるためのもの。ハッシュを進めたときにモデルと照らしたかは確かめない
（直しを戻してハッシュだけを進める迂回は、tests/test_replay.py が実物の写しで落とす）。
"""

import ast
import hashlib
import io
import tokenize


def bindings(tree: ast.Module) -> list:
    """モジュールの水準で名前を束ねる文と、その名前の (文, 名前) の列。def・class・代入・import の文を、if・try などの中も見る。
    関数・クラスの中と、代入式（:=）・globals()・setattr による動的な束ね直しは見ない。"""
    out, todo = [], list(tree.body)
    while todo:
        n = todo.pop(0)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((n, n.name))
            continue
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            out += [(n, t.id) for x in targets for t in ast.walk(x) if isinstance(t, ast.Name)]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out += [(n, (a.asname or a.name).split(".")[0]) for a in n.names]
        for field in ("body", "orelse", "finalbody", "handlers"):  # 複合文の中の文
            todo += [c for c in getattr(n, field, []) if isinstance(c, (ast.stmt, ast.excepthandler))]
        if isinstance(n, ast.ExceptHandler) and n.name:
            out.append((n, n.name))
    return out


def stripped(src: str, node: ast.AST, doc=None) -> str:
    """node の行から、コメント・doc（先頭の docstring の式）の字句・空行・行末の空白を除いたもの。"""
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    text = "\n".join(src.splitlines()[start - 1:node.end_lineno]) + "\n"
    cut = []  # 除く字句の (始まり, 終わり)。行は text の中で 1 から、列は文字で数える
    at = "before" if doc else "none"  # 先頭の docstring の字句の前・中・後
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type == tokenize.COMMENT:
            cut.append((t.start, t.end))
        elif at == "before" and t.type == tokenize.STRING and t.start[0] == doc.lineno - start + 1:
            at = "in"  # 同じ行の後ろの文（; の後）は残す
            cut.append((t.start, t.end))
        elif at == "in" and t.type == tokenize.STRING:  # 暗黙の連結
            cut.append((t.start, t.end))
        elif at == "in" and t.type != tokenize.NL:
            at = "after"
    lines = text.splitlines()
    for (r0, c0), (r1, c1) in reversed(cut):
        lines[r0 - 1:r1] = [lines[r0 - 1][:c0] + lines[r1 - 1][c1:]]
    return "\n".join(line.rstrip() for line in lines if line.strip())


def body(src: str, name: str) -> str:
    """モジュールの水準の関数 name の本文（デコレータを含む）から、コメント・先頭の docstring・空行・行末の空白を除いたものに、
    本文が読むモジュールの水準の定数（MAX_LEASE_MIN など、代入で束ねた名前）の代入の文を、同じように除いて足したもの。
    モジュールの水準で name を束ねる文が、その def 1 つでなければ LookupError（後の束ね直しが勝ち、照らした本文が動かなくなる）。"""
    tree = ast.parse(src)
    binds = bindings(tree)
    found = [n for n, b in binds if b == name]
    if len(found) != 1 or not isinstance(found[0], ast.FunctionDef):
        raise LookupError(f"モジュールの水準で、{name} を束ねる文が def 1 つでない（{len(found)} か所）")
    node = found[0]
    first = node.body[0]
    doc = first if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str) else None
    read = {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
    consts = sorted({id(n): n for n, b in binds if b in read and isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))}.values(),
                    key=lambda n: n.lineno)
    return "\n".join([stripped(src, node, doc)] + [stripped(src, n) for n in consts])


def fingerprint(src: str, name: str) -> str:
    return hashlib.sha256(body(src, name).encode("utf-8")).hexdigest()[:16]


def drift(src: str, modeled: dict) -> list:
    """modeled（関数の名前とハッシュ）のうち、src の今の本文と違うものを「名前: 照合した … / 今 …」の行で返す。"""
    return [f"{name}: 照合した {old} / 今 {fingerprint(src, name)}" for name, old in modeled.items() if fingerprint(src, name) != old]
