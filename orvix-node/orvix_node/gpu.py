"""GPU detection via pynvml, with a stub mode for development without a GPU.

Set ORVIX_NODE_STUB_GPU=true to get plausible fake data on any machine. pynvml
is an optional dependency — its absence is handled gracefully (returns None /
unavailable rather than raising), unless stub mode is on.
"""

import os
from datetime import datetime, timezone

from orvix_node.logger import logger
from orvix_node.protocol import GPUInfo, GPUMetrics

# pynvml is optional. Import lazily and tolerate its absence.
try:
    import pynvml  # type: ignore

    _PYNVML_AVAILABLE = True
except Exception:  # noqa: BLE001
    pynvml = None  # type: ignore
    _PYNVML_AVAILABLE = False


def _stub_enabled() -> bool:
    return os.environ.get("ORVIX_NODE_STUB_GPU", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class GPUDetector:
    """Detects the primary GPU and reports real-time metrics."""

    def __init__(self) -> None:
        self._nvml_inited = False
        self._warned_stub = False
        if _stub_enabled() and not self._warned_stub:
            logger.warning("GPU stub mode is ENABLED — for development only.")
            self._warned_stub = True

    # --- nvml lifecycle ----------------------------------------------------
    def _ensure_nvml(self) -> bool:
        if not _PYNVML_AVAILABLE:
            return False
        if self._nvml_inited:
            return True
        try:
            pynvml.nvmlInit()
            self._nvml_inited = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("pynvml init failed: {}", exc)
            return False

    # --- public API --------------------------------------------------------
    def detect(self) -> GPUInfo | None:
        if _stub_enabled():
            return GPUInfo(
                vendor="nvidia",
                model="STUB RTX 4090",
                vram_total_mb=24576,
                cuda_version="12.4",
                driver_version="550.00",
                compute_capability="8.9",
                pci_bus_id="0000:00:00.0",
            )

        if not self._ensure_nvml():
            if not _PYNVML_AVAILABLE:
                logger.warning(
                    "pynvml not installed and stub mode off — no GPU detected. "
                    "Install with `pip install orvix-node[gpu]` or set ORVIX_NODE_STUB_GPU=true."
                )
            return None

        try:
            count = pynvml.nvmlDeviceGetCount()
            if count == 0:
                return None
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

            try:
                cuda_ver = pynvml.nvmlSystemGetCudaDriverVersion_v2()
                cuda_str = f"{cuda_ver // 1000}.{(cuda_ver % 1000) // 10}"
            except Exception:  # noqa: BLE001
                cuda_str = None
            try:
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                cc = f"{major}.{minor}"
            except Exception:  # noqa: BLE001
                cc = None
            try:
                pci = _decode(pynvml.nvmlDeviceGetPciInfo(handle).busId)
            except Exception:  # noqa: BLE001
                pci = None

            return GPUInfo(
                vendor="nvidia",
                model=_decode(pynvml.nvmlDeviceGetName(handle)),
                vram_total_mb=int(mem.total // (1024 * 1024)),
                cuda_version=cuda_str,
                driver_version=_decode(pynvml.nvmlSystemGetDriverVersion()),
                compute_capability=cc,
                pci_bus_id=pci,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GPU detection error: {}", exc)
            return None

    def is_available(self) -> bool:
        if _stub_enabled():
            return True
        if not self._ensure_nvml():
            return False
        try:
            return pynvml.nvmlDeviceGetCount() > 0
        except Exception:  # noqa: BLE001
            return False

    def get_metrics(self) -> GPUMetrics:
        if _stub_enabled():
            # Plausible-looking values that vary over time.
            import random

            used = random.randint(4000, 20000)
            util = random.randint(5, 95)
            return GPUMetrics(
                gpu_util_pct=util,
                memory_used_mb=used,
                memory_total_mb=24576,
                memory_util_pct=int(used / 24576 * 100),
                temperature_c=random.randint(40, 75),
                power_draw_w=random.randint(60, 320),
                timestamp=datetime.now(timezone.utc),
            )

        if not self._ensure_nvml():
            return GPUMetrics(memory_total_mb=0)

        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            total_mb = int(mem.total // (1024 * 1024))
            used_mb = int(mem.used // (1024 * 1024))
            try:
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:  # noqa: BLE001
                temp = None
            try:
                power = int(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000)
            except Exception:  # noqa: BLE001
                power = None
            return GPUMetrics(
                gpu_util_pct=int(util.gpu),
                memory_used_mb=used_mb,
                memory_total_mb=total_mb,
                memory_util_pct=int(used_mb / total_mb * 100) if total_mb else 0,
                temperature_c=temp,
                power_draw_w=power,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GPU metrics error: {}", exc)
            return GPUMetrics(memory_total_mb=0)

    def health_check(self) -> dict:
        if _stub_enabled():
            return {
                "status": "ok",
                "gpu_count": 1,
                "primary_gpu": "STUB RTX 4090",
                "issues": ["stub mode"],
            }

        if not _PYNVML_AVAILABLE:
            return {
                "status": "unavailable",
                "gpu_count": 0,
                "primary_gpu": None,
                "issues": ["pynvml not installed"],
            }
        if not self._ensure_nvml():
            return {
                "status": "error",
                "gpu_count": 0,
                "primary_gpu": None,
                "issues": ["pynvml init failed"],
            }
        try:
            count = pynvml.nvmlDeviceGetCount()
            if count == 0:
                return {
                    "status": "unavailable",
                    "gpu_count": 0,
                    "primary_gpu": None,
                    "issues": ["no CUDA GPU found"],
                }
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            return {
                "status": "ok",
                "gpu_count": count,
                "primary_gpu": _decode(pynvml.nvmlDeviceGetName(handle)),
                "issues": [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "gpu_count": 0,
                "primary_gpu": None,
                "issues": [str(exc)],
            }


# Process-wide singleton.
detector = GPUDetector()
