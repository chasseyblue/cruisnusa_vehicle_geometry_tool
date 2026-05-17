# Cruis'n USA Vehicle Geometry Tool

`cruisnusa_vehicle_geometry_tool.py` extracts source-backed vehicle geometry from the Cruis'n USA ROM set and exports it as Wavefront OBJ/MTL.

This extractor is focused on the **vehicle models referenced by the game source**, not the broader world/static geometry scans.

It reconstructs:

- vehicle model roots from the compiled `VEHICLE_TABLE`
- vertex and polygon data from the program ROM bank (`u10_13`)
- UVs using the runtime packed AIV coordinates **plus** the texture-page row base
- optional degraded/LOD vehicle models
- debug reports and atlas guide SVGs

---

## What this script does

The script uses two inputs:

1. **The main ROM ZIP** (`maindata.zip`)
2. **The source tree ZIP or extracted source directory**

From the source, it parses:

- `WAVE.ASM` for vehicle slot definitions
- the compiled `VEHICLE_TABLE` bridge
- source texture evidence showing that texture map addresses behave like **row bases inside a vertically stacked 256-wide atlas**

From the ROMs, it:

- rebuilds the interleaved `u10_13` program/data bank
- resolves each vehicle model pointer into a byte offset
- parses model headers, vertices, polygons, texpage values, and local UV byte pairs
- exports OBJ/MTL files with corrected stacked-atlas UVs

---

## Proven data path

This extractor is based on a source-backed chain rather than loose heuristics:

- `WAVE.ASM` defines the vehicle slots used by the game
- the compiled `VEHICLE_TABLE` is found in `u10_13`
- vehicle model pointers resolve into `u10_13`, not the other scanned geometry banks
- polygon records contain:
  - vertex indices
  - packed local AIV UV byte pairs
  - a runtime texture map address / texpage row base
- source image symbol progression shows that the texture map address advances in **row units**, which is consistent with a **vertically stacked atlas**

That means the final exported UVs are not just `u,v = local_u, local_v`; they are:

- `u = local_u`
- `v = (texpage_row_base - atlas_base_row) + local_v`

---

## Requirements

- Python 3.9+
- No third-party packages required

Uses only the Python standard library:

- `argparse`
- `csv`
- `json`
- `re`
- `struct`
- `zipfile`
- `dataclasses`
- `pathlib`

---

## Supported inputs

### ROM ZIP

The script expects the Cruis'n USA main ROM archive containing at least the program ROM files used to build `u10_13`:

- `v4.5_4-11-95_cruisn_usa_u10_86b3.u10`
- `v4.5_4-11-95_cruisn_usa_u11_6d73.u11`
- `v4.5_4-11-95_cruisn_usa_u12_4b32.u12`
- `v4.5_4-11-95_cruisn_usa_u13_430e.u13`

### Source input

Pass either:

- the Cruis'n USA source ZIP, or
- an extracted source directory

The source is required for this extractor because it supplies the source-backed vehicle bridge and UV proof.

---

## Usage

Basic usage:

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip"
```

Write to a custom output directory:

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out
```

Include degrade models when present:

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out --include-degraded
```

Windows example:

```powershell
python cruisnusa_vehicle_geometry_tool.py "C:\Cruisn\maindata.zip" --source "C:\Cruisn\cruisin-usa-main.zip" -o "C:\Cruisn\vehicle_out"
```

---

## Command-line options

### Required arguments

#### `input`

Path to the Cruis'n USA ROM ZIP.

#### `--source`

Path to the source ZIP or extracted source directory.

---

### Output control

#### `-o, --out`

Output directory.

Default:

```text
cruisnusa_vehicle_out
```

---

### Model selection

#### `--include-degraded`

Also export `degrade1` and `degrade2` model (LODs) roots when present.

Without this flag, only the base/root vehicle model is exported for each slot.

---

### UV options

#### `--uv-mode {stacked,local}`

UV export mode.

- `stacked` = recommended; reconstruct atlas-space UVs using runtime texpage row bases
- `local` = older/debug behavior; uses only local UV byte pairs

Default:

```text
stacked
```

#### `--atlas-width N`

Atlas width in pixels for stacked mode.

Default:

```text
256
```

#### `--base-row auto|VALUE`

Base row used for stacked UV reconstruction.

- `auto` = use the minimum observed texpage value for the model
- integer or hex value = force a specific base row

Examples:

```bash
--base-row auto
--base-row 0xA56
--base-row 2646
```

Default:

```text
auto
```

#### `--row-bias N`

Additive bias applied to each runtime texpage row before stacking.

Useful for edge-case testing when validating against textures.

Default:

```text
0
```

#### `--uv-denom {255,256,image_extent}`

UV normalization strategy.

- `255` = normalize by 255
- `256` = normalize by 256
- `image_extent` = normalize by actual atlas/image size

Default:

```text
image_extent
```

#### `--no-vflip`

Disable V flip.

By default, V is flipped for OBJ export.

---

### Material options

#### `--multi-material`

Keep one material per texpage instead of collapsing to a single atlas material.

Default behavior is a **single atlas material** per exported model.

---

### Debug output options

#### `--no-guides`

Disable SVG atlas guide output.

By default, the script writes per-model SVG guides showing:

- page bands
- UV occupancy rectangles
- row spans per texpage

---

## Recommended commands

### Standard extraction

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out
```

### Include degraded models

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out --include-degraded
```

### Compare against old/local UV behavior

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out_local --uv-mode local
```

### Test a row bias

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out_bias --row-bias -1
```

### Disable V flip

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out_novflip --no-vflip
```

### Force separate materials per texpage

```bash
python cruisnusa_vehicle_geometry_tool.py maindata.zip --source "cruisin-usa-main.zip" -o cruisnusa_vehicle_out_multi --multi-material
```

---

## Output structure

A typical output directory looks like this:

```text
cruisnusa_vehicle_out/
├─ atlas_guides/
│  ├─ slot00_cvette_base.svg
│  ├─ slot01_hotrod_base.svg
│  └─ ...
├─ objs/
│  ├─ slot00_cvette_base.obj
│  ├─ slot00_cvette_base.mtl
│  ├─ slot01_hotrod_base.obj
│  ├─ slot01_hotrod_base.mtl
│  └─ ...
├─ atlas_hints.json
├─ manifest.csv
├─ manifest.json
├─ scan_report.txt
└─ source_bridge.json
```

---

## Output files explained

### `objs/*.obj`

Exported vehicle geometry.

### `objs/*.mtl`

Material library for each OBJ.

By default, each OBJ uses one atlas material named:

```text
vehicle_atlas
```

With `--multi-material`, materials are split per texpage.

### `manifest.json`

Structured model metadata for all parsed models.

Includes fields such as:

- label
- word address
- byte offset
- radius
- vertex count
- polygon count
- texpage values
- validity status

### `manifest.csv`

Spreadsheet-friendly summary of the parsed/exported models.

### `scan_report.txt`

Human-readable report containing:

- compiled `VEHICLE_TABLE` location
- source UV proof summary
- per-slot model stats
- atlas row/base information

### `source_bridge.json`

Bridge data linking source-derived vehicle slots to compiled ROM model addresses.

### `atlas_hints.json`

Per-model atlas plan information, including:

- atlas width/height
- base row
- min/max texpage row
- local UV extents
- page bands

### `atlas_guides/*.svg`

Debug guides showing the inferred vertically stacked atlas usage for each model.

These are useful when validating texture placement and texpage ranges.

---

## Export naming

Export names are generated as:

```text
slotNN_symbol_suffix
```

Examples:

- `slot00_cvette_base`
- `slot01_hotrod_base`
- `slot02_missle_base`
- `slot03_testor_base`

If `--include-degraded` is enabled, additional outputs may include:

- `_degrade1`
- `_degrade2`

---

## Current limitations

This script currently exports:

- vehicle geometry
- stacked-atlas UVs
- MTL structure
- debug/bridge metadata

It does **not** currently reconstruct and write the final texture atlas image itself.

---

## Troubleshooting

### "Compiled VEHICLE_TABLE not found in u10_13"

Likely causes:

- wrong ROM ZIP
- missing or differently named program ROM files
- incompatible ROM revision

Check that the ZIP contains the expected `u10`–`u13` files.

### "word address not inside u10_13"

The resolved model pointer does not map into the expected program/data bank.

Possible causes:

- incompatible ROM/source pairing
- different build/revision
- corrupted interleave source data

### UVs still look slightly off

Try the following:

```bash
--row-bias -1
--row-bias 1
--uv-denom 255
--uv-denom 256
--no-vflip
```

Also compare against:

```bash
--uv-mode local
```

If `local` looks much worse and `stacked` looks close, the stacked-atlas path is doing the right job and only needs minor alignment refinement.

---

## License / usage note

This README documents the extraction workflow and reverse-engineered format behavior for preservation, research, and tooling purposes.

## Thanks

To [historicalsource](https://github.com/historicalsource/cruisin-usa) for Cruis'n USA Source Code
