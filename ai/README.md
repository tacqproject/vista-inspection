# AI defektní segmentace (dacl10k) — integrace do VISTA

VISTA umí spustit AI segmentaci betonových poruch přes **všechny snímky inspekce**
(19 tříd dacl10k). Těžký model (BEiT-large adapter + Mask2Former, mmcv) běží ve
**vlastním prostředí** a VISTA ho volá jako externí příkaz — jako Meshroom wrapper.

## Stav (2026-09-30)

- ✅ Prostředí: **`/media/epr/ssd/dacl10k_env`** (conda, Python 3.10.21, torch 2.0.1+cu117,
  torchvision 0.15.2, **mmcv-full 1.7.2** – prebuilt wheel cu117/torch2.0 (1.7.1 wheel
  neexistuje, source build padá), transformers 4.40.2, numpy 1.26, opencv-headless 4.10).
  Cache: `/media/epr/ssd/conda_pkgs`, `/media/epr/ssd/pipcache`, `TMPDIR=/media/epr/ssd/tmp`.
- ❌ **Oficiální váhy (BEiT-L adapter + Mask2Former, `weights/models.pth`) NEJDOU stáhnout.**
  Drive složka `120MExaJwfFyd4-SA6ShC4ufsXeCVbHcl` vrací 404 anonymně (gdown 6.4.1 i curl;
  kontrolní veřejná složka vrací 200) i přes autentizované Google Drive API ("not found") →
  smazaná/neveřejná. GitHub issue #2 v repu hlásí totéž od 12/2024, autor neodpověděl.
  Žádné releases, forky mají stejný mrtvý odkaz, na HF/Wayback nic.
  Architektura je ale ověřená (build + GPU běh s náhodnými vahami: ~14 s / 12 MP při
  tile 1024, ~3.6 GiB VRAM) → když se `models.pth` někde objeví, stačí ho dát do
  `/media/epr/ssd/dacl10k_weights/models.pth` a wrapper ho sám použije.
- ✅ **Fallback model (běží):** SegFormer mit-b1 od autorů datasetu dacl10k
  (HF Space `phiyodr/dacl-challenge`, `runs/2023-08-31_rich-paper-12/best_model_cpu.pth`,
  „10 epoch @512“). Převedeno na čistý state_dict:
  `weights/segformer_mitb1_dacl_space/segformer_mitb1_dacl10k_statedict.pth`.
  Slabší model – podklad k revizi, ne verdikt.

## Spuštění

```bash
/media/epr/ssd/dacl10k_env/bin/python /home/epr/VISTA/ai/dacl10k_challenge/dacl10k_infer.py \
    --images <dir> --out <dir> --tile 1024 --overlap 128 --device cuda --save-masks
```
Volby: `--backend auto|official|segformer` (auto = official, pokud existují váhy),
`--threshold 0.5`, `--save-masks`, `--force-tile N --force-overlap N` (přebije VISTOu
předávané `--tile 1024`; dá se dát do `DACL10K_CMD`), `--gpu-lock /tmp/vista-gpu.lock`
(zamkne GPU uvnitř; nekombinovat s vnějším `flock` na stejný soubor → deadlock).
Env ekvivalenty: `DACL10K_BACKEND`, `DACL10K_WEIGHTS`, `DACL10K_SAVE_MASKS=1`,
`DACL10K_FORCE_TILE`, `DACL10K_FORCE_OVERLAP`, `DACL10K_GPU_LOCK`.

Do `backend/.env` (doporučeno – tile 2048 dává u SegFormeru méně falešných detekcí):
```
DACL10K_CMD=/media/epr/ssd/dacl10k_env/bin/python /home/epr/VISTA/ai/dacl10k_challenge/dacl10k_infer.py --save-masks --force-tile 2048 --force-overlap 256 --gpu-lock /tmp/vista-gpu.lock
```

### Výstupy masek (`--save-masks`)
- `mask_<stem>.png` – 8bit L, **původní rozlišení**, hodnota = index do
  `results.json["mask_classes"]` = `["background", "Crack", "ACrack", "Wetspot",
  "Efflorescence", "Rust", "Rockpocket", "Hollowareas", "Cavity", "Spalling", "Graffiti",
  "Weathering", "Restformwork", "ExposedRebars", "Bearing", "EJoint", "Drainage",
  "PEquipment", "JTape", "WConccor"]` (0 = pozadí, 1..19 = `classes[v-1]`).
  Pixel = třída s nejvyšší pravděpodobností, pokud > threshold, jinak 0 (multi-label se
  zplošťuje na argmax).
- `conf_<stem>.png` – 8bit L, `round(255 * max pravděpodobnost třídy)`; pixely třídy
  mají ≥ 128 (při thr 0.5), pozadí < 128 → lze přeprahovat.
- Orientace = surové pixely (EXIF rotace se neaplikuje; fotky v inspekci mají orientaci 1).
- `results.json` navíc: `mask_classes`, `model` (backend, váhy, threshold, tile),
  `mask_format`, u obrázku `mask`, `confidence`, `width`, `height`, `seconds`.

### Výkon (RTX A2000, 4032×3024)
SegFormer: ~1.1–1.9 s / snímek (tile 1024), ~0.8–1.7 s (tile 2048), start ~3 s,
VRAM 0.4–1.0 GiB, RAM ~4 GB. Oficiální model (až budou váhy): ~14 s / snímek (tile 1024).

## Kontrakt (kdyby ses chtěl odpojit od tohohle modelu)

VISTA volá: `<cmd> --images <dir> --out <dir> --tile 1024 --overlap 128 --device cuda`
a čeká `<out>/results.json` + `overlay_*.jpg`. Progress přes `PROGRESS <pct> <msg>`
na stdout. Celý harness (dlaždice/slepení/coverage/overlay) je hotový; model se
napojuje jako backend třída s metodou `predict_probs(tile) -> [19,h,w]` pravděpodobností
(viz `OfficialBackend`, `SegformerBackend` v `dacl10k_infer.py`).

## Test bez modelu (mock)
```
DACL10K_CMD=/home/epr/VISTA/backend/.venv/bin/python /home/epr/VISTA/ai/mock_infer.py
```

## Poznámky k modelu
- Výkon **oficiálního** modelu (testdev mIoU; váhy nedostupné): Spalling 0.50, Rust 0.49, Efflorescence 0.46, Weathering
  0.43 slušné; Crack 0.32, Cavity 0.16 slabé. Podklad k revizi, ne verdikt.
- Fixní vstup 512 px → proto dlaždice s překryvem (jinak se 4K downsampluje a
  trhliny zmizí).
- SegFormer fallback (subjektivně na 3 fotkách pilířů, inspekce f917a30a): Graffiti
  spolehlivé; Spalling/odlupky omítky a Crack zachytí, ale s FP; PEquipment, Cavity,
  Drainage, JTape, Efflorescence dávají falešné pozitivy (světlé přetřené plochy, zem,
  odpadky). Tile 1024 = víc detailu i šumu + švy dlaždic; tile 2048 = rozumný kompromis;
  celý snímek naráz = hladké, ale trhliny mizí.
- Lišejník/mech nemá vlastní třídu → spadne pod Weathering/Wetspot.
