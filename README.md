# 🎟 Ticket Prijstracker – Ploegendienst & Oranje Zoet 2026

Een automatische prijstracker voor TicketSwap-tickets, gehost via GitHub Pages.  
Elke 2 uur worden de laagste prijzen voor 2 tickets opgehaald en opgeslagen.  
Je krijgt automatisch een e-mail als de prijs onder jouw drempel zakt.

---

## Stap 1 – Repository klaarmaken op GitHub

1. Ga naar [github.com](https://github.com) en maak een account aan (of log in).
2. Klik rechtsboven op **+** → **New repository**.
3. Geef het de naam `ploegendienst-oranjezoet-dashboard`.
4. Zet hem op **Public** (vereist voor gratis GitHub Pages).
5. Klik **Create repository** – laat alles leeg.

## Stap 2 – Bestanden uploaden

**Optie A – Via de website (eenvoudigst):**

1. Open je nieuwe lege repository op GitHub.
2. Klik **uploading an existing file** (of **Add file → Upload files**).
3. Sleep de volledige inhoud van deze map erheen:
   - `index.html`
   - `config.json`
   - `data/prices.json`
   - `scraper/scrape.py`
   - `scraper/requirements.txt`
   - `.github/workflows/scrape.yml`
4. Klik **Commit changes**.

**Optie B – Via de terminal (als je Git hebt):**

```bash
git remote add origin https://github.com/JOUW_GEBRUIKERSNAAM/ploegendienst-oranjezoet-dashboard.git
git push -u origin main
```

## Stap 3 – GitHub Pages inschakelen

1. Ga naar je repository op GitHub.
2. Klik op **Settings** (tandwiel-tabblad).
3. Scroll naar **Pages** in het linkermenu.
4. Stel onder **Source** in: **Deploy from a branch**.
5. Branch: `main`, map: `/ (root)`.
6. Klik **Save**.

Na ~1 minuut is je dashboard bereikbaar op:  
`https://JOUW_GEBRUIKERSNAAM.github.io/ploegendienst-oranjezoet-dashboard`

## Stap 4 – TicketSwap-URLs instellen

Open `config.json` en vul de juiste URLs in:

1. Ga naar [ticketswap.nl](https://www.ticketswap.nl) en zoek de evenementen.
2. Kopieer de URL van de overzichtspagina van elk evenement.
3. Bewerk `config.json` (klik op het bestand in GitHub, dan het potlood-icoon):

```json
{
  "email": "jouw@email.com",
  "threshold": 100,
  "events": {
    "ploegendienst": {
      "name": "Ploegendienst Kingsnight Special 2026",
      "url": "https://www.ticketswap.nl/event/..."
    },
    "oranjezoet": {
      "name": "Oranje Zoet 2026",
      "url": "https://www.ticketswap.nl/festival-tickets/a/oranje-zoet"
    }
  }
}
```

Sla op met **Commit changes**.

## Stap 5 – E-mailalerts instellen

De scraper verstuurt e-mails via SMTP (bijv. Gmail).  
Sla de inloggegevens veilig op als **GitHub Secrets** (ze zijn nooit zichtbaar in de code).

### Gmail instellen (aanbevolen)

1. Zet in je Google-account **2-stapsverificatie** aan.
2. Ga naar [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Maak een app-wachtwoord aan voor "Mail" → kopieer het (16 tekens).

### Secrets toevoegen aan GitHub

1. Ga naar je repository → **Settings** → **Secrets and variables** → **Actions**.
2. Klik **New repository secret** en voeg toe:

| Naam            | Waarde                    |
|-----------------|---------------------------|
| `SMTP_SERVER`   | `smtp.gmail.com`          |
| `SMTP_PORT`     | `587`                     |
| `SMTP_USER`     | `jouwemail@gmail.com`     |
| `SMTP_PASSWORD` | *(het app-wachtwoord)*    |

### E-mailadres en drempel instellen

Bewerk `config.json` (zie Stap 4) of gebruik de **instellingenpagina** op je dashboard.

## Stap 6 – Eerste keer handmatig uitvoeren

1. Ga naar je repository → **Actions**.
2. Klik links op **Scrape Ticket Prices**.
3. Klik **Run workflow** → **Run workflow**.
4. Wacht ~2 minuten totdat de workflow klaar is.
5. Ververs je dashboard – je ziet nu de eerste prijs!

Daarna draait de scraper automatisch elke 2 uur.

---

## Drempel aanpassen via de pagina

Op je dashboard staat een **Instellingen**-sectie.  
Wil je de drempel opslaan zonder het bestand handmatig te bewerken?

1. Maak een **GitHub Personal Access Token** aan:
   - GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**
   - **Repository access**: alleen dit repository
   - **Permissions** → **Contents**: `Read and write`
2. Klik **Generate token** en kopieer hem.
3. Plak het token in het veld **Geavanceerd** op het dashboard.
4. Sla je instellingen op – ze worden automatisch opgeslagen in `config.json`.

---

## Veelgestelde vragen

**De scraper vindt geen prijzen – wat nu?**  
TicketSwap kan zijn lay-out aanpassen. Check de log in GitHub Actions (tab **Actions**).  
Probeer ook de URL te controleren: ga zelf naar TicketSwap en kopieer de exacte URL van het evenement.

**Hoe vaak controleert de scraper?**  
Elke 2 uur (zie `.github/workflows/scrape.yml`). Je kunt dit aanpassen door de `cron`-regel te wijzigen.  
Voorbeeld: elk uur → `'0 * * * *'`, elke 30 minuten → `'*/30 * * * *'`.

**Ik ontvang geen e-mail – wat controleer ik?**  
- Zijn de SMTP-secrets correct ingesteld? (Stap 5)
- Staat het juiste e-mailadres in `config.json`?
- Check de workflow-log in GitHub Actions op foutmeldingen.
- Gmail: zorg dat je een **app-wachtwoord** gebruikt, niet je normale wachtwoord.

**Hoelang wordt de prijshistorie bewaard?**  
De laatste 360 metingen (~30 dagen bij 2u interval). Oudere data wordt automatisch verwijderd.
