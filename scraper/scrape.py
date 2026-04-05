"""
TicketSwap doorverkoop prijsscraper – Ploegendienst & Oranje Zoet 2026
Draait via GitHub Actions elke 30 minuten.

Strategie:
1. Onderschep TicketSwap API/GraphQL-responses → meest betrouwbaar
2. HTML fallback: zoek alleen in listing-kaarten, sla officiële prijs over
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


# ─── Prijs scrapen ────────────────────────────────────────────────────────────

async def get_lowest_resale_price(page, url: str) -> float | None:
    """
    Haalt de laagste doorverkoop-prijs per ticket op van een TicketSwap-pagina.
    Gebruikt eerst API-interceptie, dan HTML-fallback gericht op listing-kaarten.
    """
    api_prices: list[float] = []

    async def on_response(response):
        """Onderschep JSON-responses van TicketSwap en zoek naar prijzen."""
        try:
            if response.status != 200:
                return
            url_r = response.url
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            if "ticketswap" not in url_r:
                return
            body = await response.json()
            _extract_prices_from_json(body, api_prices)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        print(f"  → Navigeren naar: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Geef tijd voor API-calls en dynamische rendering
        await page.wait_for_timeout(5000)
    finally:
        page.remove_listener("response", on_response)

    # ── Poging 1: API-interceptie ──────────────────────────────────────────
    if api_prices:
        # TicketSwap stuurt prijzen soms in centen (bv. 4500 = €45,00)
        as_euros_cents = [p / 100 for p in api_prices if 500 <= p <= 50_000]
        as_euros_direct = [p for p in api_prices if 5 <= p <= 500]

        candidates = as_euros_cents if as_euros_cents else as_euros_direct
        if candidates:
            result = round(min(candidates), 2)
            print(f"  ✓ Laagste doorverkoop (API): €{result:.2f} per ticket")
            return result

    # ── Poging 2: HTML gericht op listing-kaarten ──────────────────────────
    print("  ⚠ Geen bruikbare API-data, HTML-fallback gebruiken...")
    return await _html_listing_prices(page)


def _extract_prices_from_json(obj, results: list, depth: int = 0) -> None:
    """Doorzoekt JSON recursief op bekende prijs-sleutels van TicketSwap."""
    if depth > 10:
        return
    price_keys = {
        "amount", "price", "totalprice", "sellerprice",
        "totalpricetransactionfee", "priceperticket",
        "value", "cents", "total", "originalamount",
    }
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in price_keys and isinstance(v, (int, float)) and v > 0:
                results.append(float(v))
            else:
                _extract_prices_from_json(v, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _extract_prices_from_json(item, results, depth + 1)


async def _html_listing_prices(page) -> float | None:
    """
    HTML-fallback: zoek prijzen in listing-kaarten en sla de
    'Originele prijs' (officiële face value) over.
    """
    try:
        prices: list[float] = await page.evaluate("""() => {
            const prices = [];
            const pricePattern = /\u20ac\\s*(\\d{1,3})(?:[,.](\\d{2}))/g;

            // Stap 1: zoek listing-containers op basis van bekende TicketSwap-patronen
            const containerSelectors = [
                '[data-testid*="listing"]',
                '[class*="ListingCard"]',
                '[class*="listing-card"]',
                '[class*="TicketListing"]',
                '[class*="ticket-listing"]',
                'ul[class*="list"] > li',
                'ol[class*="list"] > li',
            ];

            let cards = [];
            for (const sel of containerSelectors) {
                cards = [...document.querySelectorAll(sel)];
                if (cards.length > 0) break;
            }

            if (cards.length > 0) {
                for (const card of cards) {
                    const text = card.innerText || '';
                    // Sla over als dit de officiële-prijs-sectie is
                    if (/originele\\s*prijs|face\\s*value|official\\s*price/i.test(text)
                        && cards.length === 1) continue;

                    let m;
                    pricePattern.lastIndex = 0;
                    while ((m = pricePattern.exec(text)) !== null) {
                        const price = parseFloat(m[1] + '.' + (m[2] || '00'));
                        if (price > 5 && price < 500) prices.push(price);
                    }
                }
                if (prices.length > 0) return prices;
            }

            // Stap 2: scan hele pagina maar skip 'originele prijs'-context
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT
            );
            while (walker.nextNode()) {
                const node = walker.currentNode;
                const text = node.textContent.trim();
                if (!/^\u20ac/.test(text) && !text.startsWith('€')) continue;

                // Controleer of ouder-elementen 'originele prijs' bevatten
                let el = node.parentElement;
                let skip = false;
                for (let i = 0; i < 4; i++) {
                    if (!el) break;
                    const cls = (el.className || '').toLowerCase();
                    const label = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (cls.includes('original') || cls.includes('facevalue')
                        || label.includes('original') || label.includes('face')) {
                        skip = true; break;
                    }
                    el = el.parentElement;
                }
                if (skip) continue;

                pricePattern.lastIndex = 0;
                let m2;
                while ((m2 = pricePattern.exec(text)) !== null) {
                    const price = parseFloat(m2[1] + '.' + (m2[2] || '00'));
                    if (price > 5 && price < 500) prices.push(price);
                }
            }
            return prices;
        }""")

        if prices:
            result = round(min(prices), 2)
            print(f"  ✓ Laagste doorverkoop (HTML): €{result:.2f} per ticket")
            return result

        print("  ✗ Geen doorverkoop-prijzen gevonden")
        return None

    except Exception as exc:
        print(f"  ✗ HTML-fallback fout: {exc}")
        return None


# ─── Alert cooldown ───────────────────────────────────────────────────────────

def _should_send_alert(entries: list[dict], event_key: str) -> bool:
    """Stuur alleen een alert als de vorige alert meer dan 24 uur geleden was."""
    alert_key = f"{event_key}_alerted"
    for entry in reversed(entries):
        if entry.get(alert_key):
            last_ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last_ts < timedelta(hours=24):
                return False
            break
    return True


# ─── E-mail alert ─────────────────────────────────────────────────────────────

def send_alert_email(config: dict, alerts: list[tuple[str, float, float]]) -> None:
    """alerts = [(naam, prijs_per_ticket, drempel_per_ticket)]"""
    smtp_server  = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port    = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user    = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    recipient    = config.get("email", "")
    dashboard_url = config.get("github_pages_url", "#")

    if not all([smtp_user, smtp_password, recipient]):
        print("  ⚠ E-mail niet geconfigureerd (stel SMTP secrets in bij GitHub Actions)")
        return
    if recipient == "jouw@email.com":
        print("  ⚠ Vul een echt e-mailadres in config.json")
        return

    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 16px'>{name}</td>"
        f"<td style='padding:8px 16px;font-weight:bold;color:#ff6b35'>€{price:.2f} p.p.</td>"
        f"<td style='padding:8px 16px;color:#888'>2× = €{price*2:.2f} · drempel €{thr:.2f}</td>"
        f"</tr>"
        for name, price, thr in alerts
    )

    html_body = f"""<html><body style="font-family:sans-serif;background:#0f0f1a;
        color:#e0e0e0;padding:2rem">
      <div style="max-width:600px;margin:auto;background:#1a1a2e;border-radius:12px;
                  padding:2rem;border:1px solid #2a2a45">
        <h2 style="color:#ff6b35;margin-top:0">🎟 Prijsalert!</h2>
        <p>Doorverkoop-tickets zijn onder jouw drempel gedaald:</p>
        <table style="width:100%;border-collapse:collapse;margin:1rem 0">
          <thead><tr style="background:#0f0f1a;color:#888">
            <th style="padding:8px 16px;text-align:left">Evenement</th>
            <th style="padding:8px 16px;text-align:left">Per ticket</th>
            <th style="padding:8px 16px;text-align:left">Totaal</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <a href="https://{dashboard_url}"
           style="display:inline-block;background:#ff6b35;color:white;
                  padding:0.75rem 1.5rem;border-radius:8px;text-decoration:none;
                  font-weight:bold">Bekijk dashboard →</a>
      </div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🎟 Prijsalert: doorverkoop-tickets onder drempel!"
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


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    base_dir = Path(__file__).parent.parent

    with open(base_dir / "config.json") as f:
        config = json.load(f)

    events = config.get("events", {})

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
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        for key, event_info in events.items():
            name = event_info.get("name", key)
            url  = event_info.get("url", "")
            print(f"\n[{name}]")

            if not url or "VIND_URL" in url:
                print("  ⚠ Geen URL ingesteld")
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
                    print("  ℹ Al gewaarschuwd (cooldown actief)")
            new_entry[f"{key}_alerted"] = alert_flag

        await browser.close()

    # Bewaar laatste 1440 metingen (~30 dagen bij 30min interval)
    price_data.append(new_entry)
    if len(price_data) > 1440:
        price_data = price_data[-1440:]

    with open(prices_path, "w") as f:
        json.dump(price_data, f, indent=2)
    print(f"\n✓ {len(price_data)} metingen opgeslagen in data/prices.json")

    if alerts_to_send:
        print("\n📧 Alert e-mail versturen...")
        send_alert_email(config, alerts_to_send)


if __name__ == "__main__":
    asyncio.run(main())
