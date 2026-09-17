"""Whether this machine can realistically run a local LLM.

Sarvam-M (sarvamai/sarvam-m) is a 24B-parameter model fine-tuned from
Mistral-Small-24B-Instruct — not a small model. Loading it, even quantized,
has real memory and disk requirements, and running it on CPU-only hardware
can be too slow for a live phone call to wait on. The client asked whether
it could replace a paid API "on the PC" — the honest answer depends entirely
on which PC, so this measures the machine it actually runs on and says
SUPPORTED / MARGINAL / UNSUPPORTED HARDWARE with the reasons, instead of
attempting to download and load a multi-gigabyte model blindly.

Cross-platform on purpose: the production box is Ubuntu, this may be
checked from a developer's Windows machine first, and the client's PC is
unknown until it is actually measured.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GPUInfo:
    name: str
    vram_mb: int | None  # None when a GPU was found but VRAM could not be read


@dataclass(frozen=True)
class HardwareReport:
    os_name: str
    cpu_name: str
    cpu_cores: int
    cpu_threads: int
    total_ram_mb: int
    available_ram_mb: int
    free_disk_mb: int
    gpus: list[GPUInfo] = field(default_factory=list)

    @property
    def has_gpu(self) -> bool:
        return len(self.gpus) > 0

    @property
    def best_gpu_vram_mb(self) -> int:
        known = [g.vram_mb for g in self.gpus if g.vram_mb]
        return max(known) if known else 0


@dataclass(frozen=True)
class ModelRequirement:
    """What a model needs, derived from its on-disk (quantized) size, not
    guessed at — the model file size IS the memory footprint for GGUF/llama.cpp
    style inference, plus headroom for context and the OS."""

    name: str
    quantized_size_gb: float
    minimum_context_ram_gb: float = 2.0  # KV cache + runtime overhead, rule of thumb
    os_headroom_gb: float = 2.0  # leave the OS and the rest of the app usable

    @property
    def cpu_only_ram_gb(self) -> float:
        return self.quantized_size_gb + self.minimum_context_ram_gb + self.os_headroom_gb

    @property
    def disk_gb_needed(self) -> float:
        return self.quantized_size_gb * 1.15  # the download plus working room


# sarvamai/sarvam-m is documented as a 24B-parameter model built on
# Mistral-Small-24B-Instruct-2501. A 4-bit (Q4_K_M) GGUF quantization of a
# 24B model is approximately 14-15 GB on disk — that figure, not the
# unquantized ~47 GB fp16 size, is the realistic "local PC" footprint, and
# is what these thresholds are based on. If the operator uses a different
# quantization the actual file size should be checked against
# ModelRequirement.quantized_size_gb below rather than trusting this default.
SARVAM_M_Q4 = ModelRequirement(name="sarvam-m (Q4_K_M GGUF, ~24B params)", quantized_size_gb=15.0)


@dataclass(frozen=True)
class Verdict:
    verdict: str  # "SUPPORTED" | "MARGINAL" | "UNSUPPORTED HARDWARE"
    reasons: list[str]
    recommended_backend: str | None
    recommended_gpu_layers: int

    @property
    def supported(self) -> bool:
        return self.verdict in ("SUPPORTED", "MARGINAL")


def _read_linux_meminfo() -> tuple[int, int] | None:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return None
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if match:
            values[match.group(1)] = int(match.group(2))
    if "MemTotal" not in values:
        return None
    total_kb = values["MemTotal"]
    # MemAvailable (kernel-estimated usable memory) is what matters for "can
    # I load a 15 GB model right now", not raw MemFree.
    available_kb = values.get("MemAvailable", values.get("MemFree", 0))
    return total_kb // 1024, available_kb // 1024


def _read_windows_meminfo() -> tuple[int, int] | None:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return None
        return stat.ullTotalPhys // (1024 * 1024), stat.ullAvailPhys // (1024 * 1024)
    except Exception:  # noqa: BLE001 - best-effort, falls through to "unknown"
        return None


def _cpu_name() -> str:
    if platform.system() == "Linux":
        path = Path("/proc/cpuinfo")
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine() or "unknown CPU"


def _detect_nvidia_gpus() -> list[GPUInfo]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        result = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    gpus: list[GPUInfo] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        vram_mb = None
        if len(parts) > 1:
            try:
                vram_mb = int(float(parts[1]))
            except ValueError:
                vram_mb = None
        gpus.append(GPUInfo(name=parts[0], vram_mb=vram_mb))
    return gpus


def _detect_windows_gpus() -> list[GPUInfo]:
    """Fallback when there is no NVIDIA GPU (nvidia-smi absent): reports what
    Windows knows, which for integrated graphics is not real dedicated VRAM
    and is reported as such rather than silently omitted."""
    exe = shutil.which("wmic")
    if not exe:
        return []
    try:
        result = subprocess.run(
            [exe, "path", "win32_VideoController", "get", "Name,AdapterRAM", "/format:csv"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    gpus: list[GPUInfo] = []
    for line in result.stdout.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 3 or cells[0] == "Node":
            continue
        _, adapter_ram, name = cells[0], cells[1], cells[2]
        if not name:
            continue
        vram_mb = None
        if adapter_ram.isdigit():
            vram_mb = int(adapter_ram) // (1024 * 1024)
        gpus.append(GPUInfo(name=name, vram_mb=vram_mb))
    return gpus


def detect_hardware(*, path_for_disk: Path | None = None) -> HardwareReport:
    """Measures the machine this process is actually running on. No
    assumptions, no cached/hardcoded numbers — every field here comes from
    an OS query made just now."""
    os_name = f"{platform.system()} {platform.release()}"
    cpu_threads = 1
    try:
        import os as _os

        cpu_threads = _os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        pass

    mem = _read_linux_meminfo() if platform.system() == "Linux" else _read_windows_meminfo()
    total_ram_mb, available_ram_mb = mem if mem is not None else (0, 0)

    disk_path = path_for_disk or Path.cwd()
    try:
        free_disk_mb = shutil.disk_usage(disk_path).free // (1024 * 1024)
    except OSError:
        free_disk_mb = 0

    gpus = _detect_nvidia_gpus()
    if not gpus and platform.system() == "Windows":
        gpus = _detect_windows_gpus()

    return HardwareReport(
        os_name=os_name,
        cpu_name=_cpu_name(),
        cpu_cores=cpu_threads,  # logical cores; physical-core detection needs psutil, which isn't a dependency here
        cpu_threads=cpu_threads,
        total_ram_mb=total_ram_mb,
        available_ram_mb=available_ram_mb,
        free_disk_mb=free_disk_mb,
        gpus=gpus,
    )


# A GPU is only useful for full offload if it clears the model's quantized
# size with headroom for the KV cache; short of that, partial offload still
# helps somewhat but the CPU-only verdict below is the safe one to report.
_GPU_OFFLOAD_HEADROOM_GB = 2.0


def assess(report: HardwareReport, requirement: ModelRequirement = SARVAM_M_Q4) -> Verdict:
    """Turns a hardware report into SUPPORTED / MARGINAL / UNSUPPORTED
    HARDWARE for `requirement`, with the reasons spelled out — this is what
    gets shown to an operator deciding whether to even attempt a download."""
    reasons: list[str] = []
    total_ram_gb = report.total_ram_mb / 1024
    available_ram_gb = report.available_ram_mb / 1024
    free_disk_gb = report.free_disk_mb / 1024
    needed_disk_gb = requirement.disk_gb_needed

    if free_disk_gb < needed_disk_gb:
        reasons.append(
            f"only {free_disk_gb:.1f} GB free disk, need ~{needed_disk_gb:.1f} GB "
            f"for {requirement.name}"
        )

    gpu_vram_gb = report.best_gpu_vram_mb / 1024
    gpu_can_fully_offload = report.has_gpu and gpu_vram_gb >= requirement.quantized_size_gb + _GPU_OFFLOAD_HEADROOM_GB

    if gpu_can_fully_offload:
        recommended_backend = "llama.cpp with full GPU offload"
        recommended_gpu_layers = -1  # llama.cpp convention: offload every layer
    elif report.has_gpu and gpu_vram_gb >= 4.0:
        recommended_backend = "llama.cpp with partial GPU offload (remaining layers on CPU)"
        recommended_gpu_layers = 0  # left for the operator to tune per-model; cannot be assumed generically
        reasons.append(
            f"GPU has {gpu_vram_gb:.1f} GB VRAM, below the ~{requirement.quantized_size_gb + _GPU_OFFLOAD_HEADROOM_GB:.0f} GB "
            "needed for full offload - partial offload is possible but slower than full GPU inference"
        )
    else:
        recommended_backend = "llama.cpp, CPU only"
        recommended_gpu_layers = 0
        if report.has_gpu:
            reasons.append(f"GPU VRAM ({gpu_vram_gb:.1f} GB) is too small to help; falling back to CPU")
        else:
            reasons.append("no GPU detected - CPU-only inference")

    if not gpu_can_fully_offload:
        # CPU-only (or CPU-heavy) inference of a 24B model is characteristically
        # slow on consumer hardware. This is a known, well-documented property
        # of llama.cpp-style inference, not a number specific to this machine —
        # it's reported as a qualitative risk, not a fabricated tokens/sec figure.
        if report.cpu_threads < 8:
            reasons.append(
                f"only {report.cpu_threads} CPU threads - CPU-only inference of a 24B model at this thread "
                "count is very unlikely to complete within the few seconds a live phone turn allows"
            )
        else:
            reasons.append(
                "CPU-only inference of a 24B model, even quantized, is typically too slow for a live phone "
                "turn (multi-second-per-token order of magnitude on consumer CPUs) - only a benchmark on "
                "this exact machine can confirm actual latency"
            )

    if available_ram_gb > 0 and available_ram_gb < requirement.cpu_only_ram_gb:
        reasons.append(
            f"only {available_ram_gb:.1f} GB RAM available right now, need ~{requirement.cpu_only_ram_gb:.1f} GB "
            f"to load {requirement.name} without a fully-offloading GPU"
        )
    elif total_ram_gb > 0 and total_ram_gb < requirement.cpu_only_ram_gb:
        reasons.append(
            f"only {total_ram_gb:.1f} GB total RAM installed, need ~{requirement.cpu_only_ram_gb:.1f} GB"
        )

    disk_ok = free_disk_gb >= needed_disk_gb
    ram_ok = (available_ram_gb >= requirement.cpu_only_ram_gb) or gpu_can_fully_offload
    cpu_speed_risk = not gpu_can_fully_offload and report.cpu_threads < 8

    if not disk_ok or not ram_ok:
        verdict = "UNSUPPORTED HARDWARE"
    elif cpu_speed_risk:
        verdict = "UNSUPPORTED HARDWARE"  # disk/RAM alone are not enough if a live call would time out
    elif not gpu_can_fully_offload:
        verdict = "MARGINAL"
    else:
        verdict = "SUPPORTED"

    if verdict == "SUPPORTED" and not reasons:
        reasons.append(
            f"{gpu_vram_gb:.1f} GB VRAM GPU covers the model with headroom, "
            f"{free_disk_gb:.1f} GB free disk covers the download"
        )

    return Verdict(
        verdict=verdict,
        reasons=reasons,
        recommended_backend=recommended_backend if verdict != "UNSUPPORTED HARDWARE" else None,
        recommended_gpu_layers=recommended_gpu_layers,
    )


def format_report(report: HardwareReport, verdict: Verdict, requirement: ModelRequirement = SARVAM_M_Q4) -> str:
    lines = [
        f"OS               : {report.os_name}",
        f"CPU              : {report.cpu_name} ({report.cpu_threads} logical threads)",
        f"RAM              : {report.total_ram_mb / 1024:.1f} GB total, {report.available_ram_mb / 1024:.1f} GB available now",
        f"Disk free        : {report.free_disk_mb / 1024:.1f} GB",
        f"GPU              : "
        + (", ".join(f"{g.name} ({g.vram_mb / 1024:.1f} GB VRAM)" if g.vram_mb else f"{g.name} (VRAM unknown)" for g in report.gpus) if report.gpus else "none detected"),
        f"Target model     : {requirement.name}",
        f"Verdict          : {verdict.verdict}",
    ]
    if verdict.recommended_backend:
        lines.append(f"Recommended backend: {verdict.recommended_backend}")
    lines.append("Reasons:")
    lines.extend(f"  - {reason}" for reason in verdict.reasons)
    return "\n".join(lines)
