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
| Color sensor | AR0234CS, **3.0 um** pixel, 1920x1200, 1/2.6 in | `e-con_See3CAM_24CUG_datasheet.pdf` sensor table (1/2.6" optical format, 3 um x 3 um, 1920H x 1200V) — verified |
| Mono sensor | IMX900, **2.25 um** pixel, 2064x1552, 1/3.1 in | `e-con_See3CAM_37CUGM_Datasheet.pdf` Table 2 (1/3.1" optical format, 2.25 um, active array 2064 x 1552) — verified |
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
**CoC assumption: 2 px of defocus blur** — color c = 2 x 3.0 um = **6.0 um**, mono c = 2 x 2.25 um = **4.5 um** (the mono's smaller pixels make it the tighter arm). Thin-lens formulas, s = focus distance:
```
H    = f^2 / (N*c) + f              # hyperfocal
near = s * (H - f) / (H + s - 2f)
far  = s * (H - f) / (H - s)        # inf if s >= H
```
At 8 mm f/2.8: H = 12.5 ft (color) / 16.7 ft (mono).

**Recommended focus: lock at ~5.5 ft** — deliberately NOT the 3-8 ft geometric mean (~4.9 ft). Focused at 5 ft, the 8 mm windows are 3.6 -> 8.3 ft (color) but only **3.9 -> 7.1 ft (mono)** — the mono arm never reaches the 8 ft far edge. Pushing focus out to 5.5 ft buys the mono its far edge for ~0.3 ft of near limit. DoF at **5.5 ft focus**, 2-px CoC:
| Focal | Color DoF | Mono DoF |
|---|---|---|
| 6 mm | 3.1 -> 25 ft | 3.5 -> 13 ft |
| **8 mm** | **3.8 -> 9.8 ft** | **4.1 -> 8.2 ft** |
| 12 mm | 4.6 -> 6.8 ft | 4.8 -> 6.4 ft |
| 16 mm | 5.0 -> 6.2 ft | 5.1 -> 6.0 ft |

At f/2.8, **12 mm and longer cannot span 3-8 ft** (DoF window narrower than the working range). This is why the long lenses that the dense-2 in codes would need are physically incompatible with this range at this fixed aperture. 8 mm is the practical ceiling; **focus ~5.5 ft and lock** (Sec 9 matches).

**Residual uncovered band (stated honestly):** at 5.5 ft focus the 8 mm 2-px-sharp window misses the near zone — **3 -> ~3.8 ft (color) and 3 -> ~4.1 ft (mono)** run at >2 px blur. A 3-px CoC tolerance pulls the near limits in to ~3.3 ft (color) / ~3.7 ft (mono), and the near zone carries the most sampling headroom (14.6/19.4 px/module at 3 ft, Sec 4), so decode should degrade gracefully there — verify live at 3-4 ft before sign-off.

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

## 8. Lighting / illumination target — speed-tiered (there is no single design band)
The blur budget sets the shutter, and the shutter sets the light. Blur < 0.5 module (= 2.5 mm at 5 mm modules) requires `t <= 2.5 mm / speed`: 2 mph -> <= 2.8 ms, 5 mph -> <= 1.12 ms, 10 mph -> <= 0.56 ms. Inverted: a 4 ms shutter only freezes ~1.4 mph, 3 ms ~1.9 mph, 2 ms ~2.8 mph, 1 ms ~5.6 mph, 0.5 ms ~11 mph. **So "1-4 ms" is NOT one design band — the 2-4 ms end is walking-pace-only, and 5-10 mph lives at 1 ms and below.** Tiered guidance:

| Speed tier | Freeze shutter | Slider (37CUGM manual Table 3) | Lux to buy (2x margin @ +12 dB) |
|---|---|---|---|
| <= ~2-3 mph (creep / walk-along) | <= 2.8 ms @ 2 mph | 2 ms (-9) | **~3,900 lux** |
| ~5 mph (typical forklift) | <= 1.12 ms | 1 ms (-10) | **~7,800 lux** |
| ~10 mph (fast lane) | <= 0.56 ms | 400 us (-11, nearest step) | **~15,600 lux** (at 0.5 ms; ~19,500 at the realizable 400 us) |

Every tier is **light-starved**; ambient will not do it. Scene illuminance on the code plane (f/2.8, 60% reflectance), derived from the rig anchor point:
| Shutter | Unity gain | At +12 dB gain (zxing tolerates noise) | With 2x margin (buy to this) |
|---|---|---|---|
| 0.5 ms | ~31,200 lux | ~7,800 lux | **~15,600 lux** |
| 1 ms | ~15,600 lux | ~3,900 lux | **~7,800 lux** |
| 2 ms | ~7,800 lux | ~1,950 lux | **~3,900 lux** |
| 3 ms | ~5,200 lux | ~1,300 lux | **~2,600 lux** |
| 4 ms | ~3,900 lux | ~980 lux | **~2,000 lux** |

**Design target — speed-honest; the purchase hinges on the actual lane speed, so measure it before buying:**
- **Walking-pace lane (<= ~2-3 mph): deliver ~2,000-4,000 lux** on the code at the lane offset (covers the 2-4 ms tiers with margin; gain absorbs the rest).
- **True 5-10 mph lane (the stated motion band): ~2,000-4,000 lux is 2-4x short.** Budget **~7,800 lux for 5 mph (1 ms)** and **~15,600 lux for 10 mph (0.5 ms)**.

Lighting parts + placement:
- **Type:** diffuse white LED **bar or ring**, 5000-6500 K, high-CRI; diffused (not bare point LEDs) to spread glare.
- **Placement:** mount **off-axis, >= 20-30 deg from the lens axis**, aimed at the code plane so the specular highlight reflects away from the sensor (directly addresses the `qr-needs-binarization` glare failure where QR only read after Otsu). Cross-light from two sides for placards that tilt.
- **Standoff/output:** size the fixture so the **on-code illuminance hits your speed tier's target above at the working distance** (a typical 24 in MV bar light or a pair of spotlights at ~1-3 ft standoff reaches the 2,000-4,000 lux walking-pace tier; the 7,800-15,600 lux 5-10 mph tiers generally take multiple or strobed fixtures). Confirm with the bench brightness readout, not the datasheet lux-at-source.
- **Spectrum:** keep it visible-band (or add IR-cut on the color lens) since the mono lens ships without IR-cut.

## 9. Software follow-up (config only, no code)
In `config/station.yaml`, after the swap: set `cameras[].settings.exposure` to the shutter your lane-speed tier requires (Sec 8 tier table; log2 slider mapping per 37CUGM manual Table 3): **-9 (2 ms)** for the walking-pace tier, **-10 (1 ms)** for ~5 mph, **-11 (400 us)** for ~10 mph — the cam-mono comments in `station.yaml` already name -9/-10 as the lit operating points. Tune `gain` to land whole-frame brightness in the bench [80,155] band, and keep auto-exposure frozen during motion. **Lock focus mechanically at ~5.5 ft** (Sec 5): 2-px-sharp over ~3.8-9.8 ft (color) / ~4.1-8.2 ft (mono), with the residual 3 -> ~4 ft near band at 2-3 px blur — verify decode there live. Optionally tighten `decode.payload_pattern` to your label format once read rate is confirmed.
