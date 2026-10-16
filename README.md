# VISTA RAW

Větev `feat/raw-isolated` přidává práci s DNG a samostatnou instanci na
`epr-bedna`, web **http://10.10.1.80:5174**. Zdrojová pracovní kopie je
`~/git/vista-inspection-raw`; původní `~/VISTA` i hlavní větev
`~/git/vista-inspection` zůstávají oddělené.

Pro nasazení, popis RAW pipeline, limity a ověření viz [docs/RAW.md](docs/RAW.md).
`./start.sh` spouští výhradně novou instanci; neukončuje původní procesy.

Repo obsahuje pouze zdrojový kód. DNG, JPEG náhledy, databáze, modely,
závislosti a lokální konfigurace patří mimo Git.

Původní dokumentace zdrojové zálohy je v [docs/SOURCE-SNAPSHOT.md](docs/SOURCE-SNAPSHOT.md).
