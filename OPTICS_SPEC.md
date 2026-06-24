# PalletScan Optics + Lighting Spec (buy-from)

## 0. Scope and binding constraint
The decoder sampling floor is the gate: **>= 5 px per code-module** (per the A/B finding in the prompt). This is stricter than the repo's synthetic envelope (`config/station.yaml` `synthetic.px_per_module_range: [3.0, 6.0]`, `module_size_mm: 5.0`) and the `optics-resolution-budget` memory (3-6 px floor). I gate on **5 px/module** as instructed; see Risks for the relaxation if the true floor is 3-4.

Formula used throughout (from the prompt):
```
px_per_module = focal_mm * module_mm / (distance_mm * pixel_um/1000)
```

## 1. Stated assumptions
| Item | Value | Source |
|---|---|---|
| Color sensor | AR0234CS, **3.0 um** pixel, 1920x1200, 1/2.6 in | prompt; lens DS confirms 1/2.6 in format |
| Mono sensor | IMX900, **2.25 um** pixel, 2064x1552, 1/3.1 in | prompt; 37CUGM DS confirms 1/3.1 in |
| Current lens (both) | **3.0 mm, f/2.8, fixed**, M12x0.5 S-mount, FOV 104.6H/128.2D (color), 89.3H/111.9D (mono) | `e-con_See3CAM_24CUG_Lens_Datasheet.pdf`, `e-con_See3CAM_37CUGM_Lens_Datasheet.pdf` |
| Mono lens IR-cut | **None** (stock 37CUGM lens has no IR-cut) | 37CUGM lens DS Table 1 |
| **Working distance** | **3-8 ft** (assumed; forklift lane offset) | assumed — STATE per prompt |
| **Production code** | **7 in x 7 in placard, 5 mm modules** | memory `optics-resolution-budget` (Brody's target) |
| Dense alt-code | 2 in QR: v2 (25 mod, 2.03 mm pitch) / v4 (33 mod, 1.54 mm pitch) | assumed module counts |
| Reflectance | 0.6 (matte white label) | assumed |
| Motion band | up to ~5-10 mph forklift, blur < 0.5 module | memory |
| 1 ft | 304.8 mm | — |

## 2. Why the 3 mm lens fails (px/module @ 3 mm, computed)
5 mm-module code (7 in production):
| Distance | Color (3.0 um) | Mono (2.25 um) |
|---|---|---|
| 3 ft | 5.5 | 7.3 |
| 5 ft | 3.3 | 4.4 |
| 8 ft | **2.1** | **2.7** |
| 10 ft | 1.6 | 2.2 |

Dense 2 in codes (1.5-2 mm pitch) are already **< 3 px/module at 3 ft** on both sensors — unreadable at any useful range. The 3 mm lens only clears 5 px/mod on a 5 mm-module code at <= ~3 ft (color) / ~4 ft (mono). This matches the live finding "2 in codes only read at <= 2-3 ft."

## 3. Required focal length for >= 5 px/module (solve f = 5 * dist_mm * pixel_um/1000 / module_mm)
Gating at the **far edge (8 ft)** of the 3-8 ft band:
| Code | Color (3.0 um) needs | Mono (2.25 um) needs |
|---|---|---|
| 7 in / 5 mm module | **f >= 7.3 mm** | **f >= 5.5 mm** |
| 2 in QR v2 (2.03 mm) | f >= 18.0 mm | f >= 13.5 mm |
| 2 in QR v4 (1.54 mm) | f >= 23.8 mm | f >= 17.8 mm |

The 2 in dense codes demand 14-24 mm glass, which at f/2.8 cannot hold depth of field across 3-8 ft (Sec 5) and gives a tiny FOV — **deprecate dense codes to >= 5 mm modules**.

## 4. Recommended lens: 8 mm M12, both bodies
A single **8 mm** part covers both sensors with margin and is the resolution/DoF sweet spot.

px/module at 8 mm, 5 mm module:
| Distance | Color | Mono |
|---|---|---|
| 3 ft | 14.6 | 19.4 |
| 5 ft | 8.7 | 11.7 |
| 8 ft | **5.5** | **7.3** |
| 10 ft | 4.4 | 5.8 |

Both clear 5 px/mod across the full 3-8 ft band (mono with comfortable headroom; color exactly at the 8 ft edge — if you want margin at 8 ft on the color body, see the 10 mm alt below). A 6 mm lens clears 5 px/mod for mono (5.5 @ 8 ft) but the **color body falls to 4.1 @ 8 ft** — so 6 mm is mono-only; **8 mm is the common part**.

Alt for a sharper color far edge: **10 mm** -> color 5.5 @ 10 ft, but DoF gets razor-thin (Sec 5). Stick with 8 mm unless you can also shrink the working distance.

## 5. Depth of field at f/2.8 (fixed aperture, no iris) — the upper bound on focal length
CoC = 2 px; focus set at geometric mean ~5 ft:
| Focal | Color DoF | Mono DoF |
|---|---|---|
| 6 mm | 3.5 ft -> inf | 4.0 -> 28 ft |
| **8 mm** | **4.5 -> 16 ft** | **5.0 -> 12 ft** |
| 12 mm | 5.7 -> 9.4 ft | 6.0 -> 8.7 ft |
| 16 mm | 6.2 -> 8.2 ft | 6.4 -> 7.9 ft |

At f/2.8, **12 mm and longer cannot span 3-8 ft** (DoF window narrower than the working range). This is why the long lenses that the dense-2 in codes would need are physically incompatible with this range at this fixed aperture. 8 mm is the practical ceiling; focus ~5 ft and lock. (Note the near edge at 3 ft sits just inside the 8 mm near-DoF limit ~4.5 ft for color at 2-px CoC; a 3-px CoC tolerance pulls the near limit in to ~3 ft. Verify live.)

## 6. FOV sanity (active sensor width: color 5.76 mm, mono 4.64 mm) at 8 mm
| Distance | Color H-FOV | Mono H-FOV |
|---|---|---|
| 3 ft | 2.2 ft | 1.7 ft |
| 5 ft | 3.6 ft | 2.9 ft |
| 8 ft | 5.8 ft | 4.6 ft |
Confirm the forklift lane keeps the code within this width at the near edge (mono is the tighter of the two).

## 7. Lens parts to buy
- **Focal length: 8 mm** (both cameras; common part).
- **Mount: M12 x 0.5 (S-mount / board lens)** — matches both bodies (lens DS: "Lens Barrel Thread M12 x 0.5").
- **f-number: f/2.8** fixed (no iris needed; matches current and the DoF math above).
- **Image circle:** >= 1/2.6 in for color body; >= 1/3.1 in covers mono. Buy one lens rated for >= 1/2.5 in to serve both.
- **Resolution rating:** >= 3 MP, low-distortion (must resolve 3.0 um pixels; the stock wide lens is <35% distortion which corner-warps codes — pick a low-distortion MV lens).
- **Color body:** add/confirm an **IR-cut** (stock mono lens is explicitly without IR-cut; for the AR0234 color arm use an IR-cut lens or visible-band LEDs to keep contrast/color clean).
- Examples to source by these params (verify in hand): e-con's own 8 mm M12 option, or Computar/Tamron/Edmund M12 8 mm f/2.8 >=3 MP low-distortion.

## 8. Lighting / illumination target
The 1-4 ms shutter that forklift motion requires is **light-starved**; ambient will not do it. Motion-freeze cross-check (blur < 0.5 module, 5 mm module): 2 mph -> <= 2.8 ms, 5 mph -> <= 1.12 ms, 10 mph -> <= 0.56 ms. So **1-4 ms is the correct design band for ~5-10 mph.**

Scene illuminance on the code plane (f/2.8, 60% reflectance), derived from the rig anchor point:
| Shutter | Unity gain | At +12 dB gain (zxing tolerates noise) | With 2x margin (buy to this) |
|---|---|---|---|
| 1 ms | ~15,600 lux | ~3,900 lux | **~7,800 lux** |
| 2 ms | ~7,800 lux | ~1,950 lux | **~3,900 lux** |
| 3 ms | ~5,200 lux | ~1,300 lux | **~2,600 lux** |
| 4 ms | ~3,900 lux | ~980 lux | **~2,000 lux** |

**Design target: deliver ~2,000-4,000 lux on the code at the lane offset** (covers 2-4 ms with margin; gain absorbs the rest). For a hard 1 ms freeze at 10 mph, target ~7,800 lux.

Lighting parts + placement:
- **Type:** diffuse white LED **bar or ring**, 5000-6500 K, high-CRI; diffused (not bare point LEDs) to spread glare.
- **Placement:** mount **off-axis, >= 20-30 deg from the lens axis**, aimed at the code plane so the specular highlight reflects away from the sensor (directly addresses the `qr-needs-binarization` glare failure where QR only read after Otsu). Cross-light from two sides for placards that tilt.
- **Standoff/output:** size the fixture so the **on-code illuminance hits 2,000-4,000 lux at the working distance** (a typical 24 in MV bar light or a pair of spotlights at ~1-3 ft standoff reaches this). Confirm with the bench brightness readout, not the datasheet lux-at-source.
- **Spectrum:** keep it visible-band (or add IR-cut on the color lens) since the mono lens ships without IR-cut.

## 9. Software follow-up (config only, no code)
In `config/station.yaml`, after the swap: set `cameras[].settings.exposure` to the lit 1-4 ms operating point (color ~ -8/-9, mono ~ -8 per memory log2 mapping), tune `gain` to land whole-frame brightness in the bench [80,155] band, and keep auto-exposure frozen during motion. Lock focus mechanically at ~5 ft. Optionally tighten `decode.payload_pattern` to your label format once read rate is confirmed.
