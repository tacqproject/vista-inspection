# vista-inspection

VISTA – aplikace pro inspekce a katalogizaci konstrukcí, fotogrammetrickou 3D
rekonstrukci, anotace poruch, měření, porovnávání inspekcí a generování protokolů.

Repozitář obsahuje zdrojovou kopii z `/home/epr/VISTA` na `epr-bedna`,
pořízenou 14. 10. 2026. Původní instalace zůstala beze změn.
Zkopírované zdrojové soubory nebyly při přípravě upravovány; doplněna byla tato
dokumentace, pravidla ignorování a příklady lokální konfigurace.

## Obsah

| Adresář | Účel |
| --- | --- |
| `frontend/` | React, TypeScript, Vite, Three.js a 3D prohlížeč |
| `backend/` | FastAPI, SQLAlchemy, inspekce, rekonstrukce a reporty |
| `ai/` | Vlastní inference wrapper, mock a ověřování detekcí |
| `defects-docker/` | Detekce poruch nad fotografiemi |
| `meshroom-docker/` | Prostředí pro Meshroom / AliceVision |
| `openmvs-docker/` | CPU a CUDA sestavení OpenMVS |
| `pgsr-docker/`, `dsine-docker/` | GPU prostředí a odhad normál |
| `scripts/meshroom_docker.sh` | Kopie spouštěče původně uloženého v `/home/epr/` |

Zachovány jsou také původní `start.*`, `docker-compose.yml`,
`requirements.txt` a `package-lock.json`.

## Co zde není

Fotografie, videa, nahrané soubory, vygenerované 3D modely, protokoly, databáze,
logy, skutečné soubory `.env`, instalované závislosti, buildy, cache a váhy AI
modelů jsou záměrně vynechány. Ikona aplikace v SVG je součástí zdrojů.

Jde o zálohu zdrojového kódu. Obnova provozních dat vyžaduje samostatnou zálohu
databáze a úložiště inspekcí; část dat původní instalace je přes symbolické
odkazy uložena mimo VISTA na SSD.

## Lokální konfigurace a spuštění

Backend používá Python; jeho Dockerfile vychází z Pythonu 3.12.
Frontend má Dockerfile s Node.js 20. Závislosti jsou v
`backend/requirements.txt` a `frontend/package-lock.json`.

1. Pro backend vytvořte virtuální prostředí a nainstalujte
   `backend/requirements.txt`.
2. Zkopírujte `backend/.env.example` do `backend/.env`. Nastavte vlastní
   `JWT_SECRET`, počáteční administrátorské heslo a cesty k úložišti a nástrojům.
3. Ve `frontend/` nainstalujte závislosti pomocí `npm ci`. Podle potřeby
   vytvořte `frontend/.env` podle příkladu.
4. Z adresáře `backend/` spusťte ve virtuálním prostředí
   `uvicorn app.main:app --host 127.0.0.1 --port 8000`.
   Ve druhém terminálu spusťte ve `frontend/` příkaz `npm run dev`.

Původní kořenový `.env.example` je zachován jako součást kopie; obsahuje také
starší nastavení ODM. Pro novou konfiguraci použijte příklady přímo u backendu
a frontendu.

Původní `start.sh` před startem ukončuje existující procesy uvicorn/Vite a
procesy na portech 8000/5173. Pro souběžný běh s původní instalací je nutné
upravit porty i související URL; tento skript nespouštějte jako druhou instanci
nad běžící VISTA.

Instalace závislostí, build ani spuštění aplikace nebyly při přípravě této
zdrojové kopie prováděny.

## Fotogrammetrie a GPU nástroje

Samotný Compose obsahuje backend a frontend; kompletní fotogrammetrické a AI
prostředí vyžaduje další nastavení:

- Meshroom / AliceVision je externí instalace. Zkopírovaný
  `scripts/meshroom_docker.sh` zachovává původní cesty
  `/media/epr/ssd/Meshroom-2025.1.0` a `/home/epr/VISTA/meshroom-docker`.
  Při přenosu na jiný stroj upravte jeho cesty a `MESHROOM_PATH` v backendu.
- `gltfpack` je externí nástroj pro zpracování modelů.
  Nastavuje se přes `GLTFPACK_PATH`.
- Docker obrazy navazují v pořadí `vista-pgsr` → `vista-dsine` →
  `vista-defects`. OpenMVS má samostatné varianty
  `vista-openmvs:2.3` a `vista-openmvs:2.3-cuda`.
- CUDA Dockerfiles obsahují nastavení architektury GPU `8.6`; při jiném
  hardwaru je potřeba konfiguraci zkontrolovat.
- Export protokolu do PDF používá externí LibreOffice.
- Výchozí pracovní adresář v původním Python kódu je cesta na SSD.
  Nový `backend/.env.example` jej přepisuje proměnnou `WORK_DIR`.

## Externí projekt dacl10k a váhy

V původní instalaci je `ai/dacl10k_challenge/` samostatný cizí Git repozitář:

- Zdroj: https://github.com/mpaques269546/dacl10k_challenge
- Použitý commit: `dc0f66644e802861010fc43b7214f05ba04b40e5`

Sledované Python zdrojáky tohoto checkoutu neměly při kopírování lokální změny.
Lokálně přidaný `dacl10k_infer.py` je totožný s verzí v `ai/`, která je
zahrnuta v této záloze. Cizí checkout, jeho fotografie, notebook a Git historie
se sem nekopírují; případný lokální checkout ignoruje `.gitignore`.

Pro instalaci této externí závislosti lze z kořene repozitáře použít:

```sh
git clone https://github.com/mpaques269546/dacl10k_challenge ai/dacl10k_challenge
git -C ai/dacl10k_challenge checkout --detach dc0f66644e802861010fc43b7214f05ba04b40e5
```

Inference běží ve vlastním Python prostředí a backend ji volá přes
`DACL10K_CMD`. Použijte vlastní wrapper `ai/dacl10k_infer.py`.
Původní instalační poznámky a popis rozhraní jsou v [ai/README.md](ai/README.md);
tam uvedené absolutní cesty popisují původní stroj.

Váhy jsou samostatné artefakty. Původní `ai/dacl10k_challenge/weights` byl
symbolický odkaz na `/media/epr/ssd/dacl10k_weights`; tento odkaz ani jeho
obsah nejsou součástí repa. Vynechán je také
`ai/verifier/verifier.pt`. Váhy je při obnově nutné dodat zvlášť.
Pro vývoj integračního rozhraní je zahrnut `ai/mock_infer.py`.
