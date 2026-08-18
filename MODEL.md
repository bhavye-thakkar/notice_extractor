# How the Public Notice Extractor finds notices — the model, A to Z

This is the reference for *what* decides that a rectangle on a newspaper page
is a જાહેર નોટિસ / જાહેર ચેતવણી, *which parameters* those decisions run on,
*how the accuracy was measured*, and *how the app learns from you*.  Every
number here is the value in the code today (`core.py`, `utils/feedback.py`,
`utils/search.py`); the history of *why* each one is what it is lives in
`../flow.md` and `../decision.md`.

---

## 1. There is no neural network — and why

The detector is a **classical computer-vision + OCR pipeline** with a small
**online learning layer** on top.  Deliberately:

* The class of object is narrow and stable: a printed box with a printed
  title in one of a handful of spellings, in one of five newspapers' house
  typefaces.  A template of the real printed title is a better model of
  that than a trained network with no training set.
* Everything must run on an office Windows PC with no GPU, on a fresh
  install, on a page it has never seen, and be explainable to the person
  clicking Not Related.
* Every decision is traceable to a page and a number (see §7); a network
  would give a score with no reason.

The "model" is therefore the sum of: embedded header **templates**, the
**geometry rules** for boxes/columns/pills, the **OCR keyword** rules, the
**veto** rules, and the **learned weights** from your feedback.

---

## 2. Inputs

| Newspaper | Source | Page image |
|---|---|---|
| Sandesh | public JSON API `new-wapi.sandesh.com/api/v1/e-paper` → CDN photos (`?w=1600`) or a whole-edition PDF (small editions) | ~2332×3231 px |
| Gujarat Samachar | e-paper page images | per page |
| Divya Bhaskar | e-paper PDF (login) | rendered by PyMuPDF |
| Nav Gujarat Samay | reader page renders | per page |
| Local PDF | any PDF | rendered by PyMuPDF |

Each page is one `numpy` BGR image.  Detection runs on a **working copy
downscaled to 1500 px wide** (`DetectionConfig.working_width`); crops and,
since this session, the OCR header bands are cut from the **full-resolution**
image.

---

## 3. The detection pipeline (`NoticeDetectionPipeline._detect_page`)

```
page ─┬─ Pass 1  ruled BOX candidates ──── template score ──┬─ ≥ threshold ─────────────► detection (box+template)
      │        (BoxCandidateDetector)      │                 ├─ 0.45–0.66: grow to pill, re-score
      │                                    │                 └─ undecided → OCR header band ─┬─ notice word ► detection (box+ocr)
      │                                    │                                                └─ ≥ review_low ► Not Sure (box+template?)
      ├─ Pass 2  full-page TEMPLATE sweep (broken borders) → attach to enclosing rect / synthesise box
      ├─ Pass 3  full-page OCR sweep — only on empty pages whose best template score ≥ 0.60
      ├─ filter  GLOBAL_MIN_ACCEPT_SCORE 0.63 (box+ocr exempt)   → dedup (fragment-vs-container rule, IoU/containment NMS)
      ├─ Pass 3b reconcile columns (snap narrow fragments to the page's own column grid)
      ├─ Pass 4  split crops holding several titles
      ├─ Pass 5  drop containers that swallow a kept crop
      ├─ Pass 6  VETO tender / auction / recruitment / સુધારો look-alikes (OCR + negative templates); trim-to-cell for mixed boxes
      └─ Pass 7  clip small same-column overlaps (no crop carries the next title)
```

### 3.1 Box candidates (`BoxCandidateDetector`)
Adaptive threshold → morphological opening with a horizontal (3.5 % page
width) and vertical (1.8 % page height) kernel → ruling-line masks → closed
contours.  A candidate must be 5.5–64 % of the page wide, ≥ 64 px tall,
aspect 0.22–9.0, with ≥ 58 % of its perimeter on ruling lines and ≥ 3 sides
≥ 30 % covered.  All closed rects ≥ 18 px tall are kept separately
(`all_rects`) so title *pills* can be found.

### 3.2 Header templates (`HeaderTemplateVerifier`)
Real title crops, embedded as base64 PNG (48 px tall) — the primary evidence:

| Label | What | Paper |
|---|---|---|
| gs-header-sample-1/2, gs-notis-court-1 | જાહેર નોટિસ (two weights, court style) | Gujarat Samachar |
| sandesh-header-sample-1 | જાહેર નોટિસ (pill) | Sandesh |
| **sandesh-header-sample-2** | જાહેર નોટીસ, outlined display face (added 18 Aug 2026) | Sandesh |
| chetavni-sandesh-1, **chetavni-sandesh-2** | જાહેર ચેતવણી pill | Sandesh |
| chetavni-gs-1 | જાહેર ચેતવણી | Gujarat Samachar |
| db-header-sample-1/2 | pill headers | Divya Bhaskar |
| NEG:gs-sudharo-1, NEG:db-sudharo-1 | નોટિસમાં સુધારો (a correction — vetoed) | — |

Plus font-rendered variants (Shruti etc.) of every spelling in
`HEADER_VARIANTS`, and font-rendered **negative** templates
(`NEGATIVE_HEADER_VARIANTS`: જાહેર નિવિદા, ટેન્ડર નોટિસ, હરાજી, કબજા નોટિસ…).

Scoring: `cv2.matchTemplate(TM_CCOEFF_NORMED)`, template resized to each
height in `strip_scales` (14…62 px), best |score| wins; polarity-free (a
white-on-black pill scores the same).  Strip = top 32 % of the box (42–170
px).  A strip in [0.45, threshold) is scored again upscaled ×1.8 with the
real crops only (the "rescale probe" for small headings).

Per-newspaper thresholds (`DetectionConfig`):

| | box_match | page_match | review_low | notes |
|---|---|---|---|---|
| default | 0.58 | 0.66 | 0.55 | |
| Gujarat Samachar | 0.66 | 0.70 | 0.55 | |
| Sandesh | 0.66 | 0.72 | 0.55 | |
| Divya Bhaskar / PDF | 0.68 | 0.72 | 0.55 | denser strip scales |

### 3.3 Grow-to-pill (Sandesh; new)
Sandesh prints the title in its **own ruled cell above** the body and the
body as two sub-column cells.  A candidate scoring 0.45–0.66 is extended
up to the nearest ruling line ≤ 52 px above when that band is a single
cell (no interior vertical rule) and, when the pill's own closed rect is on
the page, takes the pill's width.  Kept only if the grown strip verifies
**and** the header hit lies in the added band (`_hit_in_band`, which reads
the verifier's `last_strip_hit` — where its own winning match sat — rather
than re-scanning with the 6-template page set, which missed real headers).

### 3.4 OCR of the header band
For undecided candidates, the **shallow band** (top 18 % of the box) is OCR'd
(`guj+eng`), cut from the full-resolution page and resized to what the
working-scale band would have had after the 72-px upscale (same cost, real
pixels).  Before OCR, white-on-black **title pills are lifted out, inverted
and stacked above the band** as isolated lines (`_normalize_pill_polarity`)
— Tesseract skips a display line that shares a row with body text.  A
Latin-heavy band with no match earns one deeper (32 %) read (English
letterheads).  A band containing any spelling in `JAHER_NOTICE_KEYWORDS` /
`CHETAVNI_KEYWORDS` (fuzzy ratio ≥ 0.80, exact for < 6 chars) makes the
box a `box+ocr` detection with confidence 0.50 + 0.45·ratio (≤ 0.97); the
band text is kept on the detection (`header_text`).

### 3.5 Veto (Pass 6)
Every detection's band text and deep-strip text is checked against
`NEGATIVE_KEYWORDS` (ટેન્ડર, નિવિદા, હરાજી, e-auction, ભરતી, કબજા નોટિસ …;
fuzzy ≥ 0.84).  A negative that outranks the positive → dropped.  Negative
*templates* veto only when ≥ 0.42, the positive < 0.72, and the negative
beats the font-rendered positive by ≥ 0.05.  `NEGATIVE_OVERRIDE_KEYWORDS`
(નોટિસમાં સુધારો) always veto.  `RELATED_HEADING_KEYWORDS` (જાહેર સમન્સ,
જાહેર નિવેદન, summons) send a template detection whose header shows one of
these and **no** notice/ચેતવણી word to Not Sure — related, not the category,
not junk.  A **template-verified wide box whose text
shows both a title and a tender word** is trimmed to the title's own ruled
cell instead of dropped (`_trim_to_header_cell`).

### 3.6 One notice = one crop
* Dedup: NMS at IoU ≥ 0.45 / containment ≥ 0.72, higher score wins — except
  a sub-column **fragment** (≤ 60 % of a verified container's width,
  ≥ 90 % inside it, and ≥ 80 % of its height when the container is
  OCR-verified) always loses to the container.
* Column reconcile: with ≥ 3 verified boxes agreeing on a column width
  (±15 %), a detection under 72 % of it that conflicts with a full-width
  notice's column is widened to the grid, re-verified, and clipped against
  covered territory.
* Split: a crop with ≥ 2 title hits (≥ 0.70) is cut along the ruling lines
  between them, insets `crop_padding+2` so crops never share a divider.
* Containers that swallow a kept crop (containment ≥ 0.62, area > 1.35×) go.
* Overlap clip: a crop's bottom stops where a same-column neighbour starts
  (overlap ≤ 25 % of its height).

### 3.7 Result confidence shown to the user
`confidence % = round(score × 100)`; the *method* string on each result
(`box+template`, `box+ocr`, `box+template+cell`, `+col`, `+split`,
`page-scan`, `ocr-sweep`) says which path found it.  Detections whose score
sat in [review_low, threshold) with no OCR confirmation are `uncertain` →
**Not Sure** queue.

---

## 4. Reading the crops (`read_notice_crops`)
Only when needed (search, notice-type filter, feedback): the whole crop is
OCR'd (`psm 11`, word boxes) from a **×1.5 cubic-upscaled copy**
(`CROP_OCR_UPSCALE`; skipped when the crop's longer side is already
> 1600 px) — measured on 16 real Sandesh crops: mean word confidence
85.1 → 87.8, high-confidence words +7 %, and on 30 crops the Gujarati
search found 9 % more hits (title phrase 12 → 18) for +17 % OCR time.
Boxes are mapped back to crop pixels; words with boxes are cached on the
result.
`classify_notice_text` labels the crop **નોટિસ / ચેતવણી** from the
distinguishing word, header band (top 30 %) first.

OCR engines, in preference order: Windows built-in OCR (Gujarati pack) →
Tesseract 5 with `tessdata/guj.traineddata` → EasyOCR (no Gujarati).
Tesseract is driven directly (PNG on stdin, text/TSV on stdout).

---

## 5. Search (`utils/search.py`)
Token search: every word of the query must occur in the notice, any order.
Text and query are normalised (zero-width chars, punctuation and *all*
whitespace removed, Latin case-folded) and, for search only, **Gujarati
vowel length and nasal marks are folded** (ી→િ, ૂ→ુ, ૈ→ે, ૌ→ો, ઁ→ં, ઈ→ઇ …) so
"નોટીસ" finds "નોટિસ" and vice versa.  Latin tokens ≥ 4 chars match fuzzily
(`difflib` ratio ≥ 0.80 over sliding windows, with an exact O(1)-per-window
prefilter — 145 k randomised comparisons, 0 mismatches vs the brute force).
**Gujarati tokens** use a Gujarati-aware weighted edit distance against any
substring (`guj_fuzzy_contains`): a dropped/garbled matra or anusvara costs
½, a swap inside a look-alike consonant class (ખ/ષ, ડ/ઙ, ભ/મ, ન/ત, ળ/લ …)
costs ½, anything else 1; the budget is 0.5 for 3-char words, 1 for 4–5,
1.5 for 6–8, 2 for 9–11, then length/5 — so સાણંદ finds OCR's સાણદ and
ચેખલા finds ચેષલા, while વડોદરા does not match either.  Same matcher picks
the OCR word to box.
Word boxes of matching OCR words are drawn as red outlines on the card and
in the preview.

---

## 6. The learning layer (`utils/feedback.py`) — reward-gated, reversible

**Signals** — only the two buttons that already exist:
`✕ Not Related` (results card or Not Sure) = negative; `✓ This Is Right`
(Not Sure) = positive.  Each click appends one record to
`data/feedback.jsonl` (append-only *evidence*): OCR text, normalised text,
crop size, newspaper, page, method, confidence, timestamp.

**Model** (`data/learned.json`, versioned, last 5 kept for `rollback()`):

| Weights | Learned from | Guard rails |
|---|---|---|
| `weights` (negative tokens) | 6-char text shingles, step 3, appearing in ≥ **3** rejected notices and ≥ **3×** more often rejected than confirmed | `log1p(neg − pos)`; a token in both is discarded |
| `positive_weights` | the mirror image | same |
| `region_weights` (layout buckets: paper \| detection path \| size class \| shape) | ≥ **4** rejections of a bucket, ratio ≥ 3 | capped at 0.9 < DEMOTE_SCORE — layout alone never hides anything |

**Applying** (`apply_learning`, once per read pass):

* `should_demote`: OCR read, confidence < **88**, and text + region points
  ≥ **1.5** → the notice moves to **Not Sure** ("held back by learning").
  A confident template match is never demoted.
* `should_promote`: a Not Sure crop with positive points ≥ **2.0** and zero
  negative points → results.  Never invents a detection.

**Reward and gate** (`relearn()`, after every click, in a worker thread):
the candidate model is scored on the user's own verdicts —
`reward = caught_negatives + promoted_positives − 3·hurt_positives −
promoted_negatives` — and **leave-one-out** (each record removed from the
build set before it is judged).  If it would hide any confirmed real notice
or promote any confirmed rejection, the model is **HELD** (the click is
kept, the old model rules).  Every decision is appended to
`data/learning_history.jsonl`.  Nothing here is deep RL; it is an online
classifier whose updates are gated by a reward on held-out human verdicts.

---

## 7. How accuracy is measured (`tools/`)

```
capture pages (scratchpad capture_sandesh.py <edition> <date> <out>)
  → python -m notice_extractor.tools.validate_pages <folder>      # replay, overlays, validation.json
  → python -m notice_extractor.tools.measure_validation validation.json \
        tools/validation/<gt>.json [previous.json] --label NAME       # TP/FP/FN/fragments/review, P/R/F1, KEEP/REVERT
```

Ground truth = hand-verified notice rectangles (working-scale, IoU ≥ 0.5 to
match), one file per edition in `tools/validation/`.  A detection inside an
expected notice counts as a *fragment*, not an extra.  Review-queue items
are counted separately (they are not shown as results).  Every scored run is
appended to `data/validation_history.jsonl`.  Rule: recall may not drop
between runs, target ≥ 98 %; changes that regress are reverted (the gate
fired once this session and the fix followed).

**Numbers (18 Aug 2026 session, Sandesh):**

| Edition | Before | After |
|---|---|---|
| Ahmedabad 2026-08-18 (89 notices, tuning set) | TP 82 · FP 0 · FN 6 · recall 0.932 | **TP 89 · FP 0 · FN 0 · recall 1.000**, 2 in Not Sure (a જાહેર નિવેદન, a સુધારો) |
| Ahmedabad 2026-08-17 (30 notices, held out) | 6 tender false positives, 1 half-width crop | **TP 30 · FP 0 · FN 0** (two 2-column notices now whole; GT corrected) |
| Ahmedabad 2026-08-16 (36 notices, second held-out; API served the 15-08 print) | 1 miss: court notice under a two-row header (pill + court name) | **TP 36 · FP 0 · FN 0**, 3 borderline items in Not Sure |

Speed: keyword matching was 44 s of a 59 s page → prefiltered, p14 36 s →
16 s; 22-page replay ≈ 450 s → ≈ 230 s.  Offline suite `tools/test_pipeline.py`
57/57; live driver `tools/qa_run.py` (real window, real extraction, buttons,
search, review, resize, shutdown) all sections PASS.

---

## 8. Every parameter, in one place

| Name | Value | Where | Meaning |
|---|---|---|---|
| working_width | 1500 | DetectionConfig | detection resolution |
| box_min_w_frac / box_max_w_frac | 0.055 / 0.640 | " | candidate width bounds |
| box_min_h_px / box_max_h_frac | 64 / 0.75 | " | candidate height bounds |
| box_min_aspect / box_max_aspect | 0.22 / 9.0 | " | h/w |
| border_coverage_total / _side | 0.58 / 0.30 | " | ruling-line coverage |
| strip_frac_of_box, strip_min/max_px | 0.32, 42/170 | " | template strip |
| ocr_header_frac | 0.18 | " | OCR band |
| review_low / rescale_probe_low | 0.55 / 0.45 | " | Not Sure band / probe floor |
| rescale_probe_factors / _scales | (1.8,) / (22,26,31,37) | " | small-heading probe |
| strip_scales | 14…62 (10 sizes) | " | template heights |
| page_scan_scales | 16,20,26,33,42 | " | full-page sweep |
| box_match / page_match threshold | per paper (§3.2) | " | accept bars |
| nms_iou_threshold / nms_containment | 0.45 / 0.72 | " | dedup |
| crop_padding | 6 px | " | crop margin |
| GLOBAL_MIN_ACCEPT_SCORE | 0.63 | core | floor for template detections |
| PAGE_SCAN_TEMPLATES | 6 | core | templates used by the sweep |
| OCR_STRIP_TARGET_HEIGHT | 72 px | core | band upscale target |
| OCR_MAX_STRIPS_PER_PAGE | 90 | core | OCR cap per page |
| OCR_SWEEP_MIN_TEMPLATE | 0.60 | core | gate for the empty-page OCR sweep |
| SWEEP_MAX_WIDTH | 900 px | core | OCR sweep image width |
| FUZZY_MATCH_RATIO / NEGATIVE_FUZZY_RATIO | 0.80 / 0.84 | core, search | keyword fuzziness |
| FUZZY_MIN_KEYWORD_LEN / FUZZY_MIN_TOKEN_LEN | 6 / 4 | core / search | below: exact match only |
| NEGATIVE_TEMPLATE_MIN / MARGIN / TRUST_POS | 0.42 / 0.05 / 0.72 | core | template veto |
| STRONG_NEGATIVE_PAGE_THRESHOLD | 0.72 | core | સુધારો pill scan |
| DETECT_CONCURRENCY / CV2_THREADS_PER_DETECT | cpu-derived | core | parallel agents |
| MIN_SUPPORT / MIN_RATIO / MIN_TOKEN_LEN | 3 / 3.0 / 4 | feedback | text learning guards |
| REGION_MIN_SUPPORT / REGION_WEIGHT_CAP | 4 / 0.9 | feedback | layout learning guards |
| DEMOTE_SCORE / PROMOTE_SCORE / PROTECT_CONFIDENCE | 1.5 / 2.0 / 88 | feedback | apply thresholds |
| pill grow window / band rule test | ≤ 52 px / interior vertical ink > 35 % | core `_grow_to_notice` | Sandesh pills |
| trim-to-cell | header ≤ 45 % of box width, rule within 1.6× header width | core | mixed boxes |
| overlap clip | ≤ 25 % of upper crop's height, ≥ 90 px left | core | Pass 7 |

---

## 9. Where things are stored

| File | What | Survives restart |
|---|---|---|
| data/feedback.jsonl | every click (evidence) | yes |
| data/learned.json | the learned model + last 5 versions | yes |
| data/learning_history.jsonl | every relearn decision with metrics | yes |
| data/validation_history.jsonl | every scored validation run | yes |
| data/recent_searches.json | search history | yes |
| data/logs/, cache/, debug/ | run leftovers | cleared on exit |
| tools/validation/*.json | ground truth per edition | repo |
