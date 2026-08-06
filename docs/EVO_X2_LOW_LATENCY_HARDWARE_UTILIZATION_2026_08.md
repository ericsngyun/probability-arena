# EVO-X2 hardware utilization and tool qualification — 2026-08

**Status:** Gate 1 inventory verified. **Benchmarks required before any stack is
selected.** Nothing installed, changed or adopted.

---

## 1. Verified inventory (read-only, 2026-08-06)

| | |
|---|---|
| CPU | **AMD Ryzen AI MAX+ 395 w/ Radeon 8060S**, 32 CPU compute units |
| iGPU | **gfx1151** (Strix Halo), **40 CUs**, PCI `c5:00.0` |
| VRAM / GTT | **1,024 MiB dedicated / 131,072 MiB (128 GiB) GTT** |
| NPU | present — `c6:00.1` Signal processing controller, `/dev/accel/accel0` |
| OS / kernel | Ubuntu 24.04.4 LTS / **6.17.0-1020-oem** |
| ROCm | **6.3.1** at `/opt/rocm-6.3.1`; `rocminfo`, `amd-smi`, `rocm-smi` present |
| `rocminfo` agent | `amdgcn-amd-amdhsa--gfx1151` — the GPU is visible to ROCm |
| PyTorch | **not installed system-wide** |
| Ollama | installed and holding **gpt-oss:120b (65 GB)**, qwen3.6:35b-a3b (23 GB), qwen3-embedding:4b, qwen3.5:2b/4b |
| Docker | 29.2.1, **26 running containers** |
| NVMe | Lexar SSD NQ790 **2 TB**; root LV **236 GB, 88 GB free** |
| Network | **eno1 at 1000 Mb/s** (1 GbE) — the only physical NIC |
| Clock | clocksource **tsc**, NTP synchronized, TZ UTC |
| Load | 0.31 / 0.41 / 0.29 |

### The finding that reframes the workstream

**EVO-X2 is not a dedicated research host.** It runs **26 containers** —
`pleadly-api`, `langfuse`, `miko-dashboard`, `miko-command-postgres`,
`riven-mariadb`, `backup-agent` and others — alongside Probability Arena's
watcher, MarketOps timers and a 65 GB local model.

Every resource-envelope decision has to start there. The question is not "how do
we use this hardware fully", it is "how much of an already-busy machine can
Probability Arena safely claim, and what must yield to what". A protected
envelope on a box with 26 neighbours is a real engineering constraint, and
sizing it from an idle-looking load average would be a mistake.

### Two inventory facts that constrain the program

**1 GbE and a residential-adjacent path.** One physical NIC at 1000 Mb/s. Link
speed is not the issue for a market-data feed; **RTT and jitter to the venue**
are, and neither has been measured. This is direct evidence for the
architecture document's position that live execution placement must be chosen
from measurement, not from where the research box happens to sit.

**VRAM 1 GiB / GTT 128 GiB.** Strix Halo's dedicated VRAM is small and the
usable pool is GTT out of unified memory. Any inference budget is therefore a
*system memory* budget shared with 26 containers and the filesystem cache that
SQLite depends on. Raising the BIOS graphics allocation is a plausible lever and
is explicitly **not** taken here: it is a reboot-requiring, rollback-defined
change to a production host.

## 2. Support matrix — status, not benchmarks

| stack | gfx1151 status | notes |
|---|---|---|
| ROCm 6.3.1 + PyTorch | plausible — ROCm sees the agent | PyTorch not installed; wheel/gfx1151 support must be verified, not assumed |
| llama.cpp ROCm/HIP | likely viable | needs a build against ROCm 6.3.1 |
| llama.cpp Vulkan | likely viable, often the pragmatic Strix Halo path | no ROCm version coupling |
| Ollama | **already operational** on this host | operational wrapper; limited concurrency/batching control |
| vLLM ROCm | doubtful on gfx1151 | data-center AMD support does **not** imply Strix Halo support |
| SGLang ROCm | unqualified | benchmark only with current executable evidence of gfx1151 viability |
| Ryzen AI NPU / ONNX Runtime GenAI | device present, toolchain unproven on Linux | `/dev/accel/accel0` exists; `amdxdna` not visible in `lsmod` |

**Nothing above is a recommendation.** The prompt's rule — do not adopt because
popular or new — cuts both ways: Ollama's incumbency is not a reason to keep it
either. It is, however, a reason to benchmark it first, since it is the only
stack already proven to run on this host.

## 3. NPU decision — **DEFER**

`DEFER NPU — TOOLCHAIN COST EXCEEDS BENEFIT`, provisionally.

The device is present and `/dev/accel/accel0` is bound, but `amdxdna` does not
appear as a loaded module and the Linux Ryzen AI toolchain requires model
conversion for a narrow supported set. The candidate workloads — classification,
evidence routing, small embeddings — are all comfortably served by the iGPU
today.

Revisit only if a measured iGPU contention problem appears. Using the NPU to
claim full hardware utilization is explicitly not a reason.

## 4. Benchmark methodology — defined, not yet run

Three workload classes: **interactive agent** (short prompt, structured output,
256–1,024 out, concurrency 1–4), **research synthesis** (8–32K prompt, 1–4K out,
concurrency 1–2), **high-volume classifier** (short prompts, constrained output,
concurrency 8–32).

Per engine/model capture: load time, TTFT, prompt tok/s, decode tok/s,
p50/p95/p99, throughput, RSS, GTT, CPU, power, thermals, failures, context
stability, structured-output correctness, tool-call correctness.

**Predeclared production guard, set before any benchmark runs:** abort
immediately if MarketOps cycle duration exceeds **90 s** (baseline 38–78 s), if
any cycle returns non-`ok`, if `database_locked` events rise above **5**, or if
watcher tick cadence drops below **2/s** (baseline ≈2.4/s). Local AI yields to
production, and the threshold is fixed in advance so a tempting benchmark result
cannot renegotiate it.

## 5. Proposed service topology

```
probability-arena-production.slice   highest priority, memory reserved
realtime-collectors.slice            latency-sensitive, CPU weight high
local-ai.slice                       yields first, memory-capped, OOM-first
batch-replay.slice                   lowest, pressure-stopped
```

Budgets are deliberately **not** numbered yet: sizing them requires Gate 2
baselines on a host whose 26 containers have not been profiled. Numbers invented
before measurement would be the same error as a guessed `_fp` divisor.

**No new production service or timer is installed by this document.**

## 6. Storage decision

Consistent with `LOW_LATENCY_ARCHITECTURE_001.md` §10: in-memory book +
append-only compressed event files + Parquet (ZSTD) research archive + SQLite
for low-frequency metadata only. Partition `venue=/date=/hour=`.

**Capacity finding:** root is 236 GB with 88 GB free on a 2 TB NVMe — most of
the device is not in the root LV. Before any archive is written, the Parquet
target must land on deliberately provisioned space, not on the 88 GB that also
holds a 4.24 GiB research database, its backups and 26 containers.

## 7. EVO versus hosted — not yet measured

Required from EVO-X2 and ≥2 hosted regions: Kalshi REST RTT, Kalshi WS
RTT/heartbeat, Polymarket REST/WS RTT, Solana RPC/gRPC latency, jitter, packet
loss, reconnect behaviour, and later demo order-ack latency.

Prior conclusion stands: EVO-X2 keeps research, governance, replay and
supervisory control. Live execution placement is chosen from measurement.

## 8. Rejected for now, with reasons

| rejected | reason |
|---|---|
| Kafka / Redpanda / NATS | no measured multi-consumer durable fan-out requirement |
| ClickHouse / PostgreSQL | Parquet + DuckDB covers the query shape; no measured need |
| Rust rewrite | Python `asyncio` has not been benchmarked and therefore has not failed |
| vLLM / SGLang on gfx1151 | no current evidence of support; would likely force a ROCm or kernel upgrade on a production host |
| ROCm or kernel upgrade | forbidden as a benchmarking convenience |
| BIOS graphics reallocation | reboot-requiring production change; needs rollback and approval |
| NPU adoption | §3 |

## 9. Rollback

Nothing was installed, changed or configured. Every command in Gate 1 was
read-only inspection. Rollback is the empty set.

## 10. 30/60/90-day plan

**Days 1–30** — Gate 2 baselines including the 26-container neighbourhood;
inference benchmark matrix (Ollama incumbent first, then llama.cpp
Vulkan/ROCm); Kalshi real-time collection *once §12 of the architecture document
is unblocked*; Parquet archive on provisioned space; deterministic replay;
`.slice` isolation; full telemetry.
*Consumes:* CPU + iGPU + GTT (inference), NVMe (archive), network (collector).

**Days 31–60** — Kalshi shadow execution; Polymarket observation; Solana route
observation; batch strategy evaluation; agentic research and postmortem
workflows.
*Consumes:* CPU (replay, fixed-point math), NVMe (archive growth), network.

**Days 61–90** — Kalshi demo execution; venue placement benchmarking; strategy
registry integration; bounded execution-gateway *design*; Solana shadow
execution.
*Consumes:* network (latency benchmarking from multiple locations), CPU.

## 11. Verdict

`EVO-X2 UTILIZATION PLAN COMPLETE — BENCHMARKS REQUIRED BEFORE SELECTION`

The inventory is verified and it changed the plan: this is a shared host with 26
containers and an existing 65 GB model, one 1 GbE NIC, and a root filesystem
with 88 GB free. Selecting an inference stack or sizing a resource envelope from
that inventory alone would be exactly the "adopt because it sounds faster"
failure the workstream forbids.
