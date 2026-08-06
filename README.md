# Bazoš.sk monitor — Auto-Moto

Sleduje nové inzeráty na [auto.bazos.sk](https://auto.bazos.sk) podľa tvojich preferencií
a pri zhode ti pošle správu na **WhatsApp** a/alebo **email**.

Prvá preferencia je už nastavená: **VW Passat do 7000 EUR**.

---

## 1. Inštalácia

```powershell
cd c:\Sandbox\AUPARK\Dashbord\kupa_auta_monitring
pip install -r requirements.txt
```

> Na tomto počítači zlyhávalo overovanie HTTPS certifikátov (`CERTIFICATE_VERIFY_FAILED`) —
> rieši to balík `pip-system-certs`, ktorý je už v `requirements.txt` a nainštalovaný.
> Ak by pip zlyhal na certifikáte, použi:
> `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt`

## 2. Nastavenie notifikácií

Otvor `config.json` a zapni si aspoň jeden kanál (`"enabled": true`).

Heslá sa nepíšu priamo do configu — hodnota `"env:NAZOV"` znamená,
že sa načíta z premennej prostredia `NAZOV`.

### Email (Gmail)

1. Zapni si dvojfaktorové overenie na Google účte.
2. Vytvor **App password**: https://myaccount.google.com/apppasswords
3. Nastav premennú a zapni kanál:

```powershell
setx BAZOS_EMAIL_PASSWORD "xxxx xxxx xxxx xxxx"
```

V `config.json` nastav `notifications.email.enabled` na `true`.

### WhatsApp — CallMeBot (zadarmo, odporúčané na začiatok)

1. Ulož si číslo **+34 644 51 95 23** do kontaktov.
2. Pošli mu cez WhatsApp správu: `I allow callmebot to send me messages`
3. Príde ti späť **apikey**.

```powershell
setx BAZOS_CALLMEBOT_KEY "123456"
```

V `config.json` doplň svoje číslo do `callmebot.phone` (formát `+421903123456`)
a nastav `notifications.whatsapp.enabled` na `true`.

### WhatsApp — Twilio (platené, spoľahlivejšie na dlhodobú prevádzku)

Nastav `"provider": "twilio"` a premenné `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.

> Po `setx` musíš otvoriť **nové** okno PowerShellu, aby sa premenné načítali.

## 3. Otestuj notifikácie

```powershell
python script.py --test-notify
```

## 4. Spustenie

```powershell
python script.py --once --dry-run   # skúšobný prechod, nič neposiela
python script.py --seed             # zapamätá si aktuálne inzeráty ako "videné"
python script.py                    # nekonečná slučka, kontrola každých 15 min
```

Prvý ostrý beh sa **automaticky nasejduje** — zapamätá si všetko, čo je na Bazoši teraz,
a nepošle ti 60 správ naraz. Notifikácie prídu až na inzeráty pridané **potom**.

## 5. Prevádzka na GitHub Actions (aktuálny režim)

Monitor beží na **GitHub Actions**, nie na lokálnom počítači:
https://github.com/martinkos11/bazos-auto-monitor

Workflow [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) sa spúšťa
každých 15 minút, urobí jeden prechod (`python script.py --once`) a **commitne
`seen.sqlite3` späť do repa** — to je jediná pamäť medzi behmi, runner je zakaždým
čistý stroj.

### Secrets

Repo je verejné, takže v `config.json` nesmie byť nič osobné. Všetky citlivé hodnoty
sú `"env:NAZOV"` a reálne hodnoty sú v **Settings → Secrets and variables → Actions**:

| Secret | Použitie |
|---|---|
| `BAZOS_PHONE` | Číslo pre WhatsApp notifikácie. |
| `BAZOS_CALLMEBOT_KEY` | CallMeBot apikey. |
| `BAZOS_EMAIL_USER` | *(voliteľné)* Gmail adresa, ak zapneš email kanál. |
| `BAZOS_EMAIL_PASSWORD` | *(voliteľné)* Gmail App password. |

```powershell
gh secret set BAZOS_PHONE --body "+421903123456"
```

### Ovládanie

```powershell
gh workflow run monitor.yml        # spustiť hneď, nečakať na cron
gh run list --workflow monitor.yml # posledné behy
gh run view --log                  # log konkrétneho behu
gh workflow disable monitor.yml    # dočasne vypnúť
```

Zmena preferencií = upraviť `config.json`, `git commit` a `git push`. Ďalší beh
už pobeží podľa nového nastavenia.

### Na čo si dať pozor

- **Cron nie je presný.** Pri záťaži GitHubu behy meškajú 5–20 minút a občas sa preskočia.
- **Naplánované workflowy sa vypnú po 60 dňoch nečinnosti repa.** Automatické commity
  databázy to väčšinou udržia živé, ale ak by notifikácie prestali chodiť, pozri sa
  do záložky Actions, či nie je workflow deaktivovaný.
- **Nespúšťať monitor lokálne aj na GitHube naraz** — každý inzerát by prišiel dvakrát
  a obe kópie `seen.sqlite3` by sa rozišli.
- **Bazoš GitHub runnery neblokuje** — overené, načíta plných 60 inzerátov na prechod.

---

## 6. Spustenie na pozadí lokálne (záložná možnosť)

Aby monitor bežal aj po reštarte a bez otvoreného okna, je v **Startup priečinku**
zástupca `Bazos Monitor.lnk`, ktorý pri prihlásení spustí `pythonw script.py`.

```powershell
# vytvorenie / obnovenie zástupcu
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Bazos Monitor.lnk"
$sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sc.TargetPath = "C:\Users\marti\AppData\Local\Programs\Python\Python39\pythonw.exe"
$sc.Arguments = '"c:\Sandbox\AUPARK\Dashbord\kupa_auta_monitring\script.py"'
$sc.WorkingDirectory = "c:\Sandbox\AUPARK\Dashbord\kupa_auta_monitring"
$sc.WindowStyle = 7
$sc.Save()
```

Zrušenie autoštartu = zmazať ten `.lnk`. Zastavenie bežiaceho monitora:
`Stop-Process -Name pythonw`.

> **Prečo nie Task Scheduler s `/sc onstart`?** Taká úloha beží pod účtom SYSTEM,
> ktorý **nevidí používateľské premenné prostredia** — `BAZOS_CALLMEBOT_KEY` ani
> `BAZOS_EMAIL_PASSWORD` by sa nenačítali a notifikácie by ticho zlyhávali.
> Navyše `schtasks /create` tu vyžaduje spustenie PowerShellu ako správca.
> Ak by si ho aj tak chcel, použi `/sc onlogon` (beží pod tvojím účtom) a celý
> príkaz napíš **na jeden riadok** — `^` na konci riadku je pokračovanie riadku
> v `cmd.exe`, nie v PowerShelli (tam je to spätný apostrof `` ` ``).

Alebo jednoducho — nechaj otvorené okno PowerShellu so spusteným `python script.py`.

### Kontrola, či beží

```powershell
Get-Process pythonw                                    # beží?
Get-Content monitor.log -Tail 20 -Wait                 # čo práve robí
```

Monitor zapisuje do `monitor.log` v priečinku skriptu — pod `pythonw` nie je konzola,
takže log je jediný spôsob, ako vidieť, čo sa deje.

---

## Preferencie (`config.json`)

```json
{
  "name": "VW Passat do 7000 EUR",
  "query": "passat",
  "category": "volkswagen",
  "price_max": 7000,
  "price_min": 500,
  "must_contain": ["passat"],
  "must_not_contain": ["disky", "diely", "rozpredam", "kupim"]
}
```

| Pole | Význam |
|---|---|
| `name` | Názov preferencie. Používa sa aj ako kľúč v databáze — **ak ho zmeníš, preferencia sa nasejduje odznova**. |
| `query` | Text, ktorý sa hľadá priamo na Bazoši. |
| `category` | Podsekcia — `volkswagen`, `audi`, `bmw`, `skoda`, `ford`… Prázdne = celá Auto sekcia (vtedy sa medzi výsledky miešajú aj diely a disky). |
| `price_min` / `price_max` | Cenový rozsah v EUR. Posiela sa aj Bazošu, aj sa kontroluje lokálne. |
| `max_pages` | Koľko strán výsledkov prejsť (20 inzerátov na stranu). |
| `must_contain` | Všetky tieto slová musia byť v inzeráte (AND). Diakritika sa ignoruje. |
| `must_contain_any` | Stačí **jedno** zo slov (OR). Pri porovnaní sa ignorujú medzery, takže `"110kw"` sadne aj na `110 kW` — predajcovia výkon píšu oboma spôsobmi. |
| `must_not_contain` | Ak je ktorékoľvek slovo v **nadpise**, inzerát sa preskočí. |
| `must_not_contain_scope` | `title` (predvolené) alebo `full`. `full` prehľadáva aj popis — pozor, zahodí aj skutočné autá, ktoré majú vo výbave napr. „hliníkové disky". |
| `must_contain_scope` | `full` (predvolené) alebo `title` — prísnejšie, slovo musí byť priamo v nadpise. |
| `allow_missing_price` | Či posielať aj inzeráty s cenou „Dohodou". |
| `skip_top_ads` | `true` = ignorovať platené TOP inzeráty. |
| `location_zip` + `radius_km` | Napr. `"85101"` a `50` — len inzeráty do 50 km od daného PSČ. |

### Pridanie ďalšej preferencie

Do poľa `preferences` pridaj ďalší objekt, napr.:

```json
{
  "name": "Octavia combi do 5000",
  "enabled": true,
  "query": "octavia combi",
  "category": "skoda",
  "price_max": 5000,
  "must_contain": ["octavia"],
  "must_not_contain": ["disky", "diely", "rozpredam"]
}
```

---

## Súbory

| Súbor | Popis |
|---|---|
| `script.py` | Samotný monitor. |
| `config.json` | Preferencie a nastavenie notifikácií. |
| `seen.sqlite3` | Databáza videných inzerátov. Vymazaním sa monitor nasejduje odznova. |
| `monitor.log` | Priebeh behu. Jediná spätná väzba, keď monitor beží na pozadí. |

## Riešenie problémov

**Nechodia žiadne notifikácie** — normálne. Posielajú sa len na inzeráty pridané *po*
prvom behu. Otestuj kanály cez `--test-notify`.

**Chcem vidieť, čo by poslal, hneď teraz** — vymaž `seen.sqlite3`, spusti `--seed`,
potom v databáze zmaž pár riadkov a spusti `--once --dry-run`.

**Príliš veľa/málo výsledkov** — uprav `must_contain` / `must_not_contain` a spusti
`--once --dry-run -v`, kde `-v` vypíše aj dôvod preskočenia každého inzerátu.

**Bazoš prestal vracať výsledky** — stránka mohla zmeniť HTML. Selektory sú
v `parse_listings()` v `script.py` (`div.inzeraty`, `h2.nadpis`, `div.inzeratycena`).
