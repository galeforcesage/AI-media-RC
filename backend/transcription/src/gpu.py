"""
gpu.py
Helpers for releasing GPU memory back to the driver.

Both faster-whisper (CTranslate2) and pyannote (PyTorch) hold VRAM for as long
as the model object is alive. Dropping the reference is necessary but not
sufficient: CPython may not collect immediately, and PyTorch keeps freed blocks
in its own caching allocator. These helpers do both steps.
"""

from __future__ import annotations

import gc
import logging

logger = logging.getLogger(__name__)


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


def cuda_memory_mb() -> float:
    """Return VRAM currently reserved by this process in MiB (0.0 if no GPU)."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.memory_reserved() / (1024 * 1024)
    except Exception:
        pass
    return 0.0
