#!/usr/bin/env python3
"""
Cruis'n USA vehicle extractor with source-backed stacked-atlas UV reconstruction.
src: https://github.com/historicalsource/cruisin-usa

    By Chassey Blue
    https://chasseyblue.com
    https://github.com/chasseyblue


1) Source head-to-head/menu TGAs in the uploaded source are all 256 pixels wide.
2) Their compiled image symbols in OBJECTS.EQU increase by exactly the TGA height,
   which proves the runtime texture-map address is a row-base inside a vertically
   stacked 256-wide atlas.
3) Compiled object polygons carry packed local AIV coordinates (8-bit x/y pairs)
   plus a runtime texture-map address. Therefore the final atlas-space texel row is:
       atlas_row = (texpage_row_base - atlas_base_row) + local_v
   while atlas-space X is just local_u.

Outputs
-------
- OBJ + MTL per vehicle root (and optional degrade models)
- manifest.csv / manifest.json
- scan_report.txt
- source_bridge.json
- atlas_hints.json      (per-model stacked-atlas width/height/base row)
- atlas_guides/*.svg    (debug guides showing page bands + used UV rectangles)

Notes
-----
- "stacked" UV mode is the recommended default for vehicle exports.
- "local" UV mode reproduces the older behavior for comparison/testing.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import struct
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROGRAM_ROMS = {
    10: "v4.5_4-11-95_cruisn_usa_u10_86b3.u10",
    11: "v4.5_4-11-95_cruisn_usa_u11_6d73.u11",
    12: "v4.5_4-11-95_cruisn_usa_u12_4b32.u12",
    13: "v4.5_4-11-95_cruisn_usa_u13_430e.u13",
}


def s16(v: int) -> int:
    return struct.unpack("<h", struct.pack("<H", v & 0xFFFF))[0]


def sanitize_name(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    out = re.sub(r"_+", "_", out).strip("_")
    return out or "unnamed"


class SourceRepo:
    def __init__(self, src: Path):
        self.src = src
        self.zf: Optional[zipfile.ZipFile] = None
        self.names: List[str] = []
        if src.is_file() and src.suffix.lower() == ".zip":
            self.zf = zipfile.ZipFile(src, "r")
            self.names = self.zf.namelist()

    def read_text(self, suffix: str) -> str:
        if self.zf is not None:
            for n in self.names:
                if n.upper().endswith(suffix.upper()):
                    return self.zf.read(n).decode("utf-8", errors="ignore")
            raise FileNotFoundError(suffix)
        for p in self.src.rglob("*"):
            if p.name.upper() == suffix.upper():
                return p.read_text(encoding="utf-8", errors="ignore")
        raise FileNotFoundError(suffix)

    def list_files(self) -> List[str]:
        if self.zf is not None:
            return list(self.names)
        return [str(p.relative_to(self.src)).replace("\\", "/") for p in self.src.rglob("*") if p.is_file()]

    def read_bytes(self, suffix: str) -> bytes:
        if self.zf is not None:
            for n in self.names:
                if n.upper().endswith(suffix.upper()):
                    return self.zf.read(n)
            raise FileNotFoundError(suffix)
        for p in self.src.rglob("*"):
            if p.name.upper() == suffix.upper():
                return p.read_bytes()
        raise FileNotFoundError(suffix)

    def iter_tga_headers(self) -> Iterable[Tuple[str, int, int]]:
        names = self.names if self.zf is not None else [str(p) for p in self.src.rglob("*.TGA")]
        for n in names:
            if not n.upper().endswith(".TGA"):
                continue
            try:
                data = self.zf.read(n) if self.zf is not None else Path(n).read_bytes()
            except Exception:
                continue
            if len(data) < 18:
                continue
            w = struct.unpack_from("<H", data, 12)[0]
            h = struct.unpack_from("<H", data, 14)[0]
            yield (Path(n).name, w, h)

    def close(self) -> None:
        if self.zf is not None:
            self.zf.close()


@dataclass
class VehicleSlot:
    slot: int
    symbol: str
    display_name: str
    kind: str
    comment: str


@dataclass
class VehicleRoot:
    slot: int
    symbol: str
    display_name: str
    kind: str
    model_word_addr: int
    palette_word: int
    ani_word_addr: int
    degrade1_word_addr: int
    degrade2_word_addr: int
    model_byte_off_u10_13: int
    degrade1_byte_off_u10_13: Optional[int]
    degrade2_byte_off_u10_13: Optional[int]


@dataclass
class ParsedModel:
    label: str
    word_addr: int
    byte_off: int
    radius: int
    vertex_count: int
    polygon_count: int
    bbox_min: Tuple[int, int, int]
    bbox_max: Tuple[int, int, int]
    texpage_values: List[int]
    exact_bytes: int
    valid: bool
    invalid_reason: str


@dataclass
class AtlasPlan:
    width: int
    height: int
    base_row: int
    min_page: int
    max_page: int
    row_bias: int
    local_u_max: int
    local_v_max: int
    pages: List[Dict[str, int]]


class RomSet:
    def __init__(self, src: Path):
        self.src = src
        self.zf = zipfile.ZipFile(src, "r")

    def read(self, name: str) -> bytes:
        return self.zf.read(name)

    def build_u10_13(self) -> bytes:
        parts = [self.read(PROGRAM_ROMS[u]) for u in (10, 11, 12, 13)]
        size = len(parts[0])
        if len({len(p) for p in parts}) != 1:
            raise ValueError("Program ROM sizes do not match")
        out = bytearray(size * 4)
        for i in range(size):
            out[i * 4 + 0] = parts[0][i]
            out[i * 4 + 1] = parts[1][i]
            out[i * 4 + 2] = parts[2][i]
            out[i * 4 + 3] = parts[3][i]
        return bytes(out)

    def close(self) -> None:
        self.zf.close()


def parse_source_vehicle_slots(source_tree: Path) -> List[VehicleSlot]:
    repo = SourceRepo(source_tree)
    try:
        wave_text = repo.read_text("WAVE.ASM")
    finally:
        repo.close()

    display_names: Dict[str, str] = {
        "cvette": "63 Muscle Car",
        "hotrod": "La Bomba",
        "missle": "Devastator VI",
        "testor": "Italia P69",
        "jeep": "Jeep",
        "sbus": "School Bus",
        "sbusp": "School Bus (player_override)",
        "sbuspm": "School Bus (player_override_degrade?)",
        "copcar": "Cop Car",
        "copcarp": "Cop Car (player_override)",
        "gtruck": "G Truck",
        "gtruckp": "G Truck (player_override)",
        "ftruck": "Fire Truck",
        "cbus": "CUSA Tour Bus",
        "muscle": "Muscle",
        "caravan": "Caravan",
        "ptruckg": "Pickup Truck",
        "mustang": "Mustang",
        "toxic": "Toxic Waste Frieght Train",
    }

    slots: List[VehicleSlot] = []
    in_table = False
    current_slot: Optional[int] = None
    current_comment = ""
    for raw in wave_text.splitlines():
        code = raw.split(";")[0].rstrip()
        if "VEHICLE_TABLE:" in raw:
            in_table = True
            continue
        if not in_table:
            continue
        if raw.lstrip().startswith("romdata"):
            break
        m_idx = re.search(r";#(\d+)", raw)
        if m_idx:
            current_slot = int(m_idx.group(1))
            current_comment = raw.strip()
            continue
        m = re.search(r"\.word\s+([A-Za-z_][A-Za-z0-9_]*)\s*,", code)
        if m and current_slot is not None:
            sym = m.group(1)
            kind = "traffic_or_drone"
            if current_slot in (0, 1, 2, 3):
                kind = "player_visible"
            elif current_slot in (15, 16, 17):
                kind = "player_override"
            elif sym in {"jeep", "copcar", "gtruck", "sbus", "sbusp", "copcarp", "gtruckp"}:
                kind = "hidden_or_special"
            slots.append(VehicleSlot(
                slot=current_slot,
                symbol=sym,
                display_name=display_names.get(sym, sym),
                kind=kind,
                comment=current_comment,
            ))
            current_slot = None
    if not slots:
        raise RuntimeError("Could not parse VEHICLE_TABLE entries from source")
    return sorted(slots, key=lambda x: x.slot)


def find_compiled_vehicle_table(program_words: Sequence[int], slot_count: int) -> int:
    signature = [1, 0, 1, 3, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0][:slot_count]
    span = 11 * slot_count
    hits: List[int] = []
    for i in range(0, len(program_words) - span):
        ok = True
        for ent, count in enumerate(signature):
            if program_words[i + ent * 11 + 5] != count:
                ok = False
                break
        if ok:
            hits.append(i)
    if not hits:
        raise RuntimeError("Compiled VEHICLE_TABLE not found in u10_13")
    hits.sort(key=lambda i: (sum(1 for e in range(slot_count) if 0xC00000 <= program_words[i + e * 11] <= 0xC7FFFF), -i), reverse=True)
    return hits[0]


def word_addr_to_u10_off(word_addr: int) -> Optional[int]:
    if 0xC00000 <= word_addr <= 0xC7FFFF:
        return (word_addr - 0xC00000) * 4
    return None


def decode_vehicle_roots(program_data: bytes, slots: Sequence[VehicleSlot]) -> Tuple[int, List[VehicleRoot]]:
    words = struct.unpack("<" + "I" * (len(program_data) // 4), program_data)
    table_word_index = find_compiled_vehicle_table(words, len(slots))
    roots: List[VehicleRoot] = []
    for idx, slot in enumerate(slots):
        base = table_word_index + idx * 11
        model = words[base + 0]
        pal = words[base + 1]
        ani = words[base + 2]
        d1 = words[base + 3]
        d2 = words[base + 4]
        roots.append(VehicleRoot(
            slot=slot.slot,
            symbol=slot.symbol,
            display_name=slot.display_name,
            kind=slot.kind,
            model_word_addr=model,
            palette_word=pal,
            ani_word_addr=ani,
            degrade1_word_addr=d1,
            degrade2_word_addr=d2,
            model_byte_off_u10_13=word_addr_to_u10_off(model) or -1,
            degrade1_byte_off_u10_13=word_addr_to_u10_off(d1),
            degrade2_byte_off_u10_13=word_addr_to_u10_off(d2),
        ))
    return table_word_index * 4, roots


def parse_static_model(data: bytes, word_addr: int, label: str) -> Tuple[ParsedModel, List[Tuple[int, int, int]], List[Dict[str, object]]]:
    off = word_addr_to_u10_off(word_addr)
    if off is None or off < 0 or off + 8 > len(data):
        pm = ParsedModel(label, word_addr, off or -1, 0, 0, 0, (0, 0, 0), (0, 0, 0), [], 0, False, "word address not inside u10_13")
        return pm, [], []

    radius, header = struct.unpack_from("<II", data, off)
    vcnt = (header & 0xFF) + 1
    pcnt = ((header >> 16) & 0xFFFF) + 1
    if vcnt <= 0 or vcnt > 4096:
        pm = ParsedModel(label, word_addr, off, radius, vcnt, pcnt, (0, 0, 0), (0, 0, 0), [], 0, False, f"implausible vertex count {vcnt}")
        return pm, [], []
    if pcnt <= 0 or pcnt > 8192:
        pm = ParsedModel(label, word_addr, off, radius, vcnt, pcnt, (0, 0, 0), (0, 0, 0), [], 0, False, f"implausible polygon count {pcnt}")
        return pm, [], []

    ptr = off + 8
    need = ptr + vcnt * 8 + pcnt * 20
    if need > len(data):
        pm = ParsedModel(label, word_addr, off, radius, vcnt, pcnt, (0, 0, 0), (0, 0, 0), [], max(0, len(data) - off), False, "model overruns u10_13")
        return pm, [], []

    verts: List[Tuple[int, int, int]] = []
    xs: List[int] = []
    ys: List[int] = []
    zs: List[int] = []
    for i in range(vcnt):
        xy, z = struct.unpack_from("<Ii", data, ptr + i * 8)
        x = s16(xy & 0xFFFF)
        y = s16((xy >> 16) & 0xFFFF)
        verts.append((x, y, z))
        xs.append(x)
        ys.append(y)
        zs.append(z)

    polys: List[Dict[str, object]] = []
    texpages: List[int] = []
    pptr = ptr + vcnt * 8
    valid_poly_count = 0
    for i in range(pcnt):
        w0, w1, w2, w3, w4 = struct.unpack_from("<IIIII", data, pptr + i * 20)
        idx = [w1 & 0xFF, (w1 >> 8) & 0xFF, (w1 >> 16) & 0xFF, (w1 >> 24) & 0xFF]
        if all(v < vcnt for v in idx):
            valid_poly_count += 1
        uv_bytes = list(struct.pack("<II", w2, w3))
        uvs = [
            (uv_bytes[0], uv_bytes[1]),
            (uv_bytes[2], uv_bytes[3]),
            (uv_bytes[4], uv_bytes[5]),
            (uv_bytes[6], uv_bytes[7]),
        ]
        polys.append({
            "w0": w0,
            "indices": idx,
            "uvs": uvs,
            "texpage": w4,
        })
        texpages.append(w4)

    valid = valid_poly_count == pcnt
    pm = ParsedModel(
        label=label,
        word_addr=word_addr,
        byte_off=off,
        radius=radius,
        vertex_count=vcnt,
        polygon_count=pcnt,
        bbox_min=(min(xs), min(ys), min(zs)),
        bbox_max=(max(xs), max(ys), max(zs)),
        texpage_values=sorted(set(texpages)),
        exact_bytes=8 + vcnt * 8 + pcnt * 20,
        valid=valid,
        invalid_reason="" if valid else f"{valid_poly_count}/{pcnt} polygons had in-range vertex indices",
    )
    return pm, verts, polys


def build_atlas_plan(polys: Sequence[Dict[str, object]], atlas_width: int = 256, row_bias: int = 0, base_row: Optional[int] = None) -> AtlasPlan:
    if not polys:
        return AtlasPlan(width=atlas_width, height=1, base_row=0, min_page=0, max_page=0, row_bias=row_bias, local_u_max=0, local_v_max=0, pages=[])

    page_stats: Dict[int, Dict[str, int]] = {}
    local_u_max = 0
    local_v_max = 0
    for poly in polys:
        page = int(poly["texpage"]) + row_bias
        us = [int(u) for u, _ in poly["uvs"]]
        vs = [int(v) for _, v in poly["uvs"]]
        local_u_max = max(local_u_max, max(us))
        local_v_max = max(local_v_max, max(vs))
        if page not in page_stats:
            page_stats[page] = {
                "page": page,
                "count": 0,
                "umin": min(us),
                "umax": max(us),
                "vmin": min(vs),
                "vmax": max(vs),
            }
        st = page_stats[page]
        st["count"] += 1
        st["umin"] = min(st["umin"], min(us))
        st["umax"] = max(st["umax"], max(us))
        st["vmin"] = min(st["vmin"], min(vs))
        st["vmax"] = max(st["vmax"], max(vs))

    min_page = min(page_stats)
    max_page = max(page_stats)
    if base_row is None:
        base_row = min_page

    pages: List[Dict[str, int]] = []
    atlas_height = 1
    for page in sorted(page_stats):
        st = dict(page_stats[page])
        st["row0"] = page - base_row
        st["row1"] = st["row0"] + st["vmax"]
        atlas_height = max(atlas_height, st["row1"] + 1)
        pages.append(st)

    return AtlasPlan(
        width=atlas_width,
        height=atlas_height,
        base_row=base_row,
        min_page=min_page,
        max_page=max_page,
        row_bias=row_bias,
        local_u_max=local_u_max,
        local_v_max=local_v_max,
        pages=pages,
    )


def _uv_norm(pixel: int, denom_mode: str, axis_extent: int) -> float:
    if denom_mode == "255":
        return pixel / 255.0
    if denom_mode == "256":
        return pixel / 256.0
    # image_extent mode: normalize by actual atlas span minus 1 when possible.
    if axis_extent <= 1:
        return 0.0
    return pixel / float(axis_extent - 1)


def _poly_face(poly: Dict[str, object]) -> Tuple[List[int], List[Tuple[int, int]]]:
    idx = list(poly["indices"])
    uv_pairs = list(poly["uvs"])
    if idx[2] == idx[3]:
        return [idx[0], idx[1], idx[2]], [uv_pairs[0], uv_pairs[1], uv_pairs[2]]
    return idx[:], uv_pairs[:]


def write_obj_mtl(
    out_base: Path,
    name: str,
    parsed: ParsedModel,
    verts: Sequence[Tuple[int, int, int]],
    polys: Sequence[Dict[str, object]],
    atlas_plan: AtlasPlan,
    uv_mode: str,
    vflip: bool,
    denom_mode: str,
    single_material: bool,
) -> Dict[str, object]:
    obj_path = out_base / f"{name}.obj"
    mtl_path = out_base / f"{name}.mtl"

    material_names: Dict[int, str] = {}
    with mtl_path.open("w", encoding="utf-8") as mtl:
        if single_material:
            mtl.write("newmtl vehicle_atlas\n")
            mtl.write("Ka 1.000 1.000 1.000\n")
            mtl.write("Kd 1.000 1.000 1.000\n")
            mtl.write("Ks 0.000 0.000 0.000\n\n")
            for tex in sorted({int(p['texpage']) for p in polys}):
                material_names[tex] = "vehicle_atlas"
        else:
            for tex in sorted({int(p['texpage']) for p in polys}):
                mat = f"texpage_{tex:08X}"
                material_names[tex] = mat
                mtl.write(f"newmtl {mat}\n")
                mtl.write("Ka 1.000 1.000 1.000\n")
                mtl.write("Kd 1.000 1.000 1.000\n")
                mtl.write("Ks 0.000 0.000 0.000\n\n")

    vt_index = 1
    vt_map: Dict[Tuple[int, int, int], int] = {}
    uv_lines: List[str] = []
    faces_written = 0
    tris_written = 0
    quads_written = 0

    atlas_h = max(1, atlas_plan.height)
    atlas_w = max(1, atlas_plan.width)

    with obj_path.open("w", encoding="utf-8") as obj:
        obj.write(f"mtllib {mtl_path.name}\n")
        obj.write(f"o {name}\n")
        for x, y, z in verts:
            obj.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")

        current_mat: Optional[str] = None
        for poly in polys:
            texpage = int(poly["texpage"])
            mat = material_names.get(texpage, "vehicle_atlas" if single_material else f"texpage_{texpage:08X}")
            if mat != current_mat:
                obj.write(f"usemtl {mat}\n")
                current_mat = mat

            face_indices, face_uvs = _poly_face(poly)
            if len(face_indices) == 3:
                tris_written += 1
            else:
                quads_written += 1

            vt_refs: List[int] = []
            for u8, v8 in face_uvs:
                if uv_mode == "stacked":
                    px = int(u8)
                    py = (texpage + atlas_plan.row_bias - atlas_plan.base_row) + int(v8)
                    key = (px, py, 1)
                    if key not in vt_map:
                        u = _uv_norm(px, denom_mode, atlas_w)
                        v_raw = _uv_norm(py, denom_mode if denom_mode != "255" and denom_mode != "256" else "image_extent", atlas_h)
                        v = (1.0 - v_raw) if vflip else v_raw
                        vt_map[key] = vt_index
                        uv_lines.append(f"vt {u:.6f} {v:.6f}\n")
                        vt_index += 1
                    vt_refs.append(vt_map[key])
                else:
                    px = int(u8)
                    py = int(v8)
                    key = (px, py, 0)
                    if key not in vt_map:
                        u = _uv_norm(px, denom_mode, 256)
                        v_raw = _uv_norm(py, denom_mode, 256)
                        v = (1.0 - v_raw) if vflip else v_raw
                        vt_map[key] = vt_index
                        uv_lines.append(f"vt {u:.6f} {v:.6f}\n")
                        vt_index += 1
                    vt_refs.append(vt_map[key])

            faces_written += 1
            refs = [f"{vi + 1}/{ti}" for vi, ti in zip(face_indices, vt_refs)]
            obj.write("f " + " ".join(refs) + "\n")

    content = obj_path.read_text(encoding="utf-8")
    lines = content.splitlines(True)
    insert_at = 2 + len(verts)
    lines[insert_at:insert_at] = uv_lines
    obj_path.write_text("".join(lines), encoding="utf-8")

    return {
        "obj": obj_path.name,
        "mtl": mtl_path.name,
        "faces_written": faces_written,
        "tri_faces": tris_written,
        "quad_faces": quads_written,
        "uv_count": len(vt_map),
        "uv_mode": uv_mode,
        "atlas_width": atlas_plan.width,
        "atlas_height": atlas_plan.height,
        "atlas_base_row": atlas_plan.base_row,
    }


def write_svg_guide(path: Path, plan: AtlasPlan) -> None:
    scale_x = 2
    scale_y = 1
    width_px = max(256, plan.width * scale_x + 240)
    height_px = max(64, plan.height * scale_y + 40)
    lines: List[str] = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">')
    lines.append('<rect x="0" y="0" width="100%" height="100%" fill="#111"/>')
    lines.append(f'<rect x="20" y="20" width="{plan.width*scale_x}" height="{plan.height*scale_y}" fill="#1d1d1d" stroke="#888" stroke-width="1"/>')
    colors = ["#e57373", "#64b5f6", "#81c784", "#ffd54f", "#ba68c8", "#4db6ac", "#ff8a65", "#90a4ae"]
    for i, page in enumerate(plan.pages):
        color = colors[i % len(colors)]
        x = 20 + page["umin"] * scale_x
        y = 20 + page["row0"] * scale_y
        w = max(1, (page["umax"] - page["umin"] + 1) * scale_x)
        h = max(1, (page["vmax"] - page["vmin"] + 1) * scale_y)
        # Page band outline.
        lines.append(f'<rect x="20" y="{20 + page["row0"] * scale_y}" width="{plan.width*scale_x}" height="{max(1, (page["vmax"] + 1) * scale_y)}" fill="none" stroke="{color}" stroke-opacity="0.35" stroke-width="1"/>')
        # Used UV rect.
        lines.append(f'<rect x="{x}" y="{20 + (page["row0"] + page["vmin"]) * scale_y}" width="{w}" height="{h}" fill="{color}" fill-opacity="0.25" stroke="{color}" stroke-width="1"/>')
        tx = 30 + plan.width * scale_x
        ty = 35 + i * 16
        lines.append(f'<text x="{tx}" y="{ty}" fill="{color}" font-family="monospace" font-size="12">page 0x{page["page"]:X} rows {page["row0"]}-{page["row1"]} uv x {page["umin"]}-{page["umax"]} y {page["vmin"]}-{page["vmax"]} n={page["count"]}</text>')
    lines.append('</svg>')
    path.write_text("\n".join(lines), encoding="utf-8")


def gather_source_uv_proof(source_tree: Path) -> Dict[str, object]:
    repo = SourceRepo(source_tree)
    try:
        widths: List[int] = []
        tga_headers: Dict[str, Dict[str, int]] = {}
        for name, w, h in repo.iter_tga_headers():
            widths.append(w)
            tga_headers[name.upper()] = {"width": w, "height": h}

        objects_equ = None
        if repo.zf is not None:
            candidates = [n for n in repo.names if n.upper().endswith("OBJECTS.EQU")]
            candidates.sort(key=lambda n: (n.count("/"), len(n)))
            if candidates:
                objects_equ = repo.zf.read(candidates[0]).decode("utf-8", errors="ignore")
        else:
            candidates = sorted(repo.src.rglob("OBJECTS.EQU"), key=lambda p: (len(p.relative_to(repo.src).parts), len(str(p))))
            if candidates:
                objects_equ = candidates[0].read_text(encoding="utf-8", errors="ignore")
        if objects_equ is None:
            raise FileNotFoundError("OBJECTS.EQU")

        sym_map: Dict[str, int] = {}
        for line in objects_equ.splitlines():
            m = re.search(r"\b([A-Za-z0-9_]+)\s+\.set\s+0?([0-9A-Fa-f]+)h\b", line)
            if m:
                sym_map[m.group(1)] = int(m.group(2), 16)

        chain: List[Dict[str, object]] = []
        chain_syms = [
            ("h2p1_I", "P1.TGA"),
            ("redhd1_I", "REDHD1.TGA"),
            ("big2_I", "BIG2.TGA"),
        ]
        prev_addr: Optional[int] = None
        prev_name: Optional[str] = None
        for sym, tga in chain_syms:
            addr = sym_map.get(sym)
            hdr = tga_headers.get(tga.upper())
            entry: Dict[str, object] = {
                "symbol": sym,
                "tga": tga,
                "addr": addr,
                "width": hdr["width"] if hdr else None,
                "height": hdr["height"] if hdr else None,
            }
            if prev_addr is not None and addr is not None and prev_name is not None:
                prev_h = tga_headers.get(prev_name.upper(), {}).get("height")
                entry["delta_from_prev"] = addr - prev_addr
                entry["prev_height"] = prev_h
                entry["delta_matches_prev_height"] = (prev_h is not None and (addr - prev_addr) == prev_h)
            chain.append(entry)
            prev_addr = addr
            prev_name = tga

        return {
            "all_source_tga_widths": sorted(set(widths)),
            "known_image_chain": chain,
            "objects_equ_symbol_count": len(sym_map),
        }
    finally:
        repo.close()


def write_reports(
    out_dir: Path,
    table_off: int,
    roots: Sequence[VehicleRoot],
    parsed_models: Sequence[ParsedModel],
    exports: Sequence[Dict[str, object]],
    atlas_hints: Dict[str, AtlasPlan],
    source_proof: Dict[str, object],
) -> None:
    manifest_json = out_dir / "manifest.json"
    manifest_csv = out_dir / "manifest.csv"
    bridge_json = out_dir / "source_bridge.json"
    atlas_json = out_dir / "atlas_hints.json"
    report_txt = out_dir / "scan_report.txt"

    by_label = {m.label: m for m in parsed_models}
    by_export = {e["label"]: e for e in exports}

    manifest_json.write_text(json.dumps([asdict(m) for m in parsed_models], indent=2), encoding="utf-8")
    bridge_json.write_text(json.dumps({
        "compiled_vehicle_table_byte_off_u10_13": table_off,
        "roots": [asdict(r) for r in roots],
        "source_uv_proof": source_proof,
    }, indent=2), encoding="utf-8")
    atlas_json.write_text(json.dumps({k: asdict(v) for k, v in atlas_hints.items()}, indent=2), encoding="utf-8")

    with manifest_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "label", "slot", "symbol", "display_name", "kind", "word_addr_hex", "byte_off_hex",
            "radius", "vertex_count", "polygon_count", "valid", "invalid_reason",
            "uv_mode", "atlas_width", "atlas_height", "atlas_base_row_hex",
            "obj", "mtl", "faces_written", "tri_faces", "quad_faces", "uv_count", "texpages"
        ])
        for r in roots:
            for suffix, addr in [("base", r.model_word_addr), ("degrade1", r.degrade1_word_addr), ("degrade2", r.degrade2_word_addr)]:
                if addr == 0:
                    continue
                label = f"slot{r.slot:02d}_{sanitize_name(r.symbol)}_{suffix}"
                m = by_label[label]
                e = by_export.get(label, {})
                plan = atlas_hints.get(label)
                w.writerow([
                    label, r.slot, r.symbol, r.display_name, r.kind, hex(m.word_addr), hex(m.byte_off),
                    m.radius, m.vertex_count, m.polygon_count, m.valid, m.invalid_reason,
                    e.get("uv_mode", ""), e.get("atlas_width", ""), e.get("atlas_height", ""), hex(plan.base_row) if plan else "",
                    e.get("obj", ""), e.get("mtl", ""), e.get("faces_written", ""), e.get("tri_faces", ""), e.get("quad_faces", ""), e.get("uv_count", ""),
                    ";".join(hex(t) for t in m.texpage_values),
                ])

    lines: List[str] = []
    lines.append("Cruis'n USA vehicle extraction report\n")
    lines.append("===================================\n\n")
    lines.append(f"Compiled VEHICLE_TABLE in u10_13: 0x{table_off:06X}\n")
    lines.append("\nUV reconstruction proof from source:\n")
    lines.append(f"- All source TGAs discovered in the uploaded source are width(s): {source_proof.get('all_source_tga_widths')}\n")
    for item in source_proof.get("known_image_chain", []):
        lines.append(
            f"- {item['symbol']} addr=0x{item['addr']:X} {item['tga']} size={item['width']}x{item['height']}"
        )
        if "delta_from_prev" in item:
            lines.append(
                f" delta_from_prev={item['delta_from_prev']} prev_height={item['prev_height']} match={item['delta_matches_prev_height']}"
            )
        lines.append("\n")
    lines.append("This proves the runtime texture address advances in row units inside a vertically stacked 256-wide atlas.\n\n")

    for r in roots:
        lines.append(f"slot {r.slot:02d} {r.symbol} ({r.display_name}) [{r.kind}]\n")
        for suffix, addr in [("base", r.model_word_addr), ("degrade1", r.degrade1_word_addr), ("degrade2", r.degrade2_word_addr)]:
            if addr == 0:
                continue
            label = f"slot{r.slot:02d}_{sanitize_name(r.symbol)}_{suffix}"
            m = by_label[label]
            plan = atlas_hints.get(label)
            lines.append(
                f"  {suffix:8s} word=0x{addr:06X} byte_off_u10_13=0x{m.byte_off:06X} radius={m.radius} verts={m.vertex_count} polys={m.polygon_count} valid={m.valid}\n"
            )
            if plan is not None:
                lines.append(
                    f"           atlas width={plan.width} height={plan.height} base_row=0x{plan.base_row:X} min_page=0x{plan.min_page:X} max_page=0x{plan.max_page:X} local_umax={plan.local_u_max} local_vmax={plan.local_v_max}\n"
                )
            if m.invalid_reason:
                lines.append(f"           reason: {m.invalid_reason}\n")
        lines.append("\n")
    report_txt.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract Cruis'n USA vehicle geometry with stacked-atlas UV reconstruction")
    ap.add_argument("input", help="Path to maindata ZIP")
    ap.add_argument("--source", required=True, help="Path to Cruis'n USA source ZIP or extracted source directory")
    ap.add_argument("-o", "--out", default="cruisnusa_vehicle_out", help="Output directory")
    ap.add_argument("--include-degraded", action="store_true", help="Also export degrade1/degrade2 model roots when present")
    ap.add_argument("--uv-mode", choices=["stacked", "local"], default="stacked", help="UV export mode")
    ap.add_argument("--atlas-width", type=int, default=256, help="Atlas width in pixels for stacked mode")
    ap.add_argument("--base-row", default="auto", help="Atlas base row for stacked mode: auto or integer/hex")
    ap.add_argument("--row-bias", type=int, default=0, help="Additive bias applied to each runtime texpage row before stacking")
    ap.add_argument("--uv-denom", choices=["255", "256", "image_extent"], default="image_extent", help="UV normalization denominator strategy")
    ap.add_argument("--no-vflip", action="store_true", help="Disable V flip for UVs")
    ap.add_argument("--multi-material", action="store_true", help="Keep one material per texpage instead of one atlas material")
    ap.add_argument("--no-guides", action="store_true", help="Disable SVG atlas guide output")
    args = ap.parse_args()

    out_dir = Path(args.out)
    export_dir = out_dir / "objs"
    guide_dir = out_dir / "atlas_guides"
    export_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_guides:
        guide_dir.mkdir(parents=True, exist_ok=True)

    romset = RomSet(Path(args.input))
    try:
        program_data = romset.build_u10_13()
    finally:
        romset.close()

    slots = parse_source_vehicle_slots(Path(args.source))
    table_off, roots = decode_vehicle_roots(program_data, slots)
    source_proof = gather_source_uv_proof(Path(args.source))

    parsed_models: List[ParsedModel] = []
    exports: List[Dict[str, object]] = []
    atlas_hints: Dict[str, AtlasPlan] = {}

    for root in roots:
        entries = [("base", root.model_word_addr)]
        if args.include_degraded:
            if root.degrade1_word_addr:
                entries.append(("degrade1", root.degrade1_word_addr))
            if root.degrade2_word_addr:
                entries.append(("degrade2", root.degrade2_word_addr))

        for suffix, addr in entries:
            label = f"slot{root.slot:02d}_{sanitize_name(root.symbol)}_{suffix}"
            parsed, verts, polys = parse_static_model(program_data, addr, label)
            parsed_models.append(parsed)
            if not parsed.valid:
                continue

            base_row_override: Optional[int]
            if args.base_row == "auto":
                base_row_override = None
            else:
                base_row_override = int(args.base_row, 0)
            plan = build_atlas_plan(polys, atlas_width=args.atlas_width, row_bias=args.row_bias, base_row=base_row_override)
            atlas_hints[label] = plan

            exp = write_obj_mtl(
                export_dir,
                label,
                parsed,
                verts,
                polys,
                atlas_plan=plan,
                uv_mode=args.uv_mode,
                vflip=not args.no_vflip,
                denom_mode=args.uv_denom,
                single_material=not args.multi_material,
            )
            exp["label"] = label
            exports.append(exp)

            if not args.no_guides:
                write_svg_guide(guide_dir / f"{label}.svg", plan)

    write_reports(out_dir, table_off, roots, parsed_models, exports, atlas_hints, source_proof)
    print(f"Wrote outputs to: {out_dir}")


if __name__ == "__main__":
    main()
