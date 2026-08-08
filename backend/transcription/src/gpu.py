"""
gpu.py
Helpers for releasing GPU memory back to the driver.

Both faster-whisper (CTranslate2) and pyannote (PyTorch) hold VRAM for as long
as the model object is alive. Dropping the reference is necessary but not
sufficient: CPython may not collect immediately, and PyTorch keeps freed blocks
in its own caching allocator. These helpers do both steps.
"""

from __future__ import annotations

import ctypes
import gc
import glob
import logging
import os

logger = logging.getLogger(__name__)

_cuda_usable: "bool | None" = None
_nvrtc_preloaded = False


def preload_nvrtc_builtins() -> None:
    """Make libnvrtc-builtins discoverable before any torch JIT compile happens.

    pyannote's speaker-embedding model JIT-compiles kernels through NVRTC. NVRTC
    dlopens its matching ``libnvrtc-builtins.so.<ver>`` by soname, which only
    resolves via the loader search path — and pip ships that file inside
    ``nvidia/cu13/lib`` where the loader never looks. The result is a hard
    ``nvrtc: error: failed to open libnvrtc-builtins.so.13.0`` at inference time.

    Setting LD_LIBRARY_PATH would fix it, but glibc snapshots that at process
    start, so it can only be done from the launcher. Loading the file here with
    RTLD_GLOBAL puts it in the process's namespace under its soname, so NVRTC's
    later dlopen resolves to the already-loaded object instead of searching disk.
    """
    global _nvrtc_preloaded
    if _nvrtc_preloaded:
        return
    _nvrtc_preloaded = True
    try:
        import nvidia
    except ImportError:
        return
    # `nvidia` is a namespace package, so use __path__ rather than __file__.
    candidates = [
        path
        for root in list(getattr(nvidia, "__path__", []))
        for path in glob.glob(os.path.join(root, "*", "lib", "libnvrtc-builtins.so.*"))
        if ".alt." not in path
    ]
    # Highest version last so it wins if several are present (a venv upgraded in
    # place can retain the older CUDA 12 copy alongside the CUDA 13 one).
    for path in sorted(candidates):
        try:
            ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            logger.debug("Preloaded %s", os.path.basename(path))
        except OSError:
            logger.debug("Could not preload %s", path, exc_info=True)


def release_cuda_memory() -> None:
    """Force a GC pass and return cached CUDA blocks to the driver.

    Safe to call when torch is missing or no GPU is present.
    """
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        logger.debug("CUDA cache release failed", exc_info=True)


def cuda_is_usable() -> bool:
    """True only if CUDA is present *and* can actually execute a kernel.

    ``torch.cuda.is_available()`` is not sufficient: a torch build without
    kernels for the installed GPU's compute capability still reports True, then
    fails at the first real op with "no kernel image is available for execution
    on the device". This runs a tiny kernel once and caches the answer.

    Note this only speaks for *torch*. faster-whisper/CTranslate2 ship their own
    CUDA kernels and can be fine on the GPU even when this returns False.
    """
    global _cuda_usable
    if _cuda_usable is not None:
        return _cuda_usable
    try:
        import torch
    except ImportError:
        _cuda_usable = False
        return False
    try:
        if not torch.cuda.is_available():
            _cuda_usable = False
            return False
        torch.zeros(8, device="cuda").add_(1).cpu()
        preload_nvrtc_builtins()
        _cuda_usable = True
    except Exception as exc:
        logger.warning(
            "CUDA reports available but cannot run kernels (%s) — using CPU. "
            "This usually means the torch build lacks kernels for this GPU's "
            "compute capability.", exc,
        )
        _cuda_usable = False
    return _cuda_usable


def cuda_memory_mb() -> float:
    """Return VRAM currently reserved by this process in MiB (0.0 if no GPU)."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.memory_reserved() / (1024 * 1024)
    except Exception:
        pass
    return 0.0
