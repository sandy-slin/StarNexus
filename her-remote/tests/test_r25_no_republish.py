"""第 25 轮 P-sre：自动更新不再每次都发布一个新的飞书应用版本。"""
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
MAC = os.path.normpath(os.path.join(_REPO, "..", "..", "feishu-agent-oneclick", "src", "install-mac.sh"))


def _segment():
    text = open(MAC, encoding="utf-8").read()
    start = text.index('MENU_ALREADY=""')
    end = text.index("\nfi\n", text.index('if [ "$MENU_ALREADY" = "yes" ]; then')) + 4
    return text[start:end]


def _run(tmp_path, auto, done):
    from bridge import menu_heal

    app = tmp_path / "app"
    (app / "bridge").mkdir(parents=True)
    for name in ("__init__.py", "menu_heal.py", "botmenu.py"):
        src = os.path.join(_REPO, "bridge", name)
        (app / "bridge" / name).write_text(open(src, encoding="utf-8").read() if os.path.exists(src) else "")
    # 假的 botmenu.py：被调用就留一个标记（真的会去飞书发布版本）
    (app / "bridge" / "botmenu.py").write_text("open(__file__ + '.ran', 'w').write('1'); print('OK=常驻菜单已写入')\n")
    config = app / "config.toml"
    config.write_text('[feishu]\napp_id = "cli_x"\n', encoding="utf-8")
    if done:
        state = menu_heal.state_path_for(str(config))
        with open(state, "w") as fh:
            json.dump({"apps": {"cli_x": {"done": True, "scopes": menu_heal.scopes_fingerprint()}}}, fh)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    env = dict(os.environ, CARHER_AUTO_UPDATE="1" if auto else "")
    script = (f'say(){{ :; }}\nAPP_HOME="{app}"\nVENV="{tmp_path / "venv"}"\nCONFIG="{config}"\nAPP_ID=cli_x\n'
              + _segment() + '\necho "OUT=$MENU_OUT"\n')
    proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=env)
    return proc.stdout, (app / "bridge" / "botmenu.py.ran").exists()


def test_auto_update_with_menu_done_does_not_republish(tmp_path):
    out, ran = _run(tmp_path, auto=True, done=True)
    assert not ran, "自动更新又发布了一次应用版本"
    assert "OK=常驻菜单已写入" in out


def test_manual_reinstall_still_runs_menu_setup(tmp_path):
    _out, ran = _run(tmp_path, auto=False, done=True)
    assert ran


def test_auto_update_when_menu_not_done_still_runs(tmp_path):
    _out, ran = _run(tmp_path, auto=True, done=False)
    assert ran


def test_selfupdate_tells_installer_it_is_an_auto_update(monkeypatch, tmp_path):
    import subprocess as sp

    from bridge import selfupdate

    seen = {}
    monkeypatch.setattr(sp, "run", lambda *a, **k: seen.update(k) or sp.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(selfupdate.platform, "system", lambda: "Darwin")
    up = selfupdate.SelfUpdater(str(tmp_path), str(tmp_path / "s.json"), is_idle=lambda: True)
    up.run_installer(str(tmp_path / "x.command"))
    assert seen["env"].get("CARHER_AUTO_UPDATE") == "1"
