#!/usr/bin/env python3
"""发版前闸：安装包里嵌的每个 bridge 文件 == 仓库里那份。不一致 ⇒ 退 1，不许发。

🔴 为什么有它（11-16）：别的会话用「安装器取工作区、bridge 从更早的线上包里拆出来」的镜像脚本
发了两版 ⇒ 公网包里没有 fleet/newsession.py ⇒ 已上线的「➕ 新会话」整版消失，发布脚本照样打绿
（它只核「上传的 == 本地产物」，不核「产物 == 源码」）。

用法：python3 check_package_matches_source.py <安装包> <安装模板> <bridge 根目录>
"""
import re
import sys


def main(pkg_path: str, tpl_path: str, src_root: str) -> int:
    pkg = open(pkg_path, encoding="utf-8").read()
    tpl = open(tpl_path, encoding="utf-8").read()
    rels = sorted(set(re.findall(r"@@EMBED ([^@]+)@@", tpl)))
    if not rels:
        print(f"✗ {tpl_path} 里一个 @@EMBED 都没找到", file=sys.stderr)
        return 1
    bad = []
    for rel in rels:
        try:
            body = open(f"{src_root}/{rel}", encoding="utf-8").read().rstrip("\n")
        except OSError as exc:
            bad.append(f"{rel}（源码读不到：{exc}）")
            continue
        if body not in pkg:
            bad.append(rel)
    if bad:
        print(f"✗ 安装包里有 {len(bad)} 个文件和仓库源码不一样（包不是用当前源码打的，先跑 build.sh）：", file=sys.stderr)
        for rel in bad:
            print(f"    {rel}", file=sys.stderr)
        return 1
    print(f"✓ 安装包里嵌的 {len(rels)} 个文件和仓库源码一致")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
