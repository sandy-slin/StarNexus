"""从 cmdclass.py 生成钩子内嵌副本。

不用正则替换 `return Verdict(...)` —— 那对多行调用和 f-string 不可靠
（实测把 f-string 截断成 `return False[:40]}` 这种语法错误）。
改用 AST：把 `Verdict(a, b)` 整个节点换成 `a`。
"""
import ast, pathlib

mod_src = pathlib.Path("bridge/cmdclass.py").read_text()
tree = ast.parse(mod_src)


class Strip(ast.NodeTransformer):
    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "Verdict":
            return node.args[0]          # Verdict(False, "...") -> False
        if isinstance(node.func, ast.Name) and node.func.id == "classify":
            node.func = ast.Name(id="_classify_readonly", ctx=ast.Load())
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        # inner_verdict.readonly -> inner_verdict
        if node.attr == "readonly":
            return node.value
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if node.name == "classify":
            node.name = "_classify_readonly"
            node.returns = ast.Name(id="bool", ctx=ast.Load())
        return node


keep = []
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == "Verdict":
        continue
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        continue
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        continue                          # 模块 docstring
    keep.append(Strip().visit(node))

# pathguard 也要嵌进钩子（独立进程、只能用标准库）
guard_src = pathlib.Path("bridge/pathguard.py").read_text()
gtree = ast.parse(guard_src)
gkeep = []
for node in gtree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        continue
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        continue
    if isinstance(node, ast.FunctionDef) and node.name == "outside_workspace":
        node.name = "_outside_workspace"
    gkeep.append(node)

block = ast.unparse(ast.Module(body=keep + gkeep, type_ignores=[]))
assert "Verdict" not in block, [l for l in block.splitlines() if "Verdict" in l][:3]
compile(block, "hookblock", "exec")

p = pathlib.Path("bridge/approval.py")
t = p.read_text()
i = t.index("HOOK_SOURCE = r")
s = t.index("READERS = frozenset(", i)
e = t.index("def digest(tool_name: str, tool_input: dict) -> str:", s)
p.write_text(t[:s] + block + "\n\n\n" + t[e:])
print(f"钩子已同步（{len(block.splitlines())} 行，AST 生成）")
