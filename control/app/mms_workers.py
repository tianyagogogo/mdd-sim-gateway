"""Decoding MMS pictures in worker processes, within a memory budget.

Decoding is the one step of a conversion whose memory grows with the camera: libheif decodes
a HEIC photo whole, about 8 bytes for every pixel, so a 48-megapixel one needs some 360 MB at
the peak however small the picture sent will be (mms_convert.decode). The control container
has 512 MB in all. Two things keep that from ever taking the control plane down:

* **A budget.** Each decode is admitted only when its cost, estimated from the header before a
  byte is decoded, fits beside what is already running. A machine with the memory for it
  decodes several pictures at once, up to MDD_MMS_CONVERT_WORKERS (default: the CPUs this
  process may use); a small one runs a large picture alone, the others waiting their turn.
  What cannot fit even alone never starts -- mms_convert then decodes a JPEG at a smaller
  scale, and refuses anything else with the reason.
* **Processes.** An estimate can be wrong. The decode runs in a worker process that volunteers
  to be the first thing the kernel kills when memory runs out, so the price of a wrong
  estimate is one picture, not every line's control plane.

The budget is MDD_MMS_CONVERT_MEMORY (MB) when set. Otherwise it is worked out whenever no
worker is running: the memory limit of this process's cgroup -- the container's, or the
systemd unit's, under cgroup v2 or the v1 that Synology DSM still mounts -- less what the
control plane already uses and a reserve; with no limit, half of what the host has available.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import threading
import time

log = logging.getLogger("mdd.mms")

MB = 1024 * 1024
# What decoding costs per pixel decoded, peak over the worker's own use, measured on the test
# gateway: libheif 7.7-8.0 bytes for iPhone-style grid HEIC from 12 to 48 megapixels (93, 182
# and 356 MB); a JPEG decoded at reduced scale and then shrunk, under 10 for the pixels it
# actually decodes. Pillow's own decoders (PNG, WebP, BMP, GIF, AVIF) are not measured and get
# the higher figure: 4 bytes a pixel at most, plus the conversion to RGB.
BYTES_PER_PIXEL = {"HEIF": 8}
DEFAULT_BYTES_PER_PIXEL = 10
# A worker with Python, Pillow and pi-heif loaded, before it decodes anything (about 20 MB
# measured; the file it is sent is counted with the decode).
WORKER_BYTES = 32 * MB
# Left for the control plane itself when the budget comes from its memory limit.
RESERVE_BYTES = 64 * MB
# A worker nobody has used for this long exits; the next picture starts a new one.
IDLE_SECONDS = 60
# A decode that takes longer than this is abandoned and its worker killed.
DECODE_TIMEOUT = 120
# cgroup v1 has no "max": an unlimited group reports the largest page-aligned count the kernel
# can hold, just under 2**63. Anything this large is no limit.
V1_UNLIMITED = 1 << 62


class OverBudget(Exception):
    """A decode that would not fit in the budget even with nothing else running."""

    def __init__(self, cost: int, budget: int):
        super().__init__(f"needs about {cost // MB} MB, and {budget // MB} MB is available")
        self.cost, self.budget = cost, budget


class WorkerDied(Exception):
    """The worker process ended while decoding: out of memory, most likely."""


def _env_int(name: str) -> int | None:
    value = (os.environ.get(name) or "").strip()
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        log.warning("%s=%r is not a number; using the default", name, value)
        return None


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _cgroup_dirs() -> list[str]:
    """This process's cgroup (v2) directory and its parents, innermost first."""
    relative = None
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        if line.startswith("0::"):
            relative = line[3:]
    if relative is None:
        return []
    path = os.path.normpath("/sys/fs/cgroup/" + relative.lstrip("/"))
    dirs = []
    while path.startswith("/sys/fs/cgroup"):
        dirs.append(path)
        if path == "/sys/fs/cgroup":
            break
        path = os.path.dirname(path)
    return dirs


def _cgroup_v1_dirs(controller: str) -> list[str]:
    """This process's cgroup (v1) directory for one controller and its parents, innermost first.

    cgroup v1 -- still what Synology DSM mounts -- has one hierarchy per controller, and
    /proc/self/cgroup names the group as "N:memory:/docker/<id>" rather than with "0::". A
    container usually sees its own group at the root of the mount, so the directory /proc names
    may not exist inside it; the walk up reaches the mount root either way, and a directory
    without the file asked for is skipped by the caller."""
    relative = None
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        _hierarchy, _colon, rest = line.partition(":")
        controllers, _colon, path = rest.partition(":")
        if controller in controllers.split(","):
            relative = path
    if relative is None:
        return []
    root = "/sys/fs/cgroup/" + controller
    path = os.path.normpath(root + "/" + relative.lstrip("/"))
    dirs = []
    while path == root or path.startswith(root + "/"):
        dirs.append(path)
        if path == root:
            break
        path = os.path.dirname(path)
    return dirs


def _used(path: str, usage_file: str, cache_keys: tuple[str, ...]) -> int:
    """What a cgroup uses, less the page cache the kernel can reclaim."""
    used = int(_read(os.path.join(path, usage_file)) or 0)
    stat = {}
    for line in (_read(os.path.join(path, "memory.stat")) or "").splitlines():
        key, _space, value = line.partition(" ")
        stat[key] = value
    for key in cache_keys:
        if key in stat:
            used -= int(stat[key])
            break
    return max(0, used)


def _memory_limit() -> tuple[int | None, int]:
    """(the tightest memory limit over this process's cgroups, what the innermost uses apart
    from page cache the kernel can reclaim); (None, 0) without a limit."""
    dirs = _cgroup_dirs()
    limits = []
    for path in dirs:
        value = _read(os.path.join(path, "memory.max"))
        if value and value != "max":
            limits.append(int(value))
    if limits:
        return min(limits), _used(dirs[0], "memory.current", ("file",))
    # No v2 memory controller: a v1 host, or a hybrid one whose unified mount has no controllers.
    limits, innermost = [], None
    for path in _cgroup_v1_dirs("memory"):
        value = _read(os.path.join(path, "memory.limit_in_bytes"))
        if value is None:
            continue
        innermost = innermost or path
        if int(value) < V1_UNLIMITED:
            limits.append(int(value))
    if not limits:
        return None, 0
    return min(limits), _used(innermost, "memory.usage_in_bytes", ("total_cache", "cache"))


def _available() -> int:
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def default_budget() -> int:
    limit, used = _memory_limit()
    if limit is not None:
        return max(0, limit - used - RESERVE_BYTES)
    return _available() // 2


def default_workers() -> int:
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    for path in _cgroup_dirs():
        quota, _space, period = (_read(os.path.join(path, "cpu.max")) or "max").partition(" ")
        if quota != "max" and period:
            count = min(count, max(1, -(-int(quota) // int(period))))
    # v1: -1 is no quota. DSM's kernel has no CFS quota at all, so there the files are absent.
    for path in _cgroup_v1_dirs("cpu"):
        quota = _read(os.path.join(path, "cpu.cfs_quota_us"))
        period = _read(os.path.join(path, "cpu.cfs_period_us"))
        if quota and period and int(quota) > 0 and int(period) > 0:
            count = min(count, max(1, -(-int(quota) // int(period))))
    return max(1, count)


def _serve(connection) -> None:
    """A worker: decode what it is sent until the parent closes the pipe."""
    try:
        # First in line for the OOM killer: a decode that outgrows its estimate ends here.
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")
    except OSError:
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from . import mms_convert
    while True:
        try:
            data, longest, orientation = connection.recv()
        except (EOFError, OSError):
            return
        try:
            image = mms_convert.decode(data, longest, orientation)
            reply = ("ok", image.size, image.tobytes())
            del image
        except mms_convert.ConversionError as exc:
            reply = ("error", str(exc), None)
        except MemoryError:
            reply = ("memory", "", None)
        del data
        try:
            connection.send(reply)
        except (EOFError, OSError):
            return


class _Worker:
    def __init__(self, context):
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_serve, args=(child,), daemon=True,
                                       name="mms-decode")
        self.process.start()
        child.close()
        self.used = time.monotonic()

    def close(self) -> None:
        try:
            self.connection.close()
        except OSError:
            pass
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)


class Pool:
    def __init__(self, workers: int | None = None, budget: int | None = None):
        self.workers = workers or _env_int("MDD_MMS_CONVERT_WORKERS") or default_workers()
        configured = _env_int("MDD_MMS_CONVERT_MEMORY")
        self._fixed_budget = budget if budget is not None else (
            configured * MB if configured is not None else None)
        self._budget: int | None = None
        # "spawn": the control plane has threads, and a forked copy of a threaded process can
        # inherit a lock some other thread held.
        self._context = multiprocessing.get_context("spawn")
        self._cond = threading.Condition()
        self._idle: list[_Worker] = []
        self._alive = 0                 # workers started and not yet closed
        self._busy = 0                  # decodes in flight
        self._in_flight = 0             # their estimated cost
        self._reaper: threading.Timer | None = None

    def budget(self) -> int:
        """The memory decoding may use, in bytes. Worked out afresh only while nothing is
        running, so the measurement never counts a worker as the control plane's own."""
        if self._fixed_budget is not None:
            return self._fixed_budget
        with self._cond:
            if self._budget is None or (self._alive == 0 and self._busy == 0):
                self._budget = default_budget()
            return self._budget

    def fits_alone(self, cost: int) -> bool:
        return cost + WORKER_BYTES <= self.budget()

    def decode(self, data: bytes, longest: int, orientation: int, cost: int):
        """mms_convert.decode(data, longest, orientation) in a worker, once `cost` bytes fit
        beside what is already running. Raises OverBudget when they never can, WorkerDied
        when the worker does not survive it, ConversionError as decode() does."""
        from PIL import Image

        from . import mms_convert
        total = self.budget()
        if cost + WORKER_BYTES > total:
            raise OverBudget(cost + WORKER_BYTES, total)
        worker = self._admit(cost, total)
        died = False
        try:
            if worker is None:
                worker = _Worker(self._context)
            worker.connection.send((data, longest, orientation))
            if not worker.connection.poll(DECODE_TIMEOUT):
                died = True
                raise mms_convert.ConversionError("converting the picture took too long")
            status, detail, raw = worker.connection.recv()
        except (EOFError, OSError, BrokenPipeError):
            died = True
            raise WorkerDied() from None
        finally:
            self._release(worker, cost, died)
        if status == "ok":
            return Image.frombytes("RGB", tuple(detail), raw)
        if status == "memory":
            raise WorkerDied()
        raise mms_convert.ConversionError(detail)

    def _admit(self, cost: int, total: int) -> _Worker | None:
        """Wait until the decode may run; an idle worker to run it on, or None to start one."""
        with self._cond:
            while True:
                charged = self._alive * WORKER_BYTES + self._in_flight
                if self._idle and charged + cost <= total:
                    worker = self._idle.pop()
                    break
                if not self._idle and self._alive < self.workers \
                        and charged + WORKER_BYTES + cost <= total:
                    worker = None
                    self._alive += 1
                    break
                if len(self._idle) > 1 or (self._idle and self._busy == 0 and
                                           self._alive >= self.workers):
                    # Idle workers hold memory this decode needs; let one go.
                    self._retire(self._idle.pop(0))
                    continue
                self._cond.wait()
            self._busy += 1
            self._in_flight += cost
            return worker

    def _release(self, worker: _Worker | None, cost: int, died: bool) -> None:
        with self._cond:
            self._busy -= 1
            self._in_flight -= cost
            if worker is None:
                self._alive -= 1        # it never started
            elif died or not worker.process.is_alive():
                self._retire(worker)
            else:
                worker.used = time.monotonic()
                self._idle.append(worker)
                self._schedule_reap()
            self._cond.notify_all()

    def _retire(self, worker: _Worker) -> None:
        """Close a worker; called with the lock held."""
        self._alive -= 1
        threading.Thread(target=worker.close, daemon=True).start()

    def _schedule_reap(self) -> None:
        if self._reaper is None:
            self._reaper = threading.Timer(IDLE_SECONDS, self._reap)
            self._reaper.daemon = True
            self._reaper.start()

    def _reap(self) -> None:
        with self._cond:
            self._reaper = None
            now = time.monotonic()
            for worker in [w for w in self._idle if now - w.used >= IDLE_SECONDS]:
                self._idle.remove(worker)
                self._retire(worker)
            if self._idle:
                self._schedule_reap()
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            while self._idle:
                self._retire(self._idle.pop())
            if self._reaper is not None:
                self._reaper.cancel()
                self._reaper = None


_pool: Pool | None = None
_pool_lock = threading.Lock()


def pool() -> Pool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = Pool()
            log.info("MMS picture conversion: up to %d at once within %d MB",
                     _pool.workers, _pool.budget() // MB)
        return _pool


def replace_pool(new: Pool | None) -> Pool | None:
    """Swap the pool (tests, or a changed setting); the old one's idle workers are closed."""
    global _pool
    with _pool_lock:
        old, _pool = _pool, new
    if old is not None:
        old.close()
    return old
