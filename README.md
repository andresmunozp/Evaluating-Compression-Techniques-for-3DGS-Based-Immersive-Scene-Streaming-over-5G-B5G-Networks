# Guía de Uso — Sistema de Compresión 4DGS

Sistema modular de compresión post-entrenamiento para modelos 4D Gaussian Splatting.  
---

## Tabla de Contenidos

1. [Requisitos Previos](#1-requisitos-previos)
2. [Estructura de Archivos](#2-estructura-de-archivos)
3. [Inicio Rápido](#3-inicio-rápido)
4. [Paso 1 — Comprimir](#4-paso-1--comprimir-compresspy)
5. [Paso 2 — Descomprimir](#5-paso-2--descomprimir-decompresspy)
6. [Paso 3 — Benchmark Completo](#6-paso-3--benchmark-completo-benchmark_compressionpy)
7. [Configuraciones YAML](#7-configuraciones-yaml)
8. [Crear tu propia configuración](#8-crear-tu-propia-configuración)
9. [Integración con Mininet](#9-integración-con-mininet)
10. [Visualización en SuperSplat](#10-visualización-en-supersplat)
11. [Interpretación de Resultados](#11-interpretación-de-resultados)
12. [Solución de Problemas](#12-solución-de-problemas)
13. [API Python (uso programático)](#13-api-python-uso-programático)

---

## 1. Requisitos Previos

### Entorno Conda

Usa el mismo entorno donde entrenas 4DGS (necesita PyTorch + CUDA):

```powershell
conda activate Gaussians4D
```

### Dependencias

Las dependencias base ya están instaladas con 4DGaussians. Opcionales:

```bash
# Para compresión zstd (mejor ratio que zlib)
pip install zstandard

# Para compresión lz4 (más rápida que zlib)
pip install lz4

# Para cargar configs tipo mmcv (solo si usas configs .py de arguments/)
pip install mmcv
```

### Modelo Entrenado

Necesitas un modelo 4DGS ya entrenado. La estructura esperada es:

```
output/dynerf/coffee_martini_sirvio/
├── point_cloud/
│   └── iteration_14000/
│       ├── point_cloud.ply           ← Gaussians canónicos
│       ├── deformation.pth           ← Red de deformación
│       ├── deformation_table.pth     ← Tabla auxiliar
│       └── deformation_accum.pth     ← Acumulador auxiliar
├── cameras.json
└── cfg_args
```

---

## 2. Estructura de Archivos

```
compression/
├── __init__.py              # Exporta la API pública
├── base.py                  # GaussianData, DeformationData, CompressionStrategy (ABC)
├── serializer.py            # Formato binario .4dgs (manifest + checksums)
├── pipeline.py              # Pipeline composable de estrategias
├── chunker.py               # Divide en .4dgsc para transmisión por red
├── strategies/
│   ├── __init__.py           # Registro de estrategias
│   ├── quantization.py       # float16, int8, int16 por atributo
│   ├── pruning.py            # Poda por opacidad/deformación/redundancia
│   ├── sh_reduction.py       # Truncar armónicos esféricos (grado 3→0,1,2)
│   ├── hexplane_compression.py  # Comprimir grids HexPlane (quantize/SVD/downsample)
│   └── entropy_coding.py     # Codificación sin pérdida (zlib/gzip/zstd/lz4)
└── configs/
    ├── lossless.yaml          # Solo entropy coding
    ├── quantize_only.yaml     # Solo float16
    ├── balanced.yaml          # Equilibrio calidad/tamaño
    ├── aggressive.yaml        # Máxima compresión
    ├── streaming_optimized.yaml # Optimizado para baja latencia
    ├── hexplane_svd.yaml      # Solo SVD en HexPlane
    └── hexplane_downsample.yaml # Solo downsample en HexPlane

compress.py                  # Script principal de compresión
decompress.py                # Script de descompresión + exportación PLY
benchmark_compression.py     # Benchmark comparativo con métricas de calidad + QoE
```

---

## 3. Inicio Rápido

Ejemplo mínimo usando tu modelo **coffee_martini_sirvio**:

```powershell
# 1. Comprimir con configuración balanceada
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --iteration 14000 ^
    --config compression/configs/balanced.yaml ^
    --output compressed_output/balanced ^
    --chunk_size 524288

# 2. Descomprimir y exportar PLYs
python decompress.py ^
    --input compressed_output/balanced ^
    --output decompressed_output/balanced ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300

# 3. Ver en SuperSplat
#    Abrir decompressed_output/balanced/gaussian_pertimestamp/ en SuperSplat

# Comandos para WSL:
python compress.py --model_path output/dynerf/coffee_martini_sirvio --iteration 14000 --config compression/configs/balanced.yaml --output compressed_output/balanced --chunk_size 524288

python decompress.py --input compressed_output/balanced --output decompressed_output/balanced --configs arguments/dynerf/coffee_martini.py --num_frames 300

```

---

## 4. Paso 1 — Comprimir (`compress.py`)

### Uso Básico

```powershell
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --iteration 14000 ^
    --config compression/configs/balanced.yaml ^
    --output compressed_output/balanced
```

### Parámetros

| Parámetro | Tipo | Default | Descripción |
|-----------|------|---------|-------------|
| `--model_path` | str | **requerido** | Directorio del modelo entrenado |
| `--iteration` | int | `-1` (última) | Iteración a cargar |
| `--config` | str | **requerido** | Archivo YAML con la configuración de compresión |
| `--output` | str | `compressed_output` | Directorio de salida |
| `--chunk_size` | int | `1048576` (1 MB) | Tamaño máximo por chunk en bytes |
| `--no_chunks` | flag | — | Escribir un solo archivo `.4dgs` en vez de chunks |

### Salida

```
compressed_output/balanced/
├── chunk_00000_of_00005.4dgsc    ← Chunks para transmisión
├── chunk_00001_of_00005.4dgsc
├── chunk_00002_of_00005.4dgsc
├── chunk_00003_of_00005.4dgsc
├── chunk_00004_of_00005.4dgsc
└── compression_report.json       ← Reporte detallado
```

### Ejemplo de salida en consola

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

### Archivo único (sin chunks)

Si no necesitas chunks para transmisión:

```powershell
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --config compression/configs/lossless.yaml ^
    --output compressed_output/lossless ^
    --no_chunks
```

Esto genera un solo archivo `model.4dgs`.

---

## 5. Paso 2 — Descomprimir (`decompress.py`)

### Uso Básico

```powershell
python decompress.py ^
    --input compressed_output/balanced ^
    --output decompressed_output/balanced ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300
```

### Parámetros

| Parámetro | Tipo | Default | Descripción |
|-----------|------|---------|-------------|
| `--input` | str | **requerido** | Directorio con chunks `.4dgsc` o archivo `.4dgs` |
| `--output` | str | `decompressed_output` | Directorio de salida |
| `--configs` | str | `None` | Config de hiperparámetros del modelo (p.ej. `arguments/dynerf/coffee_martini.py`) |
| `--num_frames` | int | `300` | Número de frames a exportar |
| `--no_verify` | flag | — | Saltar verificación de checksums |
| `--compression_config` | str | `None` | YAML de compresión (se usa el embebido si no se da) |

### Las 3 Fases

El script separa los tiempos en 3 fases independientes:

| Fase | Qué hace | Relevancia |
|------|----------|------------|
| **1. Assembly** | Reensambla los chunks `.4dgsc` → archivo `.4dgs` | Mide overhead de formato |
| **2. Decode** | Descomprime el archivo → `GaussianData` + `DeformationData` | **Latencia de red** (tiempo que tarda en estar listo tras recibir) |
| **3. Export** | Ejecuta la red de deformación por frame → escribe PLYs | Tiempo de renderizado |

### Salida

```
decompressed_output/balanced/
├── gaussian_pertimestamp/
│   ├── time_00000.ply      ← PLYs compatibles con SuperSplat
│   ├── time_00001.ply
│   ├── ...
│   └── time_00299.ply
└── decompression_report.json
```

### Ejemplo de salida en consola

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

### Desde un archivo único

```powershell
python decompress.py ^
    --input compressed_output/lossless/model.4dgs ^
    --output decompressed_output/lossless ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300
```

---

## 6. Paso 3 — Benchmark Completo (`benchmark_compression.py`)

Compara múltiples configuraciones de compresión de una sola vez.

### Uso Básico

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

### Parámetros

| Parámetro | Tipo | Default | Descripción |
|-----------|------|---------|-------------|
| `--model_path` | str | **requerido** | Directorio del modelo |
| `--iteration` | int | `-1` | Iteración |
| `--source_path` | str | **requerido** | Directorio del dataset fuente |
| `--configs` | str | `None` | Config de hiperparámetros |
| `--compression_configs` | str[] | **requerido** | Lista de YAMLs a comparar |
| `--output_dir` | str | `benchmark_results` | Directorio de resultados |
| `--num_frames` | int | `50` | Frames para evaluar |
| `--bandwidth_mbps` | float | `10.0` | Ancho de banda simulado para QoE |
| `--chunk_size` | int | `1048576` | Tamaño de chunk |
| `--skip_vmaf` | flag | — | Saltar cálculo de VMAF |
| `--skip_render` | flag | — | Solo métricas de compresión (sin renderizado) |

### Solo métricas de compresión (rápido, sin GPU de render)

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

### Probar con diferentes anchos de banda

```powershell
# Simular enlace lento (1 Mbps)
python benchmark_compression.py ... --bandwidth_mbps 1 --output_dir bench_1mbps

# Simular enlace medio (10 Mbps)
python benchmark_compression.py ... --bandwidth_mbps 10 --output_dir bench_10mbps

# Simular enlace rápido (100 Mbps)
python benchmark_compression.py ... --bandwidth_mbps 100 --output_dir bench_100mbps
```

### Salida

```
benchmark_results/
├── benchmark_results.json     ← Resultados completos (JSON)
├── benchmark_summary.csv      ← Tabla resumen (CSV)
├── reference/                 ← Frames renderizados del modelo original
├── lossless/
│   └── decompressed/          ← PLYs descomprimidos
├── balanced/
│   └── decompressed/
└── aggressive/
    └── decompressed/
```

### Ejemplo de tabla de comparación

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

## 7. Configuraciones YAML

### Disponibles

| Archivo | Estrategias | Caso de uso |
|---------|-------------|-------------|
| `lossless.yaml` | Entropy (zlib-9) | Baseline sin pérdida |
| `quantize_only.yaml` | Float16 | ~50% reducción, pérdida mínima |
| `balanced.yaml` | Pruning + SH→1 + fp16 + HexPlane + zlib | Equilibrio general |
| `aggressive.yaml` | Pruning fuerte + SH→0 + int8 + SVD + zlib | Máxima compresión |
| `streaming_optimized.yaml` | Pruning + SH→1 + fp16 + HexPlane + zlib-9 | Baja latencia Mininet |
| `hexplane_svd.yaml` | SVD rank-8 | Evaluar SVD aislado |
| `hexplane_downsample.yaml` | Downsample 2x | Evaluar downsample aislado |
| `lightgaussian_balanced.yaml` | LightGaussian 30% + SH→1 + fp16 + HexPlane + zlib | Pruning por significancia global (equilibrado) |
| `lightgaussian_aggressive.yaml` | LightGaussian 60% + SH→0 + int8 + SVD + zlib-9 | Pruning por significancia global (agresivo) |

---

## 8. Crear tu propia configuración

Crea un archivo YAML en `compression/configs/`. Formato:

```yaml
# mi_config.yaml
strategies:
  - name: NombreDeLaClase
    params:
      param1: valor1
      param2: valor2

  - name: OtraEstrategia
    params:
      ...
```

### Estrategias disponibles y sus parámetros

#### `PruningStrategy` — Poda de Gaussians (por umbrales)

```yaml
- name: PruningStrategy
  params:
    opacity_threshold: 0.005     # Eliminar Gaussians con opacidad < umbral (sigmoid-space)
    deformation_threshold: null  # Eliminar con poca deformación acumulada
    redundancy_radius: null      # Radio para eliminar duplicados (KDTree)
    max_gaussians: 150000        # Límite máximo de Gaussians
```

#### `LightGaussianPruningStrategy` — Poda por Significancia Global (LightGaussian)

Implementa el *Volume-weighted Importance Score* del paper
[LightGaussian (Fan et al., NeurIPS 2024)](https://arxiv.org/abs/2311.17245).

**Diferencia con `PruningStrategy`:** en vez de umbrales fijos de opacidad,
calcula un *Global Significance Score* por Gaussiana que combina:
- **Volumen** (producto de escalas activadas)
- **Importancia** (opacidad o visibilidad en vistas de entrenamiento)

Las Gaussianas con menor puntuación global son podadas. Además, incorpora
un factor opcional de **deformación 4DGS-aware** que protege Gaussians
dinámicamente relevantes (extensión original para 4DGS).

```yaml
# Modo rápido (solo parámetros, sin GPU/cámaras)
- name: LightGaussianPruningStrategy
  params:
    prune_percent: 0.3           # Fracción de Gaussians a eliminar (0.0–1.0)
    v_pow: 0.1                   # Exponente para ratio de volumen normalizado
    importance_mode: parameter   # "parameter" (rápido) o "render" (GPU + cámaras)
    deformation_weight: 0.5      # Peso del bonus de deformación (0 = desactivado)
    prune_decay: 1.0             # Factor de decay iterativo

# Modo render (más fiel al paper, necesita source_path + GPU)
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
    num_views: 50                # Cámaras a muestrear para visibilidad
    temporal_samples: 5          # Timestamps por cámara (4DGS)
```

**Parámetros detallados:**

| Parámetro | Tipo | Default | Descripción |
|-----------|------|---------|-------------|
| `prune_percent` | float | 0.3 | Fracción de Gaussians a eliminar (bottom X% por score) |
| `prune_decay` | float | 1.0 | Multiplicador de decay (para encadenar pruning iterativo) |
| `v_pow` | float | 0.1 | Exponente del ratio de volumen normalizado |
| `importance_mode` | str | `parameter` | `"parameter"`: sigmoid(opacity). `"render"`: visibilidad con forward passes |
| `deformation_weight` | float | 0.0 | Peso del bonus de deformación acumulada (4DGS-aware). 0 = off |
| `source_path` | str | None | Path al dataset (solo modo `render`) |
| `model_path` | str | None | Path al modelo (solo modo `render`) |
| `iteration` | int | -1 | Iteración del modelo (solo modo `render`) |
| `configs` | str | None | Config de hiperparámetros de la red de deformación |
| `num_views` | int | 50 | Vistas de entrenamiento a muestrear (modo `render`) |
| `temporal_samples` | int | 5 | Timestamps uniformes por cámara (modo `render`) |

#### `SHReductionStrategy` — Reducir armónicos esféricos

```yaml
- name: SHReductionStrategy
  params:
    target_sh_degree: 1    # 0, 1, o 2 (original es 3)
    # Grado 0 → solo color DC (máxima reducción, ~76%)
    # Grado 1 → 3 coeficientes más (buena calidad)
    # Grado 2 → 8 coeficientes más
```

#### `QuantizationStrategy` — Cuantización

```yaml
- name: QuantizationStrategy
  params:
    attribute_dtypes:
      xyz: float16          # Opciones: float16, int8, int16, uint8
      features_dc: float16
      features_rest: float16
      opacity: float16
      scaling: float16
      rotation: float16
    quantize_deformation: false  # true → también cuantizar red de deformación a fp16
```

#### `HexPlaneCompressionStrategy` — Comprimir grids HexPlane

```yaml
# OPCIÓN A: Baseline seguro (cuantizar a float16)
- name: HexPlaneCompressionStrategy
  params:
    method: quantize       # Siempre seguro, ~50% en grids

# OPCIÓN B: Experimental SVD (truncated SVD por canal)
- name: HexPlaneCompressionStrategy
  params:
    method: svd
    svd_rank: 8            # Menor rank = más compresión + más pérdida

# OPCIÓN C: Experimental downsample
- name: HexPlaneCompressionStrategy
  params:
    method: downsample
    downsample_factor: 2.0  # Factor de reducción espacial
```

#### `EntropyCodingStrategy` — Codificación lossless

```yaml
- name: EntropyCodingStrategy
  params:
    algorithm: zlib   # Opciones: zlib, gzip, zstd, lz4
    level: 6          # Nivel de compresión (1-9 para zlib/gzip, 1-22 para zstd)
```

### Orden Recomendado

Las estrategias se aplican **en orden secuencial**. El orden recomendado es:

1. **PruningStrategy** o **LightGaussianPruningStrategy** — Primero eliminar datos innecesarios
2. **SHReductionStrategy** — Luego reducir dimensionalidad
3. **QuantizationStrategy** — Cuantizar lo que queda
4. **HexPlaneCompressionStrategy** — Comprimir la red de deformación
5. **EntropyCodingStrategy** — Siempre al final (compresión lossless del resultado)

> **Nota:** `PruningStrategy` y `LightGaussianPruningStrategy` son módulos independientes.
> Puedes usar uno u otro, o incluso ambos en secuencia (primero LightGaussian
> para significancia global, luego PruningStrategy para limpieza adicional).

### Ejemplo personalizado

```yaml
# mi_streaming_lento.yaml — Para enlaces de 1 Mbps
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
    --config compression/configs/mi_streaming_lento.yaml ^
    --output compressed_output/mi_config ^
    --chunk_size 262144
```

---

## 9. Integración con Mininet

### Flujo completo

```
[Host A]                            [Red Mininet]                      [Host B]
  compress.py                                                           decompress.py
  → .4dgsc chunks ──── enviar secuencialmente ────→ recibir chunks ──→ → PLYs
                         (TCP socket, scp, etc.)
```

### Paso a paso

```powershell
# === En Host A (emisor) ===

# 1. Comprimir (ajustar chunk_size según MTU/ventana TCP)
python compress.py ^
    --model_path output/dynerf/coffee_martini_sirvio ^
    --config compression/configs/streaming_optimized.yaml ^
    --output /tmp/chunks ^
    --chunk_size 524288

# 2. Los chunks están en /tmp/chunks/*.4dgsc
#    Enviarlos por la red Mininet (scp, netcat, socket TCP, etc.)

# Ejemplo con netcat:
for f in /tmp/chunks/chunk_*.4dgsc; do
    cat "$f" | nc -q 1 10.0.0.2 9000
done

# === En Host B (receptor) ===

# 3. Recibir chunks en un directorio
mkdir -p /tmp/received_chunks
# (tu script Mininet de recepción guarda los chunks aquí)

# 4. Descomprimir
python decompress.py ^
    --input /tmp/received_chunks ^
    --output /tmp/decompressed ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 300

# 5. Abrir /tmp/decompressed/gaussian_pertimestamp/ en SuperSplat
```

---

## 10. Visualización en SuperSplat

### Cargar secuencia de PLYs

1. Abre **SuperSplat** en el navegador
2. Carga los archivos `time_XXXXX.ply` desde `decompressed_output/balanced/gaussian_pertimestamp/`
3. SuperSplat reproduce la animación frame a frame

Los PLYs exportados son **100% compatibles** con el formato de `export_perframe_3DGS.py`.

---

## 11. Interpretación de Resultados

### `compression_report.json`

```json
{
  "original_size_bytes": 42670000,
  "compressed_size_bytes": 8320000,
  "compression_ratio": 5.13,        // Veces más pequeño
  "savings_pct": 80.5,              // % de ahorro
  "compression_time_s": 1.234,
  "num_gaussians": 134521,
  "num_chunks": 9,
  "chunk_size": 524288
}
```

### `decompression_report.json`

```json
{
  "assemble_time_s": 0.023,    // Rearmar chunks → no relevante en red real
  "decode_time_s": 0.892,      // LATENCIA DE RED (tiempo hasta tener el modelo listo)
  "export_time_s": 45.123,     // Ejecutar deformación + escribir PLYs
  "total_time_s": 46.038,
  "num_gaussians": 115642,     // Puede ser < original si hubo pruning
  "num_frames": 300
}
```

---

## 12. Solución de Problemas

### Error: `ModuleNotFoundError: No module named 'torch'`

Activa el entorno correcto:
```powershell
conda activate Gaussians4D
```

### Error: `FileNotFoundError: PLY not found`

Verifica que la ruta y la iteración sean correctas:
```powershell
# Listar iteraciones disponibles
dir output\dynerf\coffee_martini_sirvio\point_cloud
```

### Error: `Import "zstandard" could not be resolved`

Esto es solo un warning. zstd es opcional. Si quieres usarlo:
```bash
pip install zstandard
```

### Error: `mmcv` no encontrado

Es opcional. Sin él se usan hiperparámetros por defecto. Si tus resultados se ven mal:
```bash
pip install mmcv
```
O pasa los parámetros correctos de la red de deformación manualmente.

### La descompresión es lenta en la Fase 3

La Fase 3 (Export) ejecuta la red de deformación en GPU para cada frame. Es normal que tome 30-120 segundos para 300 frames. Para reducirlo:
- Usa `--num_frames 50` para pruebas
- Asegúrate de que estás usando GPU (CUDA)

### Los PLYs no se ven bien en SuperSplat

Verifica que la configuración de hiperparámetros sea correcta pasando `--configs`:
```powershell
python decompress.py ^
    --input compressed_output/balanced ^
    --output decompressed/ ^
    --configs arguments/dynerf/coffee_martini.py ^
    --num_frames 50
```

---

## 13. API Python (uso programático)

### Compresión básica

```python
import numpy as np
import torch
from compression.base import GaussianData, DeformationData
from compression.pipeline import CompressionPipeline

# Cargar datos
from compress import load_gaussian_data, load_deformation_data

gaussian = load_gaussian_data("output/dynerf/coffee_martini_sirvio", 14000)
deformation = load_deformation_data("output/dynerf/coffee_martini_sirvio", 14000)

# Crear pipeline desde YAML
pipeline = CompressionPipeline.from_yaml("compression/configs/balanced.yaml")

# Comprimir → archivo binario
archive = pipeline.compress_to_archive(gaussian, deformation)
print(f"Compressed: {len(archive) / 1e6:.2f} MB")

# Ver estadísticas
pipeline.print_stats()

# Descomprimir
pipeline2 = CompressionPipeline.from_yaml("compression/configs/balanced.yaml")
dec_gaussian, dec_deformation, manifest = pipeline2.decompress_from_archive(archive)
print(f"Decompressed: {dec_gaussian.num_gaussians} Gaussians")
```

### Pipeline desde diccionario

```python
config = {
    "strategies": [
        {"name": "SHReductionStrategy", "params": {"target_sh_degree": 1}},
        {"name": "QuantizationStrategy", "params": {
            "attribute_dtypes": {"xyz": "float16", "features_dc": "float16"}
        }},
    ]
}
pipeline = CompressionPipeline.from_config(config)
```

### Chunking manual

```python
from compression.chunker import ModelChunker, ModelAssembler

# Dividir
chunker = ModelChunker(chunk_size=512 * 1024)  # 512 KB
paths = chunker.split_and_write(archive, "output_chunks/")

# Reensamblar
reassembled = ModelAssembler.assemble_from_dir("output_chunks/")
assert archive == reassembled
```

### Leer solo el manifest (sin descomprimir)

```python
from compression.serializer import ModelSerializer

manifest = ModelSerializer.read_manifest_only(archive)
print(f"Gaussians: {manifest['num_gaussians']}")
print(f"SH degree: {manifest['sh_degree']}")
print(f"Strategies: {[m['strategy'] for m in manifest['strategy_metadata']]}")
```
