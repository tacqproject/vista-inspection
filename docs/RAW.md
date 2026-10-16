# Samostatná VISTA RAW

## Nasazení na epr-bedna

| Součást | Umístění |
| --- | --- |
| Web | http://10.10.1.80:5174 |
| API | 127.0.0.1:8001, z webu přes `/api` |
| Zdrojáky | `/home/epr/git/vista-inspection-raw`, větev `feat/raw-isolated` |
| Původní repo | `/home/epr/git/vista-inspection`, hlavní větev |
| Databáze nové instance | `/media/epr/ssd/vista-raw/state/vista.db` |
| Uploady, modely | `/media/epr/ssd/vista-raw/uploads` |
| Pracovní data, náhledy | `/media/epr/ssd/vista-raw/scratch` |
| Konfigurace a provozní záznamy | `/media/epr/ssd/vista-raw/operations` |
| Sdílené originály, jen čtení | `/media/epr/ssd/vista-work/datasets/corvallis-bridge/VLABDW/photos` |

Přihlašovací účty byly při založení zkopírovány do nové databáze. Uživatel
se přihlásí stejným jménem a heslem; pozdější změny účtů se mezi instancemi
nesynchronizují. Nová instance má vlastní JWT klíč a vlastní provozní data.

V konstrukci **Most Corvallis – RAW** jsou dvě inspekce: všech 798 originálů
a samostatná malá sada 20 snímků pro ověření. Velká sada se po importu
automaticky nerekonstruuje. Spuštění: inspekce → **Model** → požadovaná kvalita.
Zdroj: Yelda Turkan / Yiye Xu, Oregon State University,
https://doi.org/10.7910/DVN/VLABDW, licence CC0.

## Práce s RAW

1. Upload přijímá DNG i běžné obrazové formáty, také uvnitř ZIP. RAW je uložen
   jako originál v `photos/`. Import místního datasetu přes `/import-local`
   vytváří odkazy na povolený adresář, takže se dalších 32,87 GB nekopíruje.
2. Galerie a náhledy se dekódují pomocí rawpy/LibRaw do oddělené cache.
   DNG jméno zůstává identitou snímku. Jeho běžné webové URL vrací JPEG;
   autentizované `/api/inspections/{id}/photos/{filename}/original` vrací DNG.
3. Pokud sada obsahuje DNG, běžné JPEG předzpracování se přeskočí. Meshroom /
   AliceVision dostává přímo originály, čte metadata kamer a počítá SfM.
   `model/source_manifest.json` zaznamenává skutečné zdroje. Cache náhledů
   není zdrojem pro rekonstrukci.
4. V režimu **Maximální přesnost** se pro OpenMVS DNG znovu přímo dekódují
   v plném rozlišení a odstraní se zkreslení podle SfM. Výsledkem jsou
   bezeztrátové RGB8 PNG místo původního mezikroku JPEG. Rozměry se musí
   přesně shodovat se SfM; při neshodě se běh zastaví.
5. OpenMVS vytváří hloubkové mapy, husté mračno, síť a textury. Následují
   výstupy pro web. AI poruchy mohou pracovat s oddělenými RGB náhledy;
   jejich názvy se mapují zpět na původní DNG.

Instalovaný OpenMVS 2.3 může při exportu OBJ spadnout s kódem 139
([upstream hlášení](https://github.com/cdcseacave/openMVS/issues/1132)).
Nová instance v tomto případě automaticky zopakuje pouze texturování do PLY
a převede texturovanou síť přes PyMeshLab do OBJ. Zachování geometrie, UV
souřadnic a pixelů textury pokrývá regresní test. Původní Docker obrazy
zůstávají beze změny.

RAW zde znamená původní DNG jako vstup rekonstrukce. OpenMVS pracuje s RGB8
a finální textury mohou být JPEG; **nejde o 16bitový řetězec od začátku do konce**.
U testovaných Hasselblad DNG se používá aktivní plocha 5472 × 3648 bez dalšího
ořezu a automatické rotace. Jiné rodiny RAW souborů zatím nejsou povolené.

## Oddělení od původní instalace

Backend a frontend běží v kontejnerech `vista-raw-backend` a
`vista-raw-frontend`, v síti `vista-raw-net`. Kontejnery nemají připojený
`/home/epr/VISTA`, původní databázi ani úložiště původních inspekcí.
Pouze adresář datasetu a Meshroom jsou připojené pro čtení. Závislosti Pythonu,
Node a gltfpack byly zkopírovány samostatně; žádný produkční balíček se nemění.
Čtou se hostitelské systémové knihovny `/usr`, `/lib` a `/lib64`.

Backend nemá přístup k Docker socketu. Úlohy spouští omezený broker přes vlastní
Unix socket. Přijímá jen `image inspect` a `run` pro předem instalované obrazy;
při startu je připne na jejich ID. Odmítá cizí zapisovatelné mounty, přístup
k produkčním adresářům, privilegované režimy, hostitelskou síť, build a správu
cizích kontejnerů. Obrazy původní VISTA se nepřestavují ani nepřeznačují.

Výpočetní úloha má 8 CPU, 24 GiB RAM bez dalšího swapu a běží po jedné.
Backend má 4 CPU / 16 GiB, web 1 CPU / 512 MiB. GPU operace spolupracují
se stávajícím `/tmp/vista-gpu.lock`. Sdílí se fyzický počítač a GPU;
výkonnostní konkurence tedy zůstává možná, zvláště pokud jiný program zámek
nepoužívá. Izolace aplikace není samostatný fyzický počítač.

Broker zastaví svoji úlohu při poklesu volného SSD pod 10 GB. Příprava PNG,
nahrávání fotografií a tvorba náhledů také kontrolují rezervu. To nenahrazuje
diskovou kvótu: prostor mohou současně spotřebovávat jiné aplikace. Celých
798 snímků potřebuje podstatně více scratch prostoru než malý test; při
nedostatku místa běh skončí vysvětlenou chybou. Rozlišení husté rekonstrukce
se přizpůsobuje dostupné paměti a místu, originály se nezmenšují.

## Spuštění a údržba

Nasazení používá již instalovaný `ubuntu:22.04` a samostatnou kopii závislostí.
Soukromé proměnné jsou v `operations/backend.env`, oprávnění 0600. Konfigurace
vyžaduje všechny zapisovatelné cesty uvnitř `ISOLATED_DATA_ROOT`; při odkazu
mimo něj start selže. Tento soubor nepatří do Gitu.

```sh
cd ~/git/vista-inspection-raw
./start.sh
docker logs --tail 100 vista-raw-backend
systemctl --user status vista-raw-broker.service
```

Spouštěč existující pojmenované kontejnery ponechá. Po změně Python kódu se
restartuje pouze `docker restart vista-raw-backend`, až po dokončení úloh.
Po změně webu: ve `frontend/` spustit `npm run build`; změna samotného
`serve-raw.mjs` vyžaduje `docker restart vista-raw-frontend`.
Změna brokeru: `systemctl --user restart vista-raw-broker.service`, bez běžících
úloh. Žádný z těchto příkazů nerestartuje původní VISTA.

Kontejnery mají `restart: unless-stopped`. Broker je uživatelská systemd služba;
na tomto hostu není zapnutý linger, proto pro broker musí běžet uživatelská
systemd relace (typicky po přihlášení uživatele epr).

`docker-compose.yml` je samostatné vývojové prostředí s jinými porty a vlastní
datovou volume. Nenasazuje hostitelský GPU broker; na epr-bedna se používá
výše uvedený `start.sh`. Staré pokyny zdrojové zálohy v `SOURCE-SNAPSHOT.md`
popisují stav před RAW větví.

## Ověření

```sh
cd ~/git/vista-inspection-raw/backend
# Uvnitř RAW backend kontejneru, kde je nastavené soukromé prostředí:
docker exec -e VISTA_TEST_DNG=/media/epr/ssd/vista-work/datasets/corvallis-bridge/VLABDW/photos/DJI_0194.DNG \
  vista-raw-backend /home/epr/git/vista-inspection-raw/backend/.venv/bin/python \
  -m unittest discover -s tests -v
```

Regresní testy pokrývají zákaz cizích mountů, symlink úniky, oddělení cest,
ochranu originálu a diskové rezervy při uploadu/ZIP, rozměry PNG a skutečný DNG → RGB → COLMAP
roundtrip včetně shody pixelů při nulovém zkreslení. Frontend se ověřuje
`npm run build` (TypeScript + Vite).

Praktické ověření 16. 10. 2026: všech osm regresních testů a build webu prošly.
V prohlížeči bylo ověřeno nahrání skutečného DNG, ruční spuštění místo
automatické rekonstrukce, galerie 798 snímků a zobrazení fotografie v rozlišení
5472 × 3648. Stažený originál má shodný MD5 s manifestem datasetu.
Testovací SfM zaregistrovalo všech 20 kamer a vytvořilo 98 885 landmarků;
vstupy byly originály DNG. Pro OpenMVS vzniklo 20 PNG v plném rozlišení.
Při výpočtu hloubkových map byl zaznamenán proces `DensifyPointCloud` na GPU
s vytížením až 100 %.

Rekonstrukce testovací sady v režimu Maximální přesnost byla dokončena včetně
textur, tří LOD úrovní a 41 streamovaných dlaždic. Test využil obnovení od
texturování po opravě výše popsaného pádu OBJ; SfM ani hustá rekonstrukce se
neopakovaly. Výsledný model a pozice kamer se otevřely v prohlížeči bez chyb
JavaScriptu a HTTP. Celá sada 798 DNG zůstává ve stavu připraveno.

Report kvality vytváří náhledy přes Mesa na CPU v režimu bez displeje
(`EGL_PLATFORM=surfaceless`), aby nepotřeboval GPU uvnitř API kontejneru;
viz [dokumentace Open3D](https://www.open3d.org/docs/release/tutorial/visualization/cpu_rendering.html).
Byl ověřen report se třemi pohledy porovnanými se zdrojovými DNG.

Výsledky praktického ověření, checksumy původní instalace a manifest importu
jsou v soukromém `operations/`. Fotografie, modely, náhledy, databáze a logy
se do repozitáře neukládají.
