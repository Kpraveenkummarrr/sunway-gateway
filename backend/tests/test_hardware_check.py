"""The Sarvam-M go/no-go check.

These test the verdict logic against fabricated hardware reports, not the
actual machine running the tests — a laptop and a production server should
get different verdicts from the same rules, and the rules are what is under
test. `detect_hardware()` itself is exercised once, only for "does not
crash and returns a well-formed report", since its actual numbers depend on
whatever machine runs the suite.
"""

from app.services.hardware_check import (
    SARVAM_M_Q4,
    GPUInfo,
    HardwareReport,
    ModelRequirement,
    assess,
    detect_hardware,
    format_report,
)

TINY_MODEL = ModelRequirement(name="tiny-test-model", quantized_size_gb=1.0)


def _report(**overrides) -> HardwareReport:
    base = dict(
        os_name="Linux 5.15",
        cpu_name="Generic CPU",
        cpu_cores=8,
        cpu_threads=8,
        total_ram_mb=32 * 1024,
        available_ram_mb=28 * 1024,
        free_disk_mb=200 * 1024,
        gpus=[],
    )
    base.update(overrides)
    return HardwareReport(**base)


def test_detect_hardware_runs_on_whatever_machine_is_actually_here() -> None:
    report = detect_hardware()
    assert report.cpu_threads >= 1
    assert report.total_ram_mb >= 0
    assert report.free_disk_mb >= 0
    assert isinstance(report.gpus, list)


# ---- verdict logic ----


def test_this_dev_laptop_class_machine_is_unsupported_for_sarvam_m() -> None:
    """The exact profile measured on the development machine this feature
    was built on: 4 threads, ~8 GB RAM with almost none free, <1 GB disk,
    integrated graphics. This must never be reported as runnable."""
    report = _report(
        cpu_threads=4,
        total_ram_mb=int(7.8 * 1024),
        available_ram_mb=512,
        free_disk_mb=700,
        gpus=[GPUInfo(name="Intel UHD Graphics", vram_mb=1024)],
    )
    verdict = assess(report, SARVAM_M_Q4)
    assert verdict.verdict == "UNSUPPORTED HARDWARE"
    assert not verdict.supported
    assert verdict.recommended_backend is None
    assert any("disk" in r for r in verdict.reasons)
    assert any("RAM" in r for r in verdict.reasons)


def test_a_workstation_gpu_with_headroom_is_supported() -> None:
    report = _report(
        cpu_threads=16,
        total_ram_mb=64 * 1024,
        available_ram_mb=48 * 1024,
        free_disk_mb=500 * 1024,
        gpus=[GPUInfo(name="NVIDIA RTX 4090", vram_mb=24 * 1024)],
    )
    verdict = assess(report, SARVAM_M_Q4)
    assert verdict.verdict == "SUPPORTED"
    assert verdict.supported
    assert "GPU" in verdict.recommended_backend
    assert verdict.recommended_gpu_layers == -1


def test_a_cpu_only_server_with_plenty_of_ram_is_marginal_not_supported() -> None:
    """RAM and disk are enough, but no GPU: it will load and can be tried,
    but latency is not promised — MARGINAL, not SUPPORTED."""
    report = _report(
        cpu_threads=32,
        total_ram_mb=64 * 1024,
        available_ram_mb=48 * 1024,
        free_disk_mb=200 * 1024,
        gpus=[],
    )
    verdict = assess(report, SARVAM_M_Q4)
    assert verdict.verdict == "MARGINAL"
    assert verdict.supported
    assert verdict.recommended_backend == "llama.cpp, CPU only"


def test_a_small_gpu_that_cannot_fully_offload_is_reported_honestly() -> None:
    report = _report(
        cpu_threads=16,
        total_ram_mb=32 * 1024,
        available_ram_mb=24 * 1024,
        free_disk_mb=200 * 1024,
        gpus=[GPUInfo(name="NVIDIA RTX 3060", vram_mb=8 * 1024)],
    )
    verdict = assess(report, SARVAM_M_Q4)
    assert "partial GPU offload" in verdict.recommended_backend
    assert any("VRAM" in r for r in verdict.reasons)


def test_insufficient_disk_alone_is_unsupported_even_with_a_great_gpu() -> None:
    report = _report(
        free_disk_mb=5 * 1024,
        gpus=[GPUInfo(name="NVIDIA RTX 4090", vram_mb=24 * 1024)],
    )
    verdict = assess(report, SARVAM_M_Q4)
    assert verdict.verdict == "UNSUPPORTED HARDWARE"
    assert any("disk" in r for r in verdict.reasons)


def test_gpu_with_unknown_vram_falls_back_to_cpu_only_instead_of_guessing() -> None:
    report = _report(gpus=[GPUInfo(name="Mystery GPU", vram_mb=None)])
    verdict = assess(report, SARVAM_M_Q4)
    assert verdict.recommended_backend in (None, "llama.cpp, CPU only")


def test_the_verdict_scales_with_the_actual_model_size_not_a_fixed_number() -> None:
    """A smaller model on the same modest hardware can be supported — the
    check is not just "no GPU means always unsupported"."""
    modest = _report(cpu_threads=8, total_ram_mb=16 * 1024, available_ram_mb=10 * 1024, free_disk_mb=50 * 1024)
    assert assess(modest, SARVAM_M_Q4).verdict == "UNSUPPORTED HARDWARE"
    assert assess(modest, TINY_MODEL).supported


def test_format_report_never_raises_and_includes_the_verdict() -> None:
    report = _report(gpus=[GPUInfo(name="Test GPU", vram_mb=8192)])
    verdict = assess(report, SARVAM_M_Q4)
    text = format_report(report, verdict, SARVAM_M_Q4)
    assert verdict.verdict in text
    assert "Reasons:" in text
