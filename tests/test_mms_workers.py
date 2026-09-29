"""MMS pictures are decoded in worker processes, within a memory budget (mms_workers).

A machine with the memory decodes several at once and at full quality; a small one takes a
large picture alone, decodes a JPEG that would not fit at a smaller scale, and refuses the
rest with the reason -- and a decode that outgrows its estimate costs one picture, never the
control plane.
"""
from __future__ import annotations

import io
import os
import signal
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from control.app import mms, mms_convert, mms_workers
from tests.test_mms_convert import TINY_HEIC, TO, photo

MB = mms_workers.MB


def fit(attachments, limit=300 * 1024):
    return mms.fit_attachments(attachments, "hi", "", TO, {"max_size": limit})


class PoolTests(unittest.TestCase):
    def use(self, **kwargs) -> mms_workers.Pool:
        pool = mms_workers.Pool(**kwargs)
        old = mms_workers.replace_pool(pool)
        self.addCleanup(mms_workers.replace_pool, old)
        self.addCleanup(pool.close)
        # Each test starts from its own pictures, not ones a previous test left decoded.
        converter = mms_convert.CONVERTERS["image"]
        saved, fits = converter._bases.copy(), dict(mms._FIT_CACHE)
        converter._bases.clear()
        mms._FIT_CACHE.clear()

        def restore():
            converter._bases.clear()
            converter._bases.update(saved)
            mms._FIT_CACHE.clear()
            mms._FIT_CACHE.update(fits)
        self.addCleanup(restore)
        return pool

    def test_a_picture_is_decoded_in_a_worker_that_offers_itself_to_the_oom_killer(self):
        pool = self.use(workers=1, budget=1024 * MB)
        image = pool.decode(photo(800, 600), 1600, 1, 20 * MB)
        self.assertEqual(image.size, (800, 600))
        worker = pool._idle[0]
        self.assertNotEqual(worker.process.pid, os.getpid())
        with open(f"/proc/{worker.process.pid}/oom_score_adj") as handle:
            self.assertEqual(handle.read().strip(), "1000")

    def test_a_worker_that_dies_costs_that_picture_and_the_next_one_gets_a_new_worker(self):
        pool = self.use(workers=1, budget=1024 * MB)
        pool.decode(photo(400, 300), 1600, 1, 10 * MB)
        os.kill(pool._idle[0].process.pid, signal.SIGKILL)
        pool._idle[0].process.join(5)
        with self.assertRaises(mms_workers.WorkerDied):
            pool.decode(photo(400, 300), 1600, 1, 10 * MB)
        self.assertEqual(pool.decode(photo(400, 300), 1600, 1, 10 * MB).size, (400, 300))
        self.assertEqual(pool._alive, 1)

    def test_decodes_that_do_not_fit_together_take_turns(self):
        cost = 50 * MB
        pool = self.use(workers=4, budget=mms_workers.WORKER_BYTES * 2 + cost + cost // 2)
        peak, lock = [0], threading.Lock()
        admit = pool._admit

        def watched(*args):
            worker = admit(*args)
            with lock:
                peak[0] = max(peak[0], pool._busy)
            return worker

        with patch.object(pool, "_admit", side_effect=watched):
            threads = [threading.Thread(target=pool.decode,
                                        args=(photo(1200, 900), 1600, 1, cost))
                       for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual(peak[0], 1)

    def test_what_can_never_fit_is_refused_before_anything_starts(self):
        pool = self.use(workers=2, budget=mms_workers.WORKER_BYTES + 10 * MB)
        with self.assertRaises(mms_workers.OverBudget):
            pool.decode(photo(400, 300), 1600, 1, 11 * MB)
        self.assertEqual((pool._alive, pool._busy), (0, 0))

    def test_a_heic_too_large_for_this_gateway_is_refused_with_the_reason(self):
        self.use(workers=1, budget=mms_workers.WORKER_BYTES + 1 * MB)
        _fitted, problem, _summary = fit([{"name": "IMG_1.HEIC", "content_type": "image/heic",
                                           "data": TINY_HEIC}])
        self.assertIn("IMG_1.HEIC", problem)
        self.assertIn("MDD_MMS_CONVERT_MEMORY", problem)

    def test_a_jpeg_too_large_to_decode_whole_is_decoded_smaller_and_says_so(self):
        data = photo(3000, 2000, quality=95)
        info = mms_convert.probe(data)
        full = mms_convert.decode_cost(info, len(data), 1600)
        smaller = mms_convert.decode_cost(info, len(data), 1280)
        self.assertLess(smaller, full)
        self.use(workers=1, budget=mms_workers.WORKER_BYTES + smaller)
        fitted, problem, summary = fit([{"name": "p.jpg", "content_type": "image/jpeg",
                                         "data": data}], 600 * 1024)
        self.assertIsNone(problem)
        entry = summary["attachments"][0]
        self.assertTrue(entry["reduced"])
        self.assertLessEqual(max(Image.open(io.BytesIO(fitted[0]["data"])).size), 1280)

    def test_with_the_memory_a_picture_is_sent_at_full_quality(self):
        self.use(workers=2, budget=1024 * MB)
        fitted, problem, summary = fit([{"name": "p.jpg", "content_type": "image/jpeg",
                                         "data": photo(3000, 2000, quality=95)}], 600 * 1024)
        self.assertIsNone(problem)
        self.assertFalse(summary["attachments"][0]["reduced"])
        self.assertEqual(max(Image.open(io.BytesIO(fitted[0]["data"])).size), 1600)

    def test_idle_workers_leave_after_a_while(self):
        pool = self.use(workers=1, budget=1024 * MB)
        with patch.object(mms_workers, "IDLE_SECONDS", 0.2):
            pool.decode(photo(400, 300), 1600, 1, 10 * MB)
            process = pool._idle[0].process
            deadline = time.monotonic() + 10
            # The pool's own thread reaps the process; exitcode is set once it has.
            while (pool._idle or process.exitcode is None) and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual((pool._idle, pool._alive), ([], 0))
        self.assertIsNotNone(process.exitcode)


class SettingsTests(unittest.TestCase):
    def test_workers_and_memory_can_be_set(self):
        with patch.dict(os.environ, {"MDD_MMS_CONVERT_WORKERS": "3",
                                     "MDD_MMS_CONVERT_MEMORY": "200"}):
            pool = mms_workers.Pool()
        self.assertEqual((pool.workers, pool.budget()), (3, 200 * MB))

    def test_by_default_workers_follow_the_cpus_and_memory_the_limit(self):
        with patch.dict(os.environ, {"MDD_MMS_CONVERT_WORKERS": "", "MDD_MMS_CONVERT_MEMORY": ""}), \
                patch.object(mms_workers, "default_workers", return_value=6), \
                patch.object(mms_workers, "_memory_limit", return_value=(512 * MB, 130 * MB)):
            pool = mms_workers.Pool()
            self.assertEqual(pool.workers, 6)
            self.assertEqual(pool.budget(), 512 * MB - 130 * MB - mms_workers.RESERVE_BYTES)
        with patch.object(mms_workers, "_memory_limit", return_value=(None, 0)), \
                patch.object(mms_workers, "_available", return_value=4096 * MB):
            self.assertEqual(mms_workers.default_budget(), 2048 * MB)

    def test_a_cgroup_limit_is_read_with_its_reclaimable_cache_left_out(self):
        files = {"/proc/self/cgroup": "0::/system.slice/control.service\n",
                 "/sys/fs/cgroup/system.slice/control.service/memory.max": "536870912",
                 "/sys/fs/cgroup/system.slice/control.service/memory.current": "300000000",
                 "/sys/fs/cgroup/system.slice/control.service/memory.stat":
                     "anon 120000000\nfile 180000000\n",
                 "/sys/fs/cgroup/system.slice/memory.max": "max",
                 "/sys/fs/cgroup/memory.max": None}
        with patch.object(mms_workers, "_read", side_effect=lambda path: files.get(path)):
            self.assertEqual(mms_workers._memory_limit(), (536870912, 120000000))


    # Synology DSM mounts cgroup v1. Inside the control container /proc names the group as the
    # host sees it, while the container's own group is the root of each mount (as observed on a
    # DS1621+ with mem_limit 512m).
    DSM_CGROUP = ("9:cpu:/docker/abc\n6:cpuacct:/docker/abc\n4:memory:/docker/abc\n"
                  "1:name=systemd:/docker/abc\n")

    def test_a_cgroup_v1_limit_is_read_where_a_container_sees_it(self):
        files = {"/proc/self/cgroup": self.DSM_CGROUP,
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes": "536870912",
                 "/sys/fs/cgroup/memory/memory.usage_in_bytes": "143667200",
                 "/sys/fs/cgroup/memory/memory.stat":
                     "cache 39645184\nrss 94171136\ntotal_cache 39645184\n"}
        with patch.object(mms_workers, "_read", side_effect=lambda path: files.get(path)):
            self.assertEqual(mms_workers._memory_limit(), (536870912, 143667200 - 39645184))
            # Not half of what the host has free: the NAS this was seen on had 22 GB.
            with patch.object(mms_workers, "_available", return_value=22 * 1024 * MB):
                self.assertEqual(mms_workers.default_budget(),
                                 536870912 - (143667200 - 39645184) - mms_workers.RESERVE_BYTES)

    def test_a_cgroup_v1_service_is_limited_by_its_own_group_or_a_parent(self):
        files = {"/proc/self/cgroup": "5:memory:/system.slice/control.service\n",
                 "/sys/fs/cgroup/memory/system.slice/control.service/memory.limit_in_bytes":
                     "9223372036854771712",
                 "/sys/fs/cgroup/memory/system.slice/control.service/memory.usage_in_bytes":
                     "300000000",
                 "/sys/fs/cgroup/memory/system.slice/control.service/memory.stat":
                     "cache 100000000\ntotal_cache 180000000\n",
                 "/sys/fs/cgroup/memory/system.slice/memory.limit_in_bytes": "1073741824",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712"}
        with patch.object(mms_workers, "_read", side_effect=lambda path: files.get(path)):
            self.assertEqual(mms_workers._memory_limit(), (1073741824, 120000000))

    def test_an_unlimited_cgroup_v1_is_no_limit(self):
        files = {"/proc/self/cgroup": self.DSM_CGROUP,
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712",
                 "/sys/fs/cgroup/memory/memory.usage_in_bytes": "143667200"}
        with patch.object(mms_workers, "_read", side_effect=lambda path: files.get(path)):
            self.assertEqual(mms_workers._memory_limit(), (None, 0))

    def test_cgroup_v2_is_preferred_where_a_hybrid_host_has_both(self):
        files = {"/proc/self/cgroup": "4:memory:/docker/abc\n0::/docker/abc\n",
                 "/sys/fs/cgroup/docker/abc/memory.max": "268435456",
                 "/sys/fs/cgroup/docker/abc/memory.current": "100000000",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes": "536870912"}
        with patch.object(mms_workers, "_read", side_effect=lambda path: files.get(path)):
            self.assertEqual(mms_workers._memory_limit(), (268435456, 100000000))

    def test_a_cgroup_v1_cpu_quota_caps_the_workers(self):
        files = {"/proc/self/cgroup": self.DSM_CGROUP,
                 "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "150000",
                 "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"}
        with patch.object(mms_workers, "_read", side_effect=lambda path: files.get(path)), \
                patch.object(mms_workers.os, "sched_getaffinity", return_value=set(range(8)),
                             create=True):
            self.assertEqual(mms_workers.default_workers(), 2)
            files["/sys/fs/cgroup/cpu/cpu.cfs_quota_us"] = "-1"
            self.assertEqual(mms_workers.default_workers(), 8)
            # DSM's kernel has no CFS quota files at all.
            del files["/sys/fs/cgroup/cpu/cpu.cfs_quota_us"]
            self.assertEqual(mms_workers.default_workers(), 8)

if __name__ == "__main__":
    unittest.main()
