"""Tests for artiq_client functionality"""

import subprocess
import sys
import os
import unittest
from tempfile import TemporaryDirectory
from pygit2 import init_repository, Signature

EXPERIMENT_CONTENT = """
from artiq.experiment import *
class EmptyExperiment(EnvExperiment):
    def build(self):
        pass
    def run(self):
        print("test content")
"""

IMPORTING_EXPERIMENT_CONTENT = """
from artiq.experiment import *
import lib
class ImportingExperiment(EnvExperiment):
    def build(self):
        pass
    def run(self):
        import lib2
        print("imported:", lib.WHICH, lib2.WHICH)
"""

DDB_CONTENT = """
device_db = {}
"""


def get_env():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return env


class TestClient(unittest.TestCase):
    def setUp(self):
        self.master = None
        self.tmp_dir = TemporaryDirectory(prefix="artiq_client_test")
        self.tmp_empty_dir = TemporaryDirectory(prefix="artiq_empty_repo")
        self.exp_name = "experiment.py"
        self.exp_path = os.path.join(self.tmp_dir.name, self.exp_name)
        self.device_db_path = os.path.join(self.tmp_dir.name, "device_db.py")
        with open(self.exp_path, "w") as f:
            f.write(EXPERIMENT_CONTENT)
        with open(self.device_db_path, "w") as f:
            f.write(DDB_CONTENT)

    def start_master(self, *args, cwd=None):
        self.master = subprocess.Popen([sys.executable, "-m", "artiq.frontend.artiq_master", "--device-db",
                                        self.device_db_path, *args], encoding="utf8", env=get_env(),
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       cwd=cwd)
        while self.master.stdout.readline().strip() != "ARTIQ master is now ready.":
            pass

    def check_and_terminate_master(self):
        while not ("test content" in self.master.stdout.readline()):
            pass
        self.run_client("terminate")
        self.assertEqual(self.master.wait(), 0)
        self.master.stdout.close()

    @staticmethod
    def run_client(*args):
        subprocess.run([sys.executable, "-m", "artiq.frontend.artiq_client", *args], check=True,
                       capture_output=True, env=get_env(), text=True, encoding="utf8").check_returncode()

    def test_submit_outside_repo(self):
        self.start_master("-r", self.tmp_empty_dir.name)
        self.run_client("submit", self.exp_path)
        self.check_and_terminate_master()

    def test_submit_by_content(self):
        self.start_master("-r", self.tmp_empty_dir.name)
        self.run_client("submit", self.exp_path, "--content")
        self.check_and_terminate_master()

    def test_submit_by_file_repo(self):
        self.start_master("-r", self.tmp_dir.name)
        self.run_client("submit", self.exp_name, "-R")
        self.check_and_terminate_master()

    @staticmethod
    def commit_all(repo, message):
        repo.index.add_all()
        repo.index.write()
        tree = repo.index.write_tree()
        signature = Signature("Test", "test@example.com")
        parents = [] if repo.head_is_unborn else [repo.head.target]
        return repo.create_commit("HEAD", signature, signature, message,
                                  tree, parents)

    def test_submit_by_git_repo(self):
        repo = init_repository(self.tmp_dir.name)
        self.commit_all(repo, "Commit message")

        self.start_master("-r", self.tmp_dir.name, "-g")
        self.run_client("submit", self.exp_name, "-R")
        self.check_and_terminate_master()

    def test_submit_pinned_revision_imports(self):
        # Modules imported by a repository experiment must resolve to the
        # requested revision's checkout, both at build time and for imports
        # performed later (e.g. in run()). This must hold even when the
        # repository working tree contains a different version of those
        # modules and is itself reachable on sys.path -- as it is when the
        # master is started from within the repository, since sys.path[0]
        # of the worker is the master's working directory.
        os.mkdir(os.path.join(self.tmp_dir.name, "subdir"))
        exp_rel_path = os.path.join("subdir", "importing_experiment.py")
        with open(os.path.join(self.tmp_dir.name, exp_rel_path), "w") as f:
            f.write(IMPORTING_EXPERIMENT_CONTENT)

        def write_libs(which):
            for lib_name in ("lib.py", "lib2.py"):
                with open(os.path.join(self.tmp_dir.name, lib_name), "w") as f:
                    f.write('WHICH = "{}"\n'.format(which))

        repo = init_repository(self.tmp_dir.name)
        write_libs("rev1")
        rev1 = str(self.commit_all(repo, "rev1"))
        write_libs("rev2")
        self.commit_all(repo, "rev2")
        # The working tree is now at rev2, while rev1 is requested below.

        # Start the master from within the repository working tree, so that
        # the rev2 modules there shadow the rev1 checkout on sys.path unless
        # revision pinning is correctly enforced.
        self.start_master("-r", self.tmp_dir.name, "-g", cwd=self.tmp_dir.name)
        self.run_client("submit", exp_rel_path, "-R", "-r", rev1)
        while True:
            line = self.master.stdout.readline()
            self.assertNotIn("ERROR", line)
            if "imported:" in line:
                self.assertIn("imported: rev1 rev1", line)
                break
        self.run_client("terminate")
        self.assertEqual(self.master.wait(), 0)
        self.master.stdout.close()

    def tearDown(self):
        if self.master is not None and self.master.poll() is None:
            self.master.kill()
            self.master.wait()
            self.master.stdout.close()
        self.tmp_dir.cleanup()
        self.tmp_empty_dir.cleanup()
