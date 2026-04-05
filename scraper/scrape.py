"""
TicketSwap doorverkoop prijsscraper – Ploegendienst & Oranje Zoet 2026

Strategie: zoek de groep list-items op de pagina met de meeste exemplaren
die elk een prijs bevatten → dat is de doorverkoop-feed, niet de officiële prijs.
"""

import asyncio
import json
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def get_lowest_resale_price(page, url: str) -> float | None:
    """
    Navigeert naar de TicketSwap-pagina en haalt de laagste doorverkoop-prijs
    per ticket op. Negeert de officiële/face-value prijs.
    """
    print(f"  → Navigeren naar: {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  ✗ Laden mislukt: {e}")
        return None

    prices: list[float] = await page.evaluate("""() => {
        // Prijs-regex: matcht €45,00 / €45.00 / € 45,00
        const priceRe = /\u20ac\s*(\\d{1,3})[,.](\\d{2})/g;

        // Sleutelwoorden die wijzen op de officiële verkoop – overslaan
        const officialRe = /officieel|official|face.?value|originele.?prijs|gezichtswaarde|organisatie/i;

        function parsePrices(text) {
            const found = [];
            let m;
            priceRe.lastIndex = 0;
            while ((m = priceRe.exec(text)) !== null) {
                const p = parseFloat(m[1] + '.' + m[2]);
                if (p > 5 && p < 500) found.push(p);
            }
            return found;
        }

        // --- Strategie 1: groepeer li/article-elementen per directe ouder ---
        // De doorverkoop-feed bestaat uit MEERDERE kaarten in dezelfde ouder.
        // De officiële prijs staat los (1 element of aparte sectie).
        const candidates = [...document.querySelectorAll('li, article')].filter(el => {
            const text = el.innerText || '';
            if (!/\u20ac\s*\\d/.test(text)) return false;           // geen prijs
            if (officialRe.test(text)) return false;                  // officieel
            // Check ook aria-labels van voorouders
            let p = el.parentElement;
            for (let i = 0; i < 4; i++) {
                if (!p) break;
                const label = p.getAttribute('aria-label') || '';
                if (officialRe.test(label)) return false;
                p = p.parentElement;
            }
            return true;
        });

        // Groepeer per ouder-element
        const groups = new Map();
        for (const el of candidates) {
            const key = el.parentElement || 'root';
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(el);
        }

        // Kies de grootste groep (meeste kaarten = listings-feed)
        let bestGroup = [];
        for (const items of groups.values()) {
            if (items.length > bestGroup.length) bestGroup = items;
        }

        if (bestGroup.length >= 1) {
            const found = [];
            for (const el of bestGroup) {
                found.push(...parsePrices(el.innerText || ''));
            }
            if (found.length > 0) return found;
        }

        // --- Strategie 2: zoek sectie met 'listing'/'beschikbaar' in class/id ---
        const sectionEl = [...document.querySelectorAll('[class*="listing"],[class*="Listing"],[id*="listing"],[class*="beschikbaar"]')].find(el => {
            const text = el.innerText || '';
            return /\u20ac\s*\\d/.test(text) && !officialRe.test(text);
        });
        if (sectionEl) return parsePrices(sectionEl.innerText || '');

        return [];
    }""")

    if not prices:
        print("  ✗ Geen doorverkoop-listings gevonden")
        return None

    # Log alle gevonden prijzen voor debuggen
    uniq = sorted(set(round(p, 2) for p in prices))
    print(f"  ℹ Gevonden doorverkoop-prijzen: {uniq}")
    result = round(min(prices), 2)
    print(f"  ✓ Laagste doorverkoop: €{result:.2f} per ticket")
    return result


# ─── Alert-cooldown ───────────────────────────────────────────────────────────

def _should_send_alert(entries: list[dict], event_key: str) -> bool:
    """Stuur alleen een alert als de vorige meer dan 24 uur geleden was."""
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

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_entry: dict = {"timestamp": now_iso}
    alerts_to_send: list[tuple[str, float, float]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="nl-NL",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for key, event_info in config.get("events", {}).items():
            name = event_info.get("name", key)
            url  = event_info.get("url", "")
            print(f"\n[{name}]")

            if not url:
                new_entry[f"{key}_price"]   = None
                new_entry[f"{key}_alerted"] = False
                continue

            price = await get_lowest_resale_price(page, url)
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
