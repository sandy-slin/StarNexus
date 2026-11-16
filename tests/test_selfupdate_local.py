"""仓库部署的机器不让安装包覆盖代码（2026-11-17 现场事故：自动更新装回旧包，群里发不了消息）。"""
import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import selfupdate


class LocalDeployTest(unittest.TestCase):
    def make(self, marked):
        root = tempfile.mkdtemp()
        pkg = os.path.join(root, "bridge")
        os.makedirs(pkg)
        if marked:
            open(selfupdate.local_deploy_mark(pkg), "w").write("test")
        up = selfupdate.SelfUpdater(pkg, is_idle=lambda: True, state_path=os.path.join(root, "s.json"))
        up.current_build = lambda: "20261113-x"
        return up

    def test_marked_skips_update(self):
        up = self.make(True)
        with mock.patch.object(selfupdate, "latest") as latest:
            self.assertIsNone(up.check())
        latest.assert_not_called()
        self.assertIn("仓库", up.last_error)

    def test_unmarked_checks_normally(self):
        up = self.make(False)
        with mock.patch.object(selfupdate, "latest", return_value=None):
            self.assertIsNone(up.check())
        self.assertIn("查不到更新源", up.last_error)


if __name__ == "__main__":
    unittest.main()
