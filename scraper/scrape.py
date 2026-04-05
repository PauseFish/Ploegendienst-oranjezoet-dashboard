"""
TicketSwap prijsscraper – Ploegendienst & Oranje Zoet 2026
Draait via GitHub Actions elke 2 uur.
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


async def get_lowest_price_for_two_tickets(page, url: str) -> float | None:
    """
    Haalt de laagste beschikbare ticketprijs op van een TicketSwap-pagina
    en geeft de prijs terug voor 2 tickets.
    """
    try:
        print(f"  → Navigeren naar: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Wacht totdat de dynamische content is geladen
        await page.wait_for_timeout(4000)

        # Probeer te wachten op ticketlijst
        try:
            await page.wait_for_selector(
                "[class*='listing'], [class*='ticket'], [data-testid*='listing']",
                timeout=10000,
            )
        except Exception:
            pass  # Ga door ook als selector niet gevonden wordt

        prices = []

        # Strategie 1: zoek elementen met price in class of data-testid
        price_els = await page.query_selector_all(
            "[class*='price'], [data-testid*='price'], [class*='Price']"
        )
        for el in price_els:
            try:
                text = await el.inner_text()
                found = _parse_prices(text)
                prices.extend(found)
            except Exception:
                pass

        # Strategie 2: zoek listing-/ticket-elementen en scan hun tekst
        if not prices:
            listing_els = await page.query_selector_all(
                "li[class], article[class], [class*='listing'], [class*='ticket-item']"
            )
            for el in listing_els:
                try:
                    text = await el.inner_text()
                    found = _parse_prices(text)
                    prices.extend(found)
                except Exception:
                    pass

        # Strategie 3: scan de volledige paginatekst als fallback
        if not prices:
            body_text = await page.inner_text("body")
            prices = _parse_prices(body_text)

        if not prices:
            print("  ✗ Geen prijzen gevonden op pagina")
            return None

        min_price = min(prices)
        total = round(min_price * 2, 2)
        print(f"  ✓ Laagste prijs per ticket: €{min_price:.2f} → 2 tickets: €{total:.2f}")
        return total

    except Exception as exc:
        print(f"  ✗ Fout bij scrapen van {url}: {exc}")
        return None


def _parse_prices(text: str) -> list[float]:
    """Extraheert alle geldige ticketprijzen (€5–€500) uit een stuk tekst."""
    prices = []
    # Matcht: €45,00 / € 45.00 / 45,00 / 45.00
    for m in re.finditer(r"(?:€\s*)?(\d{1,3})(?:[,\.](\d{2}))\b", text):
        euro_part = int(m.group(1))
        cent_part = int(m.group(2)) if m.group(2) else 0
        price = euro_part + cent_part / 100
        if 5 < price < 500:
            prices.append(price)
    return prices


def _should_send_alert(entries: list[dict], event_key: str) -> bool:
    """
    Stuur een alert als:
    - De vorige meting GEEN alert had verzonden, OF
    - De laatste alert meer dan 24 uur geleden is.
    """
    alert_key = f"{event_key}_alerted"
    if not entries:
        return True
    # Zoek de meest recente vermelding met alerted=True
    for entry in reversed(entries[:-1]):  # sla de nieuwste over (die we nu toevoegen)
        if entry.get(alert_key):
            last_ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if now - last_ts < timedelta(hours=24):
                return False  # Al gewaarschuwd binnen 24 uur
            break
    return True


def send_alert_email(config: dict, alerts: list[tuple[str, float, float]]):
    """Verstuurt een e-mailalert als de prijs onder de drempel zakt.
    alerts = [(event_name, price_2tickets, threshold_per_ticket), ...]
    """
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    recipient = config.get("email", "")
    dashboard_url = config.get("github_pages_url", "#")

    if not all([smtp_user, smtp_password, recipient]):
        print("  ⚠ E-mail niet geconfigureerd (stel SMTP secrets in bij GitHub Actions)")
        return
    if recipient == "jouw@email.com":
        print("  ⚠ Vul een echt e-mailadres in config.json")
        return

    subject = "🎟 Prijsalert: tickets onder drempel!"
    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 16px'>{name}</td>"
        f"<td style='padding:8px 16px; font-weight:bold; color:#ff6b35'>€{price_2:.2f}</td>"
        f"<td style='padding:8px 16px; color:#888'>€{price_2/2:.2f} p.p. · drempel €{thr:.2f}</td>"
        f"</tr>"
        for name, price_2, thr in alerts
    )

    html_body = f"""
    <html><body style="font-family:sans-serif;background:#0f0f1a;color:#e0e0e0;padding:2rem">
      <div style="max-width:600px;margin:auto;background:#1a1a2e;border-radius:12px;
                  padding:2rem;border:1px solid #2a2a45">
        <h2 style="color:#ff6b35;margin-top:0">🎟 Prijsalert!</h2>
        <p>De volgende tickets zijn onder jouw drempel gedaald:</p>
        <table style="width:100%;border-collapse:collapse;margin:1rem 0">
          <thead>
            <tr style="background:#0f0f1a;color:#888">
              <th style="padding:8px 16px;text-align:left">Evenement</th>
              <th style="padding:8px 16px;text-align:left">2 tickets</th>
              <th style="padding:8px 16px;text-align:left">Per ticket</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <a href="https://{dashboard_url}"
           style="display:inline-block;background:#ff6b35;color:white;padding:0.75rem 1.5rem;
                  border-radius:8px;text-decoration:none;font-weight:bold">
          Bekijk dashboard →
        </a>
      </div>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipient, msg.as_string())
        print(f"  ✓ Alert verstuurd naar {recipient}")
    except Exception as exc:
        print(f"  ✗ E-mail versturen mislukt: {exc}")


async def main():
    base_dir = Path(__file__).parent.parent

    # Laad configuratie
    with open(base_dir / "config.json") as f:
        config = json.load(f)

    events = config.get("events", {})

    # Laad bestaande prijsdata
    prices_path = base_dir / "data" / "prices.json"
    prices_path.parent.mkdir(exist_ok=True)
    if prices_path.exists() and prices_path.stat().st_size > 2:
        with open(prices_path) as f:
            price_data: list[dict] = json.load(f)
    else:
        price_data = []

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_entry: dict = {"timestamp": now_iso}
    alerts_to_send: list[tuple[str, float, float]] = []  # (name, price_2tickets, threshold_per_ticket)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="nl-NL",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        for key, event_info in events.items():
            name = event_info.get("name", key)
            url = event_info.get("url", "")
            print(f"\n[{name}]")

            if not url or "VIND_URL" in url:
                print("  ⚠ Geen URL ingesteld – sla de juiste TicketSwap-URL op in config.json")
                new_entry[f"{key}_price"] = None
                new_entry[f"{key}_alerted"] = False
                continue

            price = await get_lowest_price_for_two_tickets(page, url)
            new_entry[f"{key}_price"] = price

            # Drempel is per ticket; price is voor 2 tickets
            threshold_per_ticket = float(event_info.get("threshold", 0))
            price_per_ticket = price / 2 if price is not None else None

            alert_flag = False
            if price_per_ticket is not None and threshold_per_ticket > 0 and price_per_ticket < threshold_per_ticket:
                if _should_send_alert(price_data, key):
                    alerts_to_send.append((name, price, threshold_per_ticket))
                    alert_flag = True
                    print(f"  🔔 Alert: €{price_per_ticket:.2f}/ticket < drempel €{threshold_per_ticket:.2f}/ticket")
                else:
                    print(f"  ℹ Al gewaarschuwd (cooldown actief)")
            new_entry[f"{key}_alerted"] = alert_flag

        await browser.close()

    # Voeg nieuwe meting toe en beperk tot laatste 1440 metingen (~30 dagen bij 30min interval)
    price_data.append(new_entry)
    if len(price_data) > 1440:
        price_data = price_data[-1440:]

    with open(prices_path, "w") as f:
        json.dump(price_data, f, indent=2)
    print(f"\n✓ {len(price_data)} metingen opgeslagen in data/prices.json")

    # Stuur alerts
    if alerts_to_send:
        print("\n📧 Alert e-mail versturen...")
        send_alert_email(config, alerts_to_send)


if __name__ == "__main__":
    asyncio.run(main())
