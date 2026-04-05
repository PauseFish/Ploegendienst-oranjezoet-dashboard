"""
TicketSwap doorverkoop prijsscraper – Ploegendienst & Oranje Zoet 2026

Gebruikt playwright-stealth om bot-detectie te omzeilen zodat de
doorverkoop-listings daadwerkelijk geladen worden.
"""

import asyncio
import json
import os
import re
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def get_lowest_resale_price(page, url: str, debug_dir: Path | None = None) -> float | None:
    """
    Navigeert naar de TicketSwap-pagina en haalt de laagste doorverkoop-prijs
    per ticket op. Gebruikt stealth-modus om bot-detectie te omzeilen.
    """
    print(f"  → Navigeren naar: {url}")

    try:
        # Wacht op networkidle zodat de listings feed geladen is
        await page.goto(url, wait_until="networkidle", timeout=60000)
        # Extra wachttijd zodat React de doorverkoop-kaarten rendert
        await page.wait_for_timeout(4000)
    except Exception as e:
        print(f"  ✗ Laden mislukt: {e}")
        return None

    # Debug: sla screenshot + HTML op als DEBUG=1
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", url.rstrip("/").split("/")[-2])
        await page.screenshot(path=str(debug_dir / f"{slug}.png"), full_page=True)
        (debug_dir / f"{slug}.html").write_text(await page.content(), encoding="utf-8")
        print(f"  ℹ Debug opgeslagen in {debug_dir}/{slug}.*")

    # Check of de foutmelding zichtbaar is (bot-detectie)
    error_visible = await page.evaluate("""() => {
        const text = document.body.innerText || '';
        return text.includes('Er is iets misgegaan') || text.includes('something went wrong');
    }""")
    if error_visible:
        print("  ⚠ TicketSwap toont foutmelding – bot-detectie actief. Probeer opnieuw met wachttijd.")
        await page.wait_for_timeout(3000)

    # Zoek alle prijzen op de pagina; de doorverkoop-kaarten staan in een feed
    prices = await page.evaluate("""() => {
        // Officiële prijs staat in een 'Officiële ticketshop' sectie
        // De doorverkoop-feed staat erna als een lijst van kaarten
        const officialRe = /officieel|official|face.?value|originele.?prijs|officiële.?ticketshop/i;

        function parsePrices(text) {
            const prices = [];
            // Matcht: €45,00 / €45.00 / € 45,00 / € 45.00
            const re = /\u20ac\s*(\d{1,3})[,.](\d{2})/g;
            let m;
            while ((m = re.exec(text)) !== null) {
                const p = parseFloat(m[1] + '.' + m[2]);
                if (p > 5 && p < 500) prices.push(p);
            }
            return prices;
        }

        // Strategie 1: zoek de doorverkoop-feed via data-testid of specifieke klassen
        const feedSelectors = [
            '[data-testid="listings"]',
            '[data-testid="listing-list"]',
            '[class*="ListingList"]',
            '[class*="listing-list"]',
            '[class*="listings-feed"]',
            '[class*="ListingsFeed"]',
            '[class*="AvailableListings"]',
            '[class*="available-listings"]',
        ];

        for (const sel of feedSelectors) {
            const el = document.querySelector(sel);
            if (el) {
                const prices = parsePrices(el.innerText || '');
                if (prices.length > 0) {
                    console.log('Feed via selector ' + sel + ':', prices);
                    return prices;
                }
            }
        }

        // Strategie 2: loop door alle secties en sla de officiële sectie over
        // De doorverkoop-kaarten zitten na de 'Officiële ticketshop' kop
        const sections = [...document.querySelectorAll('section, [class*="Section"], [class*="section"]')];
        for (const section of sections) {
            const headerText = section.querySelector('h1,h2,h3,h4')?.innerText || '';
            if (officialRe.test(headerText)) continue;
            const prices = parsePrices(section.innerText || '');
            if (prices.length > 1) {
                // Meerdere prijzen in één sectie = doorverkoop-feed
                console.log('Feed via section:', prices);
                return prices;
            }
        }

        // Strategie 3: groepeer li/article-elementen per ouder,
        // kies de groep met de meeste kaarten (= listings-feed)
        const candidates = [...document.querySelectorAll('li, article')].filter(el => {
            const text = el.innerText || '';
            if (!/\u20ac\s*\d/.test(text)) return false;
            // Sla officiële elementen over (check zichzelf + 5 voorouders)
            let node = el;
            for (let i = 0; i < 5; i++) {
                if (!node) break;
                const t = (node.className || '') + ' ' + (node.getAttribute?.('aria-label') || '')
                        + ' ' + (node.innerText || '').substring(0, 100);
                if (officialRe.test(t)) return false;
                node = node.parentElement;
            }
            return true;
        });

        const groups = new Map();
        for (const el of candidates) {
            const key = el.parentElement || document.body;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(el);
        }

        let bestGroup = [];
        for (const items of groups.values()) {
            if (items.length > bestGroup.length) bestGroup = items;
        }

        if (bestGroup.length >= 1) {
            const prices = [];
            for (const el of bestGroup) {
                prices.push(...parsePrices(el.innerText || ''));
            }
            if (prices.length > 0) {
                console.log('Feed via groep (' + bestGroup.length + ' items):', prices);
                return prices;
            }
        }

        // Strategie 4: fallback – verzamel alle prijzen maar sla officiële sectie over
        const allText = document.body.innerText || '';
        // Haal de officiële prijs-sectie eruit door te splitsen op de kop
        const splitIdx = allText.indexOf('Officiële ticketshop');
        let searchText = allText;
        if (splitIdx >= 0) {
            // Neem het stuk NA de officiële sectie (begin van volgende alinea)
            const afterOfficial = allText.indexOf('\n\n', splitIdx + 30);
            if (afterOfficial > 0) searchText = allText.substring(afterOfficial);
        }

        const fallbackPrices = parsePrices(searchText);
        if (fallbackPrices.length > 0) {
            console.log('Fallback pagina-prijzen:', fallbackPrices);
            return fallbackPrices;
        }

        // Alles mislukken: log de volledige pagina-tekst voor debug
        console.log('Volledige paginatekst (eerste 2000 tekens):', allText.substring(0, 2000));
        return [];
    }""")

    if prices:
        uniq = sorted(set(round(p, 2) for p in prices))
        print(f"  ℹ Gevonden doorverkoop-prijzen: {uniq}")
        result = round(min(prices), 2)
        print(f"  ✓ Laagste doorverkoop: €{result:.2f} per ticket")
        return result

    print("  ✗ Geen doorverkoop-listings gevonden")
    return None


# ─── Alert-cooldown ───────────────────────────────────────────────────────────

def _should_send_alert(entries: list[dict], event_key: str) -> bool:
    for entry in reversed(entries):
        if entry.get(f"{event_key}_alerted"):
            last = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last < timedelta(hours=24):
                return False
            break
    return True


# ─── E-mail ───────────────────────────────────────────────────────────────────

def send_alert_email(config: dict, alerts: list[tuple[str, float, float]]) -> None:
    smtp_server   = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port     = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user     = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    recipient     = config.get("email", "")
    dashboard_url = config.get("github_pages_url", "#")

    if not all([smtp_user, smtp_password, recipient]):
        print("  ⚠ SMTP niet geconfigureerd – stel secrets in bij GitHub Actions")
        return
    if recipient == "jouw@email.com":
        print("  ⚠ Vul een echt e-mailadres in config.json")
        return

    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 16px'>{name}</td>"
        f"<td style='padding:8px 16px;font-weight:bold;color:#ff6b35'>€{price:.2f}</td>"
        f"<td style='padding:8px 16px;color:#888'>2× = €{price*2:.2f} · drempel €{thr:.2f}</td>"
        f"</tr>"
        for name, price, thr in alerts
    )

    html = f"""<html><body style="font-family:sans-serif;background:#0f0f1a;color:#e0e0e0;padding:2rem">
      <div style="max-width:600px;margin:auto;background:#1a1a2e;border-radius:12px;
                  padding:2rem;border:1px solid #2a2a45">
        <h2 style="color:#ff6b35;margin-top:0">🎟 Prijsalert!</h2>
        <p>Doorverkoop-tickets zijn onder jouw drempel gedaald:</p>
        <table style="width:100%;border-collapse:collapse;margin:1rem 0">
          <thead><tr style="background:#0f0f1a;color:#888">
            <th style="padding:8px 16px;text-align:left">Evenement</th>
            <th style="padding:8px 16px;text-align:left">Per ticket</th>
            <th style="padding:8px 16px;text-align:left">Totaal / drempel</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <a href="https://{dashboard_url}" style="display:inline-block;background:#ff6b35;
           color:white;padding:.75rem 1.5rem;border-radius:8px;text-decoration:none;
           font-weight:bold">Bekijk dashboard →</a>
      </div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🎟 Prijsalert: doorverkoop-tickets onder drempel!"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as s:
            s.ehlo(); s.starttls(); s.login(smtp_user, smtp_password)
            s.sendmail(smtp_user, recipient, msg.as_string())
        print(f"  ✓ Alert verstuurd naar {recipient}")
    except Exception as e:
        print(f"  ✗ E-mail mislukt: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    base_dir = Path(__file__).parent.parent

    with open(base_dir / "config.json") as f:
        config = json.load(f)

    prices_path = base_dir / "data" / "prices.json"
    prices_path.parent.mkdir(exist_ok=True)
    if prices_path.exists() and prices_path.stat().st_size > 2:
        with open(prices_path) as f:
            price_data: list[dict] = json.load(f)
    else:
        price_data = []

    debug_dir: Path | None = None
    if os.environ.get("DEBUG") == "1":
        debug_dir = base_dir / "debug"

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_entry: dict = {"timestamp": now_iso}
    alerts_to_send: list[tuple[str, float, float]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
            viewport={"width": 1280, "height": 900},
            # Stel extra HTTP-headers in zoals een echte browser
            extra_http_headers={
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        page = await context.new_page()

        # Activeer stealth-modus: verbergt tekenen van geautomatiseerde browser
        await stealth_async(page)

        # Log browser console berichten voor debuggen
        page.on(
            "console",
            lambda msg: print(f"  [browser] {msg.text[:300]}")
            if msg.type in ("log", "warn", "error")
            else None,
        )

        for key, event_info in config.get("events", {}).items():
            name = event_info.get("name", key)
            url  = event_info.get("url", "")
            print(f"\n[{name}]")

            if not url:
                new_entry[f"{key}_price"]   = None
                new_entry[f"{key}_alerted"] = False
                continue

            price = await get_lowest_resale_price(page, url, debug_dir=debug_dir)
            new_entry[f"{key}_price"] = price

            threshold = float(event_info.get("threshold", 0))
            alert_flag = False
            if price is not None and threshold > 0 and price < threshold:
                if _should_send_alert(price_data, key):
                    alerts_to_send.append((name, price, threshold))
                    alert_flag = True
                    print(f"  🔔 Alert: €{price:.2f} < drempel €{threshold:.2f}")
                else:
                    print("  ℹ Al gewaarschuwd (cooldown)")
            new_entry[f"{key}_alerted"] = alert_flag

        await browser.close()

    # Bewaar laatste 1440 metingen (~30 dagen bij 30min interval)
    price_data.append(new_entry)
    if len(price_data) > 1440:
        price_data = price_data[-1440:]

    with open(prices_path, "w") as f:
        json.dump(price_data, f, indent=2)
    print(f"\n✓ {len(price_data)} metingen opgeslagen")

    if alerts_to_send:
        print("\n📧 Alert e-mail versturen...")
        send_alert_email(config, alerts_to_send)


if __name__ == "__main__":
    asyncio.run(main())
