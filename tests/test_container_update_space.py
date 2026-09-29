"""How much space a container update asks for, and what it leaves behind afterwards.

The check used to be a flat 6 GiB for staging and for Docker's image store, sized before the
images were slimmed; it refused a Raspberry Pi with 5.1 GiB free for an update whose arm64
archives total about 530 MB. And every successful update left the replaced release's
predecessors in place, so the space shrank with each release.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host import mdd_container_update as cu
from host import mdd_update

MB = 1000 * 1000
# The v1.13.0-rc1 arm64 archives.
RC1_ARM64 = {"mdd-sim-gateway-control-v1.13.0-rc1-arm64.tar.gz": 131 * MB,
             "mdd-sim-gateway-hardware-v1.13.0-rc1-arm64.tar.gz": 139 * MB,
             "mdd-sim-gateway-egress-v1.13.0-rc1-arm64.tar.gz": 82 * MB,
             "mdd-sim-gateway-engine-v1.13.0-rc1-arm64.tar.gz": 152 * MB}


class SpaceRequiredTests(unittest.TestCase):
    def test_it_follows_the_archives_of_this_release(self):
        staging, store = cu.space_required(RC1_ARM64, list(RC1_ARM64))
        archives = sum(RC1_ARM64.values())
        self.assertEqual(staging, archives + cu.STAGING_MARGIN)
        self.assertEqual(store, archives * (cu.IMAGE_EXPANSION + 1) + cu.IMAGE_STORE_MARGIN)
        # The Pi that was refused had 5.1 GiB free on the one filesystem both live on.
        self.assertLess(store, int(5.1 * cu.GIB))

    def test_unknown_sizes_fall_back_to_a_fixed_figure(self):
        names = list(RC1_ARM64)
        for sizes in ({}, {**RC1_ARM64, names[0]: 0}, {**RC1_ARM64, names[1]: "131"}):
            with self.subTest(sizes=sizes):
                self.assertEqual(cu.space_required(sizes, names),
                                 (cu.FALLBACK_REQUIRED, cu.FALLBACK_REQUIRED))
        self.assertLess(cu.FALLBACK_REQUIRED, 6 * cu.GIB)

    def _perform(self, project, sizes, staging_free, store_free=None):
        (project / "update").mkdir(parents=True, exist_ok=True)
        network = project / "network.json"
        with patch.object(cu, "find_compose", return_value=project / "compose.yaml"), \
                patch.object(cu, "host_arch", return_value="arm64"), \
                patch.object(cu.mdd_update, "read_network_config",
                             return_value={"asset_sizes": sizes}), \
                patch.object(cu.mdd_update, "validated_download_routes", return_value=[]), \
                patch.object(cu.shutil, "disk_usage",
                             return_value=SimpleNamespace(free=staging_free)), \
                patch.object(cu.docker, "from_env", return_value=Mock()), \
                patch.object(cu, "docker_root_free_bytes", return_value=store_free or 0):
            cu.perform(project, "1.13.0-rc1", "MddIdd/mdd-sim-gateway", network, Mock(phase="x"))

    def test_a_refusal_says_how_much_is_needed_and_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(mdd_update.UpdateError) as refused:
                self._perform(Path(tmp), RC1_ARM64, staging_free=100 * MB)
        self.assertIn("persistent disk space", str(refused.exception))
        self.assertRegex(str(refused.exception), r"needs \d+\.\d GiB, 0\.1 GiB free")

    def test_the_image_store_is_checked_with_the_same_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(mdd_update.UpdateError) as refused:
                self._perform(Path(tmp), RC1_ARM64, staging_free=5 * cu.GIB,
                              store_free=2 * cu.GIB)
        self.assertIn("Docker image-store space", str(refused.exception))
        self.assertIn("2.0 GiB free", str(refused.exception))


class PreviousReleaseTests(unittest.TestCase):
    def test_the_installed_release_images_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.assertEqual(cu.previous_image_ids(project), set())
            (project / "update").mkdir()
            (project / "update" / "installed-images.json").write_text(json.dumps({
                "version": "1.12.0", "images": {
                    "engine": {"image_id": "sha256:engine112"},
                    "control": {"image_id": "sha256:control112"},
                    "broken": "not a dict"}}))
            self.assertEqual(cu.previous_image_ids(project),
                             {"sha256:engine112", "sha256:control112"})


def image(image_id, *tags, managed=True):
    labels = {cu.MANAGED: "true"} if managed else {}
    return SimpleNamespace(id=image_id, tags=list(tags), attrs={"Config": {"Labels": labels}})


class PruneTests(unittest.TestCase):
    def client(self, images, running=()):
        client = Mock()
        client.images.list.return_value = images
        client.containers.list.return_value = [SimpleNamespace(image=SimpleNamespace(id=i))
                                               for i in running]
        return client

    def test_only_releases_older_than_the_rollback_go(self):
        repo = "ghcr.io/mddidd/mdd-sim-gateway"
        images = [
            image("new-engine", f"{repo}-engine:v1.13.0"),
            image("old-engine", f"{repo}-engine:v1.12.0"),         # the rollback
            image("rc-engine", f"{repo}-engine:v1.12.0-rc11"),     # superseded
            image("rc-control", "mdd-sim-gateway/control:v1.12.0-rc11", managed=False),
            image("host-engine", "mdd-sim-gateway/engine:latest",
                  "mdd-sim-gateway/engine-base:trusted"),           # host-install alias
            image("in-use", f"{repo}-hardware:v1.11.0"),             # a container uses it
            image("someone-else", "postgres:16", managed=False),      # not ours
        ]
        client = self.client(images, running=["in-use"])
        removed = cu.prune_superseded_images(client, {"new-engine", "old-engine", None})
        self.assertEqual(removed, 2)
        self.assertEqual([c.args[0] for c in client.images.remove.call_args_list],
                         ["rc-engine", "rc-control"])

    def test_a_failure_never_fails_the_update(self):
        client = self.client([image("rc", "mdd-sim-gateway/control:v1")])
        client.images.remove.side_effect = RuntimeError("image is being used")
        self.assertEqual(cu.prune_superseded_images(client, set()), 0)


if __name__ == "__main__":
    unittest.main()
