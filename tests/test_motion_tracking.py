"""TIER 2 per-object multi-object tracking (motion.tracking == "multi").

The default ("single") path is exercised exhaustively in test_motion_gate.py;
here we (a) pin that the default is byte-for-byte the old behavior (tracks
always empty), and (b) drive the multi path: two concurrent objects yield two
candidate ids, identity survives a crossing, brief merges do not churn ids, a
sustained merge re-emits a CLOSE so the absorbed track's miss still finalizes,
noise specks and the object cap are honored, and break closes all open tracks.

Note on the fixtures: a SOLID square moving by N px frame-diffs only at its
leading/trailing edges (its interior is unchanged), so a fast solid square
fractures into two edge blobs. The objects here oscillate brightness every
frame (``_val``) so the WHOLE interior diffs — a single stable blob per object
— which isolates the association logic from frame-diff edge artifacts.
"""

from __future__ import annotations

import numpy as np

from palletscan.config import MotionConfig
from palletscan.pipeline.motion_gate import MotionGate
from palletscan.types import Frame, SegmentKind

H, W = 360, 640
BG = 90


def _frame(image: np.ndarray, idx: int) -> Frame:
    return Frame(image=image, ts=idx / 30.0, frame_index=idx, source_id="cam0")


def _blank() -> np.ndarray:
    return np.full((H, W), BG, np.uint8)


def _val(i: int) -> int:
    """Fill value that toggles each frame so the object's interior diffs."""
    return 200 + (i % 2) * 40


def _square(img: np.ndarray, cx: int, cy: int, val: int, size: int = 80) -> None:
    half = size // 2
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(W, cx + half), min(H, cy + half)
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = val


def _multi_cfg(**kw) -> MotionConfig:
    base = dict(tracking="multi", open_frames=3, quiet_frames=5)
    base.update(kw)
    return MotionConfig(**base)


def _run(gate: MotionGate, frames: list[np.ndarray]):
    results, events = [], []
    for i, img in enumerate(frames):
        res, evs = gate.update(_frame(img, i))
        results.append(res)
        events.extend(evs)
    events.extend(gate.flush())
    return results, events


# -- default single mode is unchanged -----------------------------------------


def test_single_mode_is_unchanged_default() -> None:
    """MotionConfig() default is single mode: every MotionResult.tracks is the
    empty sentinel and the OPEN/CLOSE shape matches the historical path."""
    gate = MotionGate(MotionConfig(), "cam0", run_token="t0")
    frames = []
    for i in range(40):
        img = _blank()
        if 5 <= i < 25:
            _square(img, 60 + (i - 5) * 22, 180, _val(i), size=100)
        frames.append(img)
    results, events = _run(gate, frames)
    assert all(r.tracks == () for r in results)
    assert [e.kind for e in events] == [SegmentKind.OPEN, SegmentKind.CLOSE]
    assert events[0].candidate_id == events[1].candidate_id == "cam0-t0-000001"


# -- two concurrent objects ---------------------------------------------------


def _two_object_stream(n: int) -> list[np.ndarray]:
    """A travels left->right along the top band; B right->left along the bottom
    band. Well separated in y, so two stable blobs throughout."""
    frames = []
    for i in range(n):
        img = _blank()
        _square(img, 60 + i * 8, 110, _val(i))  # A, top band
        _square(img, W - 60 - i * 8, 250, _val(i))  # B, bottom band
        frames.append(img)
    return frames


def test_two_concurrent_objects_yield_two_candidate_ids() -> None:
    gate = MotionGate(_multi_cfg(), "cam0", run_token="t0")
    results, events = _run(gate, _two_object_stream(22))
    opens = [e for e in events if e.kind is SegmentKind.OPEN]
    assert len(opens) == 2, [e.candidate_id for e in events]
    assert len({e.candidate_id for e in opens}) == 2
    # At the peak there are two concurrently-open tracks in one frame.
    assert max(len(r.tracks) for r in results) == 2


def _crossing_stream(n: int) -> list[np.ndarray]:
    """Two objects whose centroids genuinely CROSS: A descends (top->bottom)
    and B rises (bottom->top) through the same vertical span. They ride two
    nearby columns (offset in x) so they pass each other as two distinct blobs
    rather than fusing — exercising association THROUGH a crossover where a
    naive nearest-blob match could swap the two ids."""
    frames = []
    ax, bx = 260, 380  # separate columns: blobs stay distinct as they pass
    for i in range(n):
        img = _blank()
        v = _val(i)
        k = i / (n - 1)
        ay = int(70 + (290 - 70) * k)  # A: top -> bottom
        by = int(290 - (290 - 70) * k)  # B: bottom -> top
        _square(img, ax, ay, v, size=70)
        _square(img, bx, by, v, size=70)
        frames.append(img)
    return frames


def test_crossing_objects_keep_identity() -> None:
    """Two objects move TOWARD each other and their y-centroid paths cross at
    mid-frame. Identity must survive the crossover: each track keeps a single
    id with a continuous (monotone) centroid trajectory — no swap. If the
    association swapped the two ids at the crossing (or fused both into one
    blob), at least one trajectory would reverse / a track would vanish."""
    cfg = _multi_cfg(quiet_frames=6, track_merge_hysteresis_frames=4)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    results, events = _run(gate, _crossing_stream(24))
    opens = [e for e in events if e.kind is SegmentKind.OPEN]
    assert len(opens) == 2, [e.candidate_id for e in events]
    assert len({e.candidate_id for e in opens}) == 2

    # Per-id y-centroid trajectory across the run (downscaled space).
    traj: dict[str, list[float]] = {}
    for r in results:
        for t in r.tracks:
            traj.setdefault(t.track_id, []).append(t.centroid[1])
    # Exactly the two opened ids are tracked, and each is seen across the whole
    # crossing (an id swap at the crossover would fragment a track into a fresh
    # id and a short stub).
    assert set(traj) == {e.candidate_id for e in opens}, traj
    assert all(len(ys) >= 18 for ys in traj.values()), {
        k: len(v) for k, v in traj.items()
    }
    # One id travels strictly down (y increasing), the other strictly up (y
    # decreasing), each end-to-end monotone (small tolerance for sampling).
    # A mid-crossing id swap reverses one of these trajectories -> NOT monotone.
    directions = []
    for ys in traj.values():
        inc = all(b >= a - 1.0 for a, b in zip(ys, ys[1:]))
        dec = all(b <= a + 1.0 for a, b in zip(ys, ys[1:]))
        assert inc or dec, f"trajectory reversed (identity swap): {ys}"
        # Each really traverses the span (not a near-flat parallel band).
        assert abs(ys[-1] - ys[0]) > 20.0, f"too small a sweep: {ys}"
        directions.append("up" if dec else "down")
    assert set(directions) == {"up", "down"}, directions


def _banded(merge_start: int, merge_stop: int, n: int) -> list[np.ndarray]:
    """Two objects move right together; in [merge_start, merge_stop) they share
    one y band (a single blob), otherwise they ride two separate bands."""
    frames = []
    for i in range(n):
        img = _blank()
        x = 50 + i * 8
        v = _val(i)
        if merge_start <= i < merge_stop:
            _square(img, x, 180, v)  # merged: one blob
        else:
            _square(img, x, 110, v)
            _square(img, x, 250, v)
        frames.append(img)
    return frames


def test_brief_merge_does_not_churn_ids() -> None:
    """A merge shorter than the hysteresis window must NOT commit: the two
    tracks survive as exactly two ids with no extra opens."""
    cfg = _multi_cfg(track_merge_hysteresis_frames=5, quiet_frames=8)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    _, events = _run(gate, _banded(8, 10, 24))  # 2-frame merge < hysteresis 5
    opens = [e for e in events if e.kind is SegmentKind.OPEN]
    assert len(opens) == 2, [e.candidate_id for e in events]
    assert len({e.candidate_id for e in opens}) == 2


def _contended_stream(n: int, conv: int, glide: int, gap: int = 24) -> list[np.ndarray]:
    """Two persistent DISTINCT blobs in tight contention range (side-by-side
    pallets): a big square (A) and a small square (B) open as two separate
    tracks, then B glides up beside A so the pair rides on as TWO distinct-
    but-tightly-overlapping blobs (a thin BG gap survives the dilation, so
    they stay two components). With overlapping padded ROIs and near
    centroids, every cross blob<->track pair stays gate-ELIGIBLE, yet each
    track keeps matching 1:1 to its own real blob every frame."""
    frames = []
    for i in range(n):
        img = _blank()
        v = _val(i)
        if i < conv:
            _square(img, 140, 120, v, size=50)  # B small, top-left
            _square(img, 500, 260, v, size=90)  # A big, bottom-right (far)
        else:
            k = min(1.0, (i - conv) / glide)
            bx = int(140 + (300 - 140) * k)  # B glides toward column x=300
            ax = int(500 + (300 - 500) * k)  # A glides toward column x=300
            ay = 120 + 25 + 45 + gap  # A settles a thin gap below B
            cay = int(260 + (ay - 260) * k)
            _square(img, bx, 120, v, size=50)  # B
            _square(img, ax, cay, v, size=90)  # A (larger)
        frames.append(img)
    return frames


def test_side_by_side_distinct_blobs_never_merge() -> None:
    """REVIEW_bringup_4d95b67 finding 9 (a)+(c): two tracks each matched 1:1
    to their OWN persistent distinct blob must never be treated as a merge,
    however long the cross pairs stay gate-eligible. The old detection ran on
    the FULL gate-eligible pair set, so side-by-side pallets accrued
    merge_streak (twice per frame with two mutually-contended blobs — halving
    the hysteresis window) until the smaller, correctly-tracked segment was
    force-closed mid-zone as "absorbed" (premature MissEvent) and its real
    blob was orphaned for that frame.

    JUSTIFICATION for replacing test_sustained_merge_commits_and_reemits: that
    test PINNED the buggy behavior — it drove the commit path with two
    persistent DISTINCT blobs (each track matched to its own real object) and
    required >2 opens, i.e. exactly the premature force-close finding 9
    condemns. The genuine merge-commit path (the blobs actually FUSING into
    one component) is pinned by
    test_genuine_merge_commit_closes_absorbed_track_after_hysteresis below."""
    # De-fragment CLOSE disabled so the two tightly-adjacent blobs stay two
    # distinct components (with it on, they would genuinely fuse).
    cfg = _multi_cfg(
        track_merge_hysteresis_frames=3, quiet_frames=6, track_close_kernel_frac=0.0
    )
    gate = MotionGate(cfg, "cam0", run_token="t0")
    frames = _contended_stream(30, 8, 6)
    results = []
    mid_events = []
    for i, img in enumerate(frames):
        res, evs = gate.update(_frame(img, i))
        results.append(res)
        mid_events.extend(evs)
    opens = [e for e in mid_events if e.kind is SegmentKind.OPEN]
    closes = [e for e in mid_events if e.kind is SegmentKind.CLOSE]
    assert len(opens) == 2, [e.candidate_id for e in mid_events]
    assert closes == [], "a track matched to its own distinct blob was merged away"
    # A track matched 1:1 to its own blob is never a merge candidate, so no
    # hysteresis ever accrues.
    assert all(t.merge_streak == 0 for t in gate._tracks.values())
    # Both objects keep updating through the whole contended tail...
    assert all(len(r.tracks) == 2 for r in results[-8:])
    # ...and both ride to the flush, where each gets its own CLOSE.
    tail = gate.flush()
    assert {e.candidate_id for e in tail} == {e.candidate_id for e in opens}


def _fusing_stream(n: int, fuse_at: int) -> list[np.ndarray]:
    """Two objects ride separate bands, then genuinely FUSE: from ``fuse_at``
    on, ONE blob replaces both (their masks merge into a single component —
    an actual merge, not mere side-by-side proximity)."""
    frames = []
    for i in range(n):
        img = _blank()
        x = 50 + i * 8
        v = _val(i)
        if i < fuse_at:
            _square(img, x, 110, v, size=90)  # A: larger -> the keeper
            _square(img, x, 250, v, size=56)  # B: smaller -> absorbed
        else:
            _square(img, x, 180, v, size=90)  # the fused single blob
        frames.append(img)
    return frames


def test_genuine_merge_commit_closes_absorbed_track_after_hysteresis() -> None:
    """REVIEW_bringup_4d95b67 finding 9 (b) + hysteresis timing: when the
    blobs ACTUALLY fuse, the losing track goes unmatched while its best
    gate-eligible blob is the fused (contended) one; after exactly
    track_merge_hysteresis_frames such frames the merge commits — the
    absorbed track gets its own CLOSE (its miss still finalizes, never
    silently folded into the survivor's segment) and the fused blob still
    updates the keeper THAT same frame. Pre-fix, the unmatched loser's
    merge_streak was reset every frame by the inactive path, so a genuine
    merge never committed and the loser lingered to the much-later quiet
    close."""
    hyst, quiet, fuse_at = 3, 8, 10
    cfg = _multi_cfg(track_merge_hysteresis_frames=hyst, quiet_frames=quiet)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    results = []
    timed: list[tuple[int, object]] = []
    for i, img in enumerate(_fusing_stream(24, fuse_at)):
        res, evs = gate.update(_frame(img, i))
        results.append(res)
        timed.extend((i, ev) for ev in evs)
    tail = gate.flush()

    opens = [(i, e) for i, e in timed if e.kind is SegmentKind.OPEN]
    closes = [(i, e) for i, e in timed if e.kind is SegmentKind.CLOSE]
    assert len(opens) == 2, [e.candidate_id for _, e in timed]
    # Exactly one mid-run CLOSE: the absorbed (smaller, second-opened) track,
    # emitted on the commit frame — exactly `hyst` contended frames after the
    # fusion — NOT quiet_frames later via quiet aging.
    assert len(closes) == 1, closes
    commit_frame, close_ev = closes[0]
    assert close_ev.candidate_id == opens[1][1].candidate_id
    assert commit_frame == fuse_at + hyst - 1, (
        f"absorbed CLOSE emitted at frame {commit_frame}; expected "
        f"{fuse_at + hyst - 1} (merge hysteresis), not "
        f"~{fuse_at + quiet - 1} (quiet aging)"
    )
    # The fused blob still updates the keeper on the commit frame: exactly
    # the keeper surfaces, freshly matched.
    surviving = results[commit_frame].tracks
    assert [t.track_id for t in surviving] == [opens[0][1].candidate_id]
    assert surviving[0].missed == 0
    # Every opened segment is accounted for by a CLOSE (commit or flush).
    assert {e.candidate_id for _, e in opens} == (
        {close_ev.candidate_id} | {e.candidate_id for e in tail}
    )


def test_blob_area_floor_rejects_noise_specks() -> None:
    """Tiny moving specks below track_min_blob_area_frac never become tracks,
    even alongside one big real object that does."""
    cfg = _multi_cfg(track_min_blob_area_frac=0.01)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    rng = np.random.default_rng(0)
    frames = []
    for i in range(20):
        img = _blank()
        _square(img, 90 + i * 8, 180, _val(i), size=90)  # one real object
        for _ in range(5):  # moving 2x2 specks, well under the floor
            sy = int(rng.integers(0, H - 2))
            sx = int(rng.integers(0, W - 2))
            img[sy : sy + 2, sx : sx + 2] = 230
        frames.append(img)
    results, events = _run(gate, frames)
    opens = [e for e in events if e.kind is SegmentKind.OPEN]
    assert len(opens) == 1, [e.candidate_id for e in events]
    assert max((len(r.tracks) for r in results), default=0) <= 1


def test_max_objects_cap() -> None:
    """With track_max_objects=2, only the two largest blobs are tracked even
    when four are present."""
    cfg = _multi_cfg(track_max_objects=2, track_min_blob_area_frac=0.001)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    frames = []
    for i in range(20):
        img = _blank()
        v = _val(i)
        _square(img, 80 + i * 6, 80, v, size=100)
        _square(img, 520 - i * 6, 80, v, size=90)
        _square(img, 80 + i * 6, 280, v, size=56)
        _square(img, 520 - i * 6, 280, v, size=44)
        frames.append(img)
    results, _ = _run(gate, frames)
    assert max(len(r.tracks) for r in results) <= 2


def test_open_frames_one_spawn_parity_with_single_mode() -> None:
    """REVIEW_bringup_4d95b67 finding 9 tail: _spawn_track hardcoded
    active_streak=1 WITHOUT evaluating the open condition, so with
    open_frames=1 an object that appears and then stops moving (one active
    diff, then quiet) never opened in multi mode — single mode opens on that
    very frame and later emits a proper CLOSE. The spawn must run the same
    debounce evaluation as a matched update."""
    frames = []
    for i in range(20):
        img = _blank()
        if i >= 5:
            _square(img, 320, 180, 220, size=100)  # appears at 5, then static
        frames.append(img)

    single = MotionGate(
        MotionConfig(open_frames=1, quiet_frames=5), "cam0", run_token="t0"
    )
    _, single_events = _run(single, frames)
    assert [e.kind for e in single_events] == [SegmentKind.OPEN, SegmentKind.CLOSE]

    multi = MotionGate(
        _multi_cfg(open_frames=1, quiet_frames=5), "cam0", run_token="t0"
    )
    _, multi_events = _run(multi, frames)
    # Parity: same OPEN/CLOSE shape, anchored on the same appearance frame.
    assert [e.kind for e in multi_events] == [SegmentKind.OPEN, SegmentKind.CLOSE]
    assert multi_events[0].frame_index == single_events[0].frame_index


def test_quiet_frames_do_not_surface_stale_tracks() -> None:
    """REVIEW_bringup_4d95b67 finding 15 (gate half): an OPEN track that went
    UNMATCHED this frame must not surface in MotionResult.tracks — app.py
    decodes every surfaced ROI, so a stale ROI would be re-decoded on every
    quiet frame until the close (single mode gates decode on THIS frame's
    motion). The track still stays alive inside the gate and closes through
    the ordinary quiet path."""
    cfg = _multi_cfg(quiet_frames=6)
    gate = MotionGate(cfg, "cam0", run_token="t0")
    frames = []
    for i in range(20):
        img = _blank()
        if i < 10:
            _square(img, 60 + i * 10, 180, _val(i), size=90)
        frames.append(img)
    results = []
    events = []
    for i, img in enumerate(frames):
        res, evs = gate.update(_frame(img, i))
        results.append(res)
        events.extend(evs)
    # Frame 10 carries the disappearance diff (last real motion); the quiet
    # tail before the close must surface NO tracks and read inactive.
    for r in results[11:16]:
        assert r.tracks == (), "stale unmatched track surfaced for decode"
        assert not r.active
    # Eviction is unchanged: the quiet path still closes the segment.
    assert [e.kind for e in events] == [SegmentKind.OPEN, SegmentKind.CLOSE]


def test_break_segment_closes_all_open_tracks() -> None:
    """A discontinuity must close EVERY open track (each gets a CLOSE), not
    just one — the multi-mode analogue of the single-segment break."""
    gate = MotionGate(_multi_cfg(), "cam0", run_token="t0")
    for i, img in enumerate(_two_object_stream(14)):
        gate.update(_frame(img, i))
    broke = gate.break_segment()
    assert len(broke) == 2, broke
    assert all(e.kind is SegmentKind.CLOSE for e in broke)
    assert len({e.candidate_id for e in broke}) == 2


# -- de-churn: morphological CLOSE coalesces a fragmented object --------------


# Downscaled mask geometry for the close test. The gate downscales to
# downscale_width=160; a 1920x1200 full-res image maps to a 100x160 mask. The
# default close kernel is round(0.04 * 160) = 6 -> forced odd -> a 7px ellipse,
# so intra-object gaps a few px wide are bridged, while a gap >> 7px is not.
_SW = 160
_FULL_H, _FULL_W = 1200, 1920
_SH = round(_FULL_H * _SW / _FULL_W)  # == 100


def _close_frame() -> Frame:
    """A dummy full-res Frame so _segment_blobs' scale (w/sw) math is sane."""
    return Frame(
        image=np.zeros((_FULL_H, _FULL_W), np.uint8),
        ts=0.0,
        frame_index=0,
        source_id="cam0",
    )


def _fragmented_mask() -> np.ndarray:
    """ONE object's motion mask, fractured into three 4x4 squares with ~3px
    gaps between them. Each gap (3px) is smaller than the 7px close kernel, so
    the close bridges them into a single component; with the close off they
    stay three separate components."""
    m = np.zeros((_SH, _SW), np.uint8)
    for ox in (20, 27, 34):  # 3px BG gaps between 4-wide fragments
        m[40:44, ox : ox + 4] = 1
    return m


def _two_cluster_mask() -> np.ndarray:
    """TWO genuinely-separate objects: each is a fragmented 3-square cluster,
    but the two clusters sit ~70px apart (gap >> the 7px kernel). The close
    must coalesce each cluster internally yet never fuse the two objects."""
    m = np.zeros((_SH, _SW), np.uint8)
    for ox in (15, 22, 29):  # cluster 1
        m[40:44, ox : ox + 4] = 1
    for ox in (110, 117, 124):  # cluster 2, far away
        m[40:44, ox : ox + 4] = 1
    return m


def test_close_coalesces_fragmented_object_into_one_blob() -> None:
    """Pins the de-churn fix: _segment_blobs morphologically CLOSEs the mask
    (sized by track_close_kernel_frac) before connectedComponentsWithStats, so
    ONE moving code whose motion mask fractured into several blobs becomes a
    SINGLE component (one track) instead of minting several churny micro-tracks.

    A low track_min_blob_area_frac keeps every 4x4 (16px) fragment ABOVE the
    per-blob area floor (0.0005 * 100*160 = 8px), so the close-off case really
    yields >1 blob from surviving fragments — proving it is the CLOSE that
    merges them, not the floor silently dropping the pieces."""
    floor = 0.0005  # each 16px fragment clears the 8px floor on its own

    # Close ON (default 0.04): the three fragments coalesce into ONE blob.
    cfg_on = _multi_cfg(track_close_kernel_frac=0.04, track_min_blob_area_frac=floor)
    gate_on = MotionGate(cfg_on, "cam0", run_token="t0")
    blobs_on = gate_on._segment_blobs(_close_frame(), _fragmented_mask(), sw=_SW)
    assert len(blobs_on) == 1, f"close-on should coalesce, got {len(blobs_on)}"

    # Close OFF (0.0, the mutation): the SAME mask stays >1 blob — the close is
    # what coalesces, not the area floor. (Setting this back to 0.04 makes the
    # assertion fail, confirming the test pins the close behavior.)
    cfg_off = _multi_cfg(track_close_kernel_frac=0.0, track_min_blob_area_frac=floor)
    gate_off = MotionGate(cfg_off, "cam0", run_token="t0")
    blobs_off = gate_off._segment_blobs(_close_frame(), _fragmented_mask(), sw=_SW)
    assert len(blobs_off) > 1, f"close-off should NOT coalesce, got {len(blobs_off)}"

    # The close must NOT fuse genuinely-separate objects: two far-apart
    # fragmented clusters still resolve to exactly TWO blobs WITH the close on.
    gate_two = MotionGate(cfg_on, "cam0", run_token="t0")
    blobs_two = gate_two._segment_blobs(_close_frame(), _two_cluster_mask(), sw=_SW)
    assert len(blobs_two) == 2, f"close must keep distinct objects, got {len(blobs_two)}"
