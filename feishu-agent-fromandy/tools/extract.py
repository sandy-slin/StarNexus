"""把官方安装包 feishu-agent-mac.command 里嵌入的文件按原路径还原出来。

安装包里每个文件都写成：
    write_file "$APP_HOME/bridge/app.py" <<'CARHER_PY_EOF'
    ...正文...
    CARHER_PY_EOF
本脚本只读安装包，不执行其中任何内容。用法：
    python3 tools/extract.py <安装包> <输出目录>
"""
import os
import re
import sys

START = re.compile(r"""^\s*write_file\s+"([^"]+)"\s*<<-?'?([A-Z0-9_]+)'?\s*$""")
VARS = {"$APP_HOME": "app_home", "${APP_HOME}": "app_home"}


def map_path(raw: str) -> str:
    for var, repl in VARS.items():
        if raw.startswith(var):
            return repl + raw[len(var):]
    return "other/" + re.sub(r"[^A-Za-z0-9._/-]", "_", raw.lstrip("$"))


def main() -> int:
    src, out = sys.argv[1], sys.argv[2]
    lines = open(src, encoding="utf-8", errors="replace").read().split("\n")
    written = []
    i = 0
    while i < len(lines):
        m = START.match(lines[i])
        if not m:
            i += 1
            continue
        rel, tag = map_path(m.group(1)), m.group(2)
        body = []
        i += 1
        while i < len(lines) and lines[i] != tag:
            body.append(lines[i])
            i += 1
        rel = os.path.normpath(rel)
        if rel.startswith("..") or os.path.isabs(rel):
            print("跳过可疑路径", rel, file=sys.stderr)
            i += 1
            continue
        dest = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
        written.append(rel)
        i += 1
    print(f"还原 {len(written)} 个文件")
    for rel in written:
        print(" ", rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
