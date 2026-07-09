# Usage Guide — 4DGS Compression System

Modular post-training compression system for 4D Gaussian Splatting models.  
---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [File Structure](#2-file-structure)
3. [Quick Start](#3-quick-start)
4. [Step 1 — Compress (`compress.py`)](#4-step-1--compress-compresspy)
5. [Step 2 — Decompress (`decompress.py`)](#5-step-2--decompress-decompresspy)
6. [Step 3 — Full Benchmark (`benchmark_compression.py`)](#6-step-3--full-benchmark-benchmark_compressionpy)
7. [YAML Configurations](#7-yaml-configurations)
8. [Create Your Own Configuration](#8-create-your-own-configuration)

---

## 1. Prerequisites

### Conda Environment

Use the same environment where you train 4DGS models; it requires PyTorch + CUDA:

```powershell
conda activate Gaussians4D
```

### Dependencies

The base dependencies are already installed with 4DGaussians. Optional dependencies:

```bash
# For zstd compression (better ratio than zlib)
pip install zstandard

# For lz4 compression (faster than zlib)
pip install lz4

# For loading mmcv-style configs (only if you use .py configs from arguments/)
pip install mmcv
```

### Trained Model

You need an already trained 4DGS model. The expected structure is:

```
output/dynerf/coffee_martini_sirvio/
├── point_cloud/
│   └── iteration_14000/
│       ├── point_cloud.ply           ← Canonical Gaussians
│       ├── deformation.pth           ← Deformation network
│       ├── deformation_table.pth     ← Auxiliary table
│       └── deformation_accum.pth     ← Auxiliary accumulator
├── cameras.json
└── cfg_args
```

---

## 2. File Structure

```
compression/
├── __init__.py              # Exports the public API
├── base.py                  # GaussianData, DeformationData, CompressionStrategy (ABC)
├── serializer.py            # .4dgs binary format (manifest + checksums)
├── pipeline.py              # Composable strategy pipeline
├── chunker.py               # Splits into .4dgsc files for network transmission
├── strategies/
│   ├── __init__.py           # Strategy registry
│   ├── quantization.py       # float16, int8, int16 by attribute
│   ├── pruning.py            # Pruning by opacity/deformation/redundancy
│   ├── sh_reduction.py       # Truncate spherical harmonics (degree 3→0,1,2)
│   ├── hexplane_compression.py  # Compress HexPlane grids (quantize/SVD/downsample)
│   └── entropy_coding.py     # Lossless coding (zlib/gzip/zstd/lz4)
└── configs/
    ├── lossless.yaml          # Entropy coding only
    ├── quantize_only.yaml     # Float16 only
    ├── balanced.yaml          # Quality/size balance
    ├── aggressive.yaml        # Maximum compression
    ├── streaming_optimized.yaml # Optimized for low latency
    ├── hexplane_svd.yaml      # SVD only on HexPlane
    └── hexplane_downsample.yaml # Downsample only on HexPlane

compress.py                  # Main compression script
decompress.py                # Decompression + PLY export script
benchmark_compression.py     # Comparative benchmark with quality + QoE metrics
```

---

## 3. Quick Start

Minimal example using your **coffee_martini_sirvio** model:

```powershell
# 1. Compress with the balanced configuration
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --iteration 14000 ^
    --config compression/configs/balanced.yaml ^
    --output compressed_output/balanced ^
    --chunk_size 524288

# 2. Decompress and export PLYs
python decompress.py ^
    --input compressed_output/balanced ^
    --output decompressed_output/balanced ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300

# 3. View in SuperSplat
#    Open decompressed_output/balanced/gaussian_pertimestamp/ in SuperSplat

# Commands for WSL:
python compress.py --model_path output/dynerf/coffee_martini_sirvio --iteration 14000 --config compression/configs/balanced.yaml --output compressed_output/balanced --chunk_size 524288

python decompress.py --input compressed_output/balanced --output decompressed_output/balanced --configs arguments/dynerf/coffee_martini.py --num_frames 300

```

---

## 4. Step 1 — Compress (`compress.py`)

### Basic Usage

```powershell
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --iteration 14000 ^
    --config compression/configs/balanced.yaml ^
    --output compressed_output/balanced
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--model_path` | str | **required** | Directory of the trained model |
| `--iteration` | int | `-1` (latest) | Iteration to load |
| `--config` | str | **required** | YAML file with the compression configuration |
| `--output` | str | `compressed_output` | Output directory |
| `--chunk_size` | int | `1048576` (1 MB) | Maximum size per chunk in bytes |
| `--no_chunks` | flag | — | Write a single `.4dgs` file instead of chunks |

### Output

```
compressed_output/balanced/
├── chunk_00000_of_00005.4dgsc    ← Chunks for transmission
├── chunk_00001_of_00005.4dgsc
├── chunk_00002_of_00005.4dgsc
├── chunk_00003_of_00005.4dgsc
├── chunk_00004_of_00005.4dgsc
└── compression_report.json       ← Detailed report
```

### Example console output

```
Loading model from output/dynerf/coffee_martini_sirvio at iteration 14000
Model loaded in 0.85s  |  134521 Gaussians
Original size: 42.67 MB (Gaussians: 33.38 MB, Deformation: 9.29 MB)
Compressed:    8.32 MB  (ratio 5.13x, savings 80.5%)
Compression time: 1.234s

======================================================================
Compression Pipeline Statistics
======================================================================
  pruning                        | ratio  1.15x | savings 13.1% | compress 0.045s
  sh_reduction                   | ratio  2.90x | savings 65.5% | compress 0.002s
  quantization                   | ratio  1.98x | savings 49.5% | compress 0.011s
  hexplane_quantize              | ratio  1.03x | savings  2.8% | compress 0.005s
  entropy_zlib                   | ratio  1.42x | savings 29.3% | compress 0.892s
======================================================================
Written 9 chunks to compressed_output/balanced/
Report saved to compressed_output/balanced/compression_report.json
```

### Single file (without chunks)

If you do not need chunks for transmission:

```powershell
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --config compression/configs/lossless.yaml ^
    --output compressed_output/lossless ^
    --no_chunks
```

This generates a single `model.4dgs` file.

---

## 5. Step 2 — Decompress (`decompress.py`)

### Basic Usage

```powershell
python decompress.py ^
    --input compressed_output/balanced ^
    --output decompressed_output/balanced ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--input` | str | **required** | Directory with `.4dgsc` chunks or `.4dgs` file |
| `--output` | str | `decompressed_output` | Output directory |
| `--configs` | str | `None` | Model hyperparameter config, e.g., `arguments/dynerf/coffee_martini.py` |
| `--num_frames` | int | `300` | Number of frames to export |
| `--no_verify` | flag | — | Skip checksum verification |
| `--compression_config` | str | `None` | Compression YAML; the embedded one is used if not provided |

### The 3 Phases

The script separates timing into 3 independent phases:

| Phase | What it does | Relevance |
|------|--------------|-----------|
| **1. Assembly** | Reassembles `.4dgsc` chunks → `.4dgs` file | Measures format overhead |
| **2. Decode** | Decompresses the file → `GaussianData` + `DeformationData` | **Network latency**: time until the model is ready after receiving it |
| **3. Export** | Runs the deformation network per frame → writes PLYs | Rendering time |

### Output

```
decompressed_output/balanced/
├── gaussian_pertimestamp/
│   ├── time_00000.ply      ← PLYs compatible with SuperSplat
│   ├── time_00001.ply
│   ├── ...
│   └── time_00299.ply
└── decompression_report.json
```

### Example console output

```
============================================================
PHASE 1: Chunk reassembly
============================================================
  Assembled from chunks: 8.32 MB
  Assembly time: 0.023s

============================================================
PHASE 2: Decode (decompression)
============================================================
  Decoded 115642 Gaussians
  SH degree: 1
  Decode time: 0.892s

============================================================
PHASE 3: Export per-frame PLYs (deformation bake-out)
============================================================
  Exported 300 PLY files to decompressed_output/balanced/gaussian_pertimestamp/
  Export time: 45.123s  (300 frames)

============================================================
TIMING SUMMARY
============================================================
  Chunk assembly:  0.023s
  Decode time:     0.892s
  Export time:      45.123s
  Total time:       46.038s
  Decode-only:     0.892s  (network-relevant latency)
  Decode+Export:   46.015s  (end-to-end reconstruction)
```

### From a single file

```powershell
python decompress.py ^
    --input compressed_output/lossless/model.4dgs ^
    --output decompressed_output/lossless ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300
```

---

## 6. Step 3 — Full Benchmark (`benchmark_compression.py`)

Compares multiple compression configurations in a single run.

### Basic Usage

```powershell
python benchmark_compression.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --iteration 14000 ^
    --source_path data/dynerf/coffee_martini ^
    --configs arguments/dynerf/coffee_martini.py ^
    --compression_configs ^
        compression/configs/lossless.yaml ^
        compression/configs/quantize_only.yaml ^
        compression/configs/balanced.yaml ^
        compression/configs/aggressive.yaml ^
        compression/configs/streaming_optimized.yaml ^
    --output_dir benchmark_results ^
    --num_frames 50 ^
    --bandwidth_mbps 10
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--model_path` | str | **required** | Model directory |
| `--iteration` | int | `-1` | Iteration |
| `--source_path` | str | **required** | Source dataset directory |
| `--configs` | str | `None` | Hyperparameter config |
| `--compression_configs` | str[] | **required** | List of YAML files to compare |
| `--output_dir` | str | `benchmark_results` | Results directory |
| `--num_frames` | int | `50` | Frames to evaluate |
| `--bandwidth_mbps` | float | `10.0` | Simulated bandwidth for QoE |
| `--chunk_size` | int | `1048576` | Chunk size |
| `--skip_vmaf` | flag | — | Skip VMAF calculation |
| `--skip_render` | flag | — | Compression metrics only, without rendering |

### Compression metrics only (fast, no rendering GPU)

```powershell
python benchmark_compression.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --source_path data/dynerf/coffee_martini ^
    --compression_configs ^
        compression/configs/lossless.yaml ^
        compression/configs/balanced.yaml ^
        compression/configs/aggressive.yaml ^
    --output_dir benchmark_results ^
    --skip_render ^
    --bandwidth_mbps 5
```

### Test with different bandwidths

```powershell
# Simulate a slow link (1 Mbps)
python benchmark_compression.py ... --bandwidth_mbps 1 --output_dir bench_1mbps

# Simulate a medium link (10 Mbps)
python benchmark_compression.py ... --bandwidth_mbps 10 --output_dir bench_10mbps

# Simulate a fast link (100 Mbps)
python benchmark_compression.py ... --bandwidth_mbps 100 --output_dir bench_100mbps
```

### Output

```
benchmark_results/
├── benchmark_results.json     ← Full results (JSON)
├── benchmark_summary.csv      ← Summary table (CSV)
├── reference/                 ← Rendered frames from the original model
├── lossless/
│   └── decompressed/          ← Decompressed PLYs
├── balanced/
│   └── decompressed/
└── aggressive/
    └── decompressed/
```

### Example comparison table

```
Config               Size MB  Ratio  Save%  Decode Startup  Rebuf  QoE
---------------------------------------------------------------------------
lossless               28.53   1.49  33.1%  0.234s    2.51s      0  4.5
quantize_only          16.92   2.52  60.3%  0.189s    1.58s      0  4.7
balanced                8.32   5.13  80.5%  0.892s    1.56s      0  4.5
aggressive              3.87  11.02  90.9%  0.645s    0.96s      0  4.8
streaming_optimized     6.15   6.94  85.6%  0.723s    1.22s      0  4.6
```

---

## 7. YAML Configurations

### Available

| File | Strategies | Use case |
|------|------------|----------|
| `lossless.yaml` | Entropy (zlib-9) | Lossless baseline |
| `quantize_only.yaml` | Float16 | ~50% reduction, minimal loss |
| `balanced.yaml` | Pruning + SH→1 + fp16 + HexPlane + zlib | General balance |
| `aggressive.yaml` | Strong pruning + SH→0 + int8 + SVD + zlib | Maximum compression |
| `streaming_optimized.yaml` | Pruning + SH→1 + fp16 + HexPlane + zlib-9 | Low-latency Mininet |
| `hexplane_svd.yaml` | SVD rank-8 | Evaluate SVD in isolation |
| `hexplane_downsample.yaml` | Downsample 2x | Evaluate downsampling in isolation |
| `lightgaussian_balanced.yaml` | LightGaussian 30% + SH→1 + fp16 + HexPlane + zlib | Global-significance pruning (balanced) |
| `lightgaussian_aggressive.yaml` | LightGaussian 60% + SH→0 + int8 + SVD + zlib-9 | Global-significance pruning (aggressive) |

---

## 8. Create Your Own Configuration

Create a YAML file in `compression/configs/`. Format:

```yaml
# my_config.yaml
strategies:
  - name: ClassName
    params:
      param1: value1
      param2: value2

  - name: AnotherStrategy
    params:
      ...
```

### Available strategies and their parameters

#### `PruningStrategy` — Gaussian pruning by thresholds

```yaml
- name: PruningStrategy
  params:
    opacity_threshold: 0.005     # Remove Gaussians with opacity < threshold (sigmoid-space)
    deformation_threshold: null  # Remove Gaussians with low accumulated deformation
    redundancy_radius: null      # Radius for removing duplicates (KDTree)
    max_gaussians: 150000        # Maximum number of Gaussians
```

#### `LightGaussianPruningStrategy` — Global Significance Pruning (LightGaussian)

Implements the *Volume-weighted Importance Score* from the
[LightGaussian (Fan et al., NeurIPS 2024)](https://arxiv.org/abs/2311.17245) paper.

**Difference from `PruningStrategy`:** instead of fixed opacity thresholds,
it computes a *Global Significance Score* for each Gaussian by combining:
- **Volume** (product of activated scales)
- **Importance** (opacity or visibility in training views)

Gaussians with the lowest global score are pruned. It also includes
an optional **4DGS-aware deformation** factor that protects dynamically
relevant Gaussians (an original extension for 4DGS).

```yaml
# Fast mode (parameters only, no GPU/cameras)
- name: LightGaussianPruningStrategy
  params:
    prune_percent: 0.3           # Fraction of Gaussians to remove (0.0–1.0)
    v_pow: 0.1                   # Exponent for normalized volume ratio
    importance_mode: parameter   # "parameter" (fast) or "render" (GPU + cameras)
    deformation_weight: 0.5      # Weight of the deformation bonus (0 = disabled)
    prune_decay: 1.0             # Iterative decay factor

# Render mode (closer to the paper, requires source_path + GPU)
- name: LightGaussianPruningStrategy
  params:
    prune_percent: 0.3
    v_pow: 0.1
    importance_mode: render
    deformation_weight: 0.5
    source_path: data/dynerf/coffee_martini
    model_path: output/dynerf/coffee_martini_sirvio
    iteration: 14000
    configs: arguments/dynerf/coffee_martini.py
    num_views: 50                # Cameras to sample for visibility
    temporal_samples: 5          # Timestamps per camera (4DGS)
```

**Detailed parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prune_percent` | float | 0.3 | Fraction of Gaussians to remove (bottom X% by score) |
| `prune_decay` | float | 1.0 | Decay multiplier for chaining iterative pruning |
| `v_pow` | float | 0.1 | Exponent of the normalized volume ratio |
| `importance_mode` | str | `parameter` | `"parameter"`: sigmoid(opacity). `"render"`: visibility with forward passes |
| `deformation_weight` | float | 0.0 | Weight of the accumulated deformation bonus (4DGS-aware). 0 = off |
| `source_path` | str | None | Dataset path (render mode only) |
| `model_path` | str | None | Model path (render mode only) |
| `iteration` | int | -1 | Model iteration (render mode only) |
| `configs` | str | None | Deformation-network hyperparameter config |
| `num_views` | int | 50 | Training views to sample (render mode) |
| `temporal_samples` | int | 5 | Uniform timestamps per camera (render mode) |

#### `SHReductionStrategy` — Reduce spherical harmonics

```yaml
- name: SHReductionStrategy
  params:
    target_sh_degree: 1    # 0, 1, or 2 (the original is 3)
    # Degree 0 → DC color only (maximum reduction, ~76%)
    # Degree 1 → 3 additional coefficients (good quality)
    # Degree 2 → 8 additional coefficients
```

#### `QuantizationStrategy` — Quantization

```yaml
- name: QuantizationStrategy
  params:
    attribute_dtypes:
      xyz: float16          # Options: float16, int8, int16, uint8
      features_dc: float16
      features_rest: float16
      opacity: float16
      scaling: float16
      rotation: float16
    quantize_deformation: false  # true → also quantize the deformation network to fp16
```

#### `HexPlaneCompressionStrategy` — Compress HexPlane grids

```yaml
# OPTION A: Safe baseline (quantize to float16)
- name: HexPlaneCompressionStrategy
  params:
    method: quantize       # Always safe, ~50% reduction in grids

# OPTION B: Experimental SVD (truncated SVD per channel)
- name: HexPlaneCompressionStrategy
  params:
    method: svd
    svd_rank: 8            # Lower rank = more compression + more loss

# OPTION C: Experimental downsampling
- name: HexPlaneCompressionStrategy
  params:
    method: downsample
    downsample_factor: 2.0  # Spatial reduction factor
```

#### `EntropyCodingStrategy` — Lossless coding

```yaml
- name: EntropyCodingStrategy
  params:
    algorithm: zlib   # Options: zlib, gzip, zstd, lz4
    level: 6          # Compression level (1-9 for zlib/gzip, 1-22 for zstd)
```

### Recommended Order

Strategies are applied **sequentially**. The recommended order is:

1. **PruningStrategy** or **LightGaussianPruningStrategy** — First remove unnecessary data
2. **SHReductionStrategy** — Then reduce dimensionality
3. **QuantizationStrategy** — Quantize what remains
4. **HexPlaneCompressionStrategy** — Compress the deformation network
5. **EntropyCodingStrategy** — Always last (lossless compression of the result)

> **Note:** `PruningStrategy` and `LightGaussianPruningStrategy` are independent modules.
> You can use one or the other, or even both sequentially: first LightGaussian
> for global significance, then PruningStrategy for additional cleanup.

### Custom example

```yaml
# my_slow_streaming.yaml — For 1 Mbps links
strategies:
  - name: PruningStrategy
    params:
      opacity_threshold: 0.02
      max_gaussians: 50000

  - name: SHReductionStrategy
    params:
      target_sh_degree: 0

  - name: QuantizationStrategy
    params:
      attribute_dtypes:
        xyz: int8
        features_dc: int8
        opacity: float16
        scaling: int8
        rotation: int8
      quantize_deformation: true

  - name: HexPlaneCompressionStrategy
    params:
      method: svd
      svd_rank: 4

  - name: EntropyCodingStrategy
    params:
      algorithm: zlib
      level: 9
```

```powershell
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --config compression/configs/my_slow_streaming.yaml ^
    --output compressed_output/my_config ^
    --chunk_size 262144
```

---
