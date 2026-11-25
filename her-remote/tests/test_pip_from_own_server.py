"""11-13 同事实机：清华镜像和 pypi.org 两个源都失败，而安装包本身是从我们服务器下到的。
⇒ 组件包放到 198 /ccdl/feishu-agent/wheels/，两个平台的安装器都必须**先**从这儿装，清华镜像只作兜底。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "feishu-agent-oneclick" / "src"
WHEELS = "https://chat.auto-link.com.cn/ccdl/feishu-agent/wheels/index.html"


def test_windows_installs_from_own_server_first():
    s = (ROOT / "install-windows.ps1").read_text(encoding="utf-8")
    assert WHEELS in s
    ours = s.index("+ $pipOurs + @('lark-oapi>=1.7.5')")
    tuna = s.index("'-i',$pipIndex,'lark-oapi>=1.7.5'")
    assert ours < tuna, "Windows 应先从我们服务器装，清华镜像在后"
    assert "--upgrade pip" in s and "@pipOurs --upgrade pip" in s, "升级 pip 也要走我们服务器(否则断网时空等超时)"
    assert "飞书官方源" not in s and "pypi.org 的话" not in s


def test_mac_installs_from_own_server_first():
    s = (ROOT / "install-mac.sh").read_text(encoding="utf-8")
    assert WHEELS in s
    ours = s.index('install --no-index --find-links "$PIP_WHEELS" "lark-oapi')
    tuna = s.index('install -i "$PIP_INDEX" "lark-oapi')
    assert ours < tuna
    assert '--find-links "$PIP_WHEELS" --upgrade pip' in s
    assert '--find-links "$PIP_WHEELS" qrcode' in s
    assert "飞书官方源" not in s
