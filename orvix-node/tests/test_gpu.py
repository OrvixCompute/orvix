"""Tests for GPU detection: stub mode and graceful handling of missing pynvml."""

import importlib

import pytest


@pytest.fixture
def fresh_gpu(monkeypatch):
    """Reload the gpu module so module-level pynvml-availability is re-evaluated."""

    def _load(stub: bool, pynvml_available: bool = False):
        monkeypatch.setenv("ORVIX_NODE_STUB_GPU", "true" if stub else "false")
        import orvix_node.gpu as gpu_mod

        importlib.reload(gpu_mod)
        monkeypatch.setattr(gpu_mod, "_PYNVML_AVAILABLE", pynvml_available)
        return gpu_mod

    return _load


def test_stub_detect_returns_fake(fresh_gpu):
    gpu = fresh_gpu(stub=True)
    det = gpu.GPUDetector()
    info = det.detect()
    assert info is not None
    assert info.model == "STUB RTX 4090"
    assert info.vram_total_mb == 24576
    assert det.is_available() is True


def test_stub_metrics_vary_and_health(fresh_gpu):
    gpu = fresh_gpu(stub=True)
    det = gpu.GPUDetector()
    m = det.get_metrics()
    assert 0 <= m.gpu_util_pct <= 100
    assert m.memory_total_mb == 24576
    health = det.health_check()
    assert health["status"] == "ok"
    assert health["primary_gpu"] == "STUB RTX 4090"
    assert "stub mode" in health["issues"]


def test_missing_pynvml_handled_gracefully(fresh_gpu):
    gpu = fresh_gpu(stub=False, pynvml_available=False)
    det = gpu.GPUDetector()
    # No raise; returns None / unavailable.
    assert det.detect() is None
    assert det.is_available() is False
    health = det.health_check()
    assert health["status"] == "unavailable"
    assert health["gpu_count"] == 0


def test_mocked_pynvml_detection(fresh_gpu, monkeypatch):
    """Feed a fake pynvml module to exercise the real detection path."""
    gpu = fresh_gpu(stub=False, pynvml_available=True)

    class FakePynvml:
        NVML_TEMPERATURE_GPU = 0

        def nvmlInit(self): ...
        def nvmlDeviceGetCount(self): return 1
        def nvmlDeviceGetHandleByIndex(self, i): return object()
        def nvmlDeviceGetName(self, h): return "RTX 2000 Ada"
        def nvmlDeviceGetMemoryInfo(self, h):
            class M:
                total = 16 * 1024 * 1024 * 1024
                used = 4 * 1024 * 1024 * 1024
            return M()
        def nvmlSystemGetCudaDriverVersion_v2(self): return 12040
        def nvmlDeviceGetCudaComputeCapability(self, h): return (8, 9)
        def nvmlDeviceGetPciInfo(self, h):
            class P:
                busId = b"0000:01:00.0"
            return P()
        def nvmlSystemGetDriverVersion(self): return b"550.00"
        def nvmlDeviceGetUtilizationRates(self, h):
            class U:
                gpu = 42
                memory = 25
            return U()
        def nvmlDeviceGetTemperature(self, h, s): return 55
        def nvmlDeviceGetPowerUsage(self, h): return 120000

    monkeypatch.setattr(gpu, "pynvml", FakePynvml())
    det = gpu.GPUDetector()
    info = det.detect()
    assert info is not None
    assert info.model == "RTX 2000 Ada"
    assert info.vram_total_mb == 16384
    assert info.compute_capability == "8.9"

    metrics = det.get_metrics()
    assert metrics.gpu_util_pct == 42
    assert metrics.temperature_c == 55
    assert metrics.power_draw_w == 120
