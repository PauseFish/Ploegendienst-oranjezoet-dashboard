"""
TicketSwap doorverkoop prijsscraper – Ploegendienst & Oranje Zoet 2026

Strategie (meerdere lagen):
1. Onderschep GraphQL API-responses van TicketSwap (betrouwbaarst)
2. Zoek prijzen in JSON-LD of window.__NEXT_DATA__
3. Val terug op DOM-scraping van de doorverkoop-feed
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

from playwright.async_api import async_playwright, Route


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def get_lowest_resale_price(page, url: str, debug_dir: Path | None = None) -> float | None:
    """
    Navigeert naar de TicketSwap-pagina en haalt de laagste doorverkoop-prijs
    per ticket op via API-onderschepping + DOM-fallback.
    """
    print(f"  → Navigeren naar: {url}")

    api_prices: list[float] = []

    async def handle_response(response):
        """Onderschep TicketSwap API-responses voor prijzen."""
        try:
            resp_url = response.url
            if "ticketswap" not in resp_url:
                return
            # GraphQL of listing endpoints
            if not any(x in resp_url for x in ["graphql", "listing", "ticket", "event"]):
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            body = await response.json()
            _extract_prices_from_json(body, api_prices)
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  ✗ Laden mislukt: {e}")
        return None
    finally:
        page.remove_listener("response", handle_response)

    # Debug: sla screenshot op als debug_dir opgegeven
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", url.split("/")[-2] if "/" in url else "page")
        screenshot_path = debug_dir / f"{slug}.png"
        html_path = debug_dir / f"{slug}.html"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        html_content = await page.content()
        html_path.write_text(html_content, encoding="utf-8")
        print(f"  ℹ Debug screenshot: {screenshot_path}")
        print(f"  ℹ Debug HTML: {html_path} ({len(html_content)} bytes)")

    # Strategie 1: API-onderschepping
    if api_prices:
        uniq = sorted(set(round(p, 2) for p in api_prices))
        print(f"  ℹ API-prijzen gevonden: {uniq}")
        result = round(min(api_prices), 2)
        print(f"  ✓ Laagste doorverkoop (API): €{result:.2f}")
        return result

    # Strategie 2: window.__NEXT_DATA__ of JSON-LD
    next_prices = await page.evaluate("""() => {
        const re = /(?:price|amount|cost)['"\\s]*[:=]['"\\s]*(\\d{1,3}[.,]\\d{2})/gi;
        const officialRe = /officieel|official|face.?value|originele.?prijs|face_value/i;

        function parseMoney(val) {
            if (typeof val === 'number' && val > 5 && val < 500) return val;
            if (typeof val === 'string') {
                const n = parseFloat(val.replace(',', '.'));
                if (n > 5 && n < 500) return n;
            }
            return null;
        }

        function walkJson(obj, prices, depth) {
            if (depth > 15 || !obj) return;
            if (Array.isArray(obj)) {
                for (const item of obj) walkJson(item, prices, depth + 1);
            } else if (typeof obj === 'object') {
                // Sla officiële prijs blokken over
                const keys = Object.keys(obj);
                if (keys.some(k => officialRe.test(k))) return;
                if (typeof obj.type === 'string' && officialRe.test(obj.type)) return;
                for (const [k, v] of Object.entries(obj)) {
                    if (/price|amount|total_price|buyer_price/i.test(k)) {
                        const p = parseMoney(v);
                        if (p !== null) prices.push(p);
                    }
                    walkJson(v, prices, depth + 1);
                }
            }
        }

        const prices = [];

        // Probeer __NEXT_DATA__
        try {
            const nd = window.__NEXT_DATA__;
            if (nd) walkJson(nd, prices, 0);
        } catch(e) {}

        // Probeer JSON-LD scripts
        for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
                walkJson(JSON.parse(s.textContent), prices, 0);
            } catch(e) {}
        }

        return [...new Set(prices.map(p => Math.round(p * 100) / 100))].sort((a,b)=>a-b);
    }""")

    if next_prices and len(next_prices) > 0:
        print(f"  ℹ __NEXT_DATA__/JSON-LD prijzen: {next_prices}")
        # Filter face value: neem de laagste prijs die minder dan €80 is
        cheap = [p for p in next_prices if p < 80]
        if cheap:
            result = round(min(cheap), 2)
            print(f"  ✓ Laagste doorverkoop (data): €{result:.2f}")
            return result

    # Strategie 3: DOM-scraping
    dom_prices = await page.evaluate("""() => {
        const priceRe = /\u20ac\\s*(\\d{1,3})[,.]( \\d{2})/g;
        const officialRe = /officieel|official|face.?value|originele.?prijs|gezichtswaarde|organisatie|normal.?price/i;

        function parsePrices(text) {
            const found = [];
            // Matcht € 45,00 of €45.00
            const re = /\u20ac\\s*(\\d{1,3})[,\\.](\\d{2})/g;
            let m;
            while ((m = re.exec(text)) !== null) {
                const p = parseFloat(m[1] + '.' + m[2]);
                if (p > 5 && p < 500) found.push(p);
            }
            return found;
        }

        function isOfficial(el) {
            let node = el;
            for (let i = 0; i < 6; i++) {
                if (!node) break;
                const t = (node.className || '') + ' ' + (node.getAttribute?.('aria-label') || '');
                if (officialRe.test(t)) return true;
                node = node.parentElement;
            }
            return false;
        }

        // Zoek alle elementen met prijzen, groepeer per ouder
        const all = [...document.querySelectorAll('*')].filter(el => {
            if (el.children.length > 0) return false; // leaf nodes
            const text = el.textContent || '';
            return /\u20ac\\s*\\d/.test(text) && text.length < 50;
        });

        const debug = [];
        const groups = new Map();
        for (const el of all) {
            const prices = parsePrices(el.textContent || '');
            if (prices.length === 0) continue;
            if (isOfficial(el)) continue;
            const parent = el.parentElement?.parentElement || el.parentElement || document.body;
            if (!groups.has(parent)) groups.set(parent, []);
            groups.get(parent).push(...prices);
            debug.push({ text: (el.textContent||'').trim(), prices, tag: el.tagName });
        }

        // Log debug info
        console.log('DOM debug entries:', JSON.stringify(debug.slice(0, 30)));

        // Flatten all non-official prices
        const allPrices = [];
        for (const prices of groups.values()) {
            allPrices.push(...prices);
        }

        return [...new Set(allPrices.map(p => Math.round(p*100)/100))].sort((a,b)=>a-b);
    }""")

    if dom_prices:
        print(f"  ℹ DOM-prijzen: {dom_prices}")
        result = round(min(dom_prices), 2)
        print(f"  ✓ Laagste doorverkoop (DOM): €{result:.2f}")
        return result

    # Strategie 4: alles – pak laagste prijs van volledige pagina
    all_prices = await page.evaluate("""() => {
        const text = document.body.innerText || '';
        const re = /\u20ac\\s*(\\d{1,3})[,\\.](\\d{2})/g;
        const prices = [];
        let m;
        while ((m = re.exec(text)) !== null) {
            const p = parseFloat(m[1] + '.' + m[2]);
            if (p > 5 && p < 500) prices.push(p);
        }
        console.log('Alle paginaprijzen:', [...new Set(prices)].sort((a,b)=>a-b));
        return [...new Set(prices.map(p => Math.round(p*100)/100))].sort((a,b)=>a-b);
    }""")

    if all_prices:
        print(f"  ℹ Alle paginaprijzen: {all_prices}")
        # Neem de laagste prijs als doorverkoop-indicatie
        result = round(min(all_prices), 2)
        print(f"  ✓ Laagste prijs (fallback): €{result:.2f}")
        return result

    print("  ✗ Geen prijzen gevonden op pagina")
    return None


def _extract_prices_from_json(obj, prices: list, depth: int = 0):
    """Doorzoek een JSON-object recursief op prijsvelden."""
    if depth > 15 or obj is None:
        return
    official_re = re.compile(
        r"officieel|official|face.?value|originele.?prijs|face_value", re.I
    )
    if isinstance(obj, list):
        for item in obj:
            _extract_prices_from_json(item, prices, depth + 1)
    elif isinstance(obj, dict):
        # Sla secties over die de officiële prijs beschrijven
        type_val = obj.get("type") or obj.get("__typename") or ""
        if official_re.search(str(type_val)):
            return
        for k, v in obj.items():
            if official_re.search(k):
                continue
            if re.search(r"price|amount|buyer_price|total_price", k, re.I):
                if isinstance(v, (int, float)) and 5 < v < 500:
                    prices.append(round(float(v), 2))
                elif isinstance(v, str):
                    try:
                        n = float(v.replace(",", "."))
                        if 5 < n < 500:
                            prices.append(round(n, 2))
                    except ValueError:
                        pass
            _extract_prices_from_json(v, prices, depth + 1)


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

    # Debug-map: alleen opslaan als DEBUG=1 omgevingsvariabele is ingesteld
    debug_dir: Path | None = None
    if os.environ.get("DEBUG") == "1":
        debug_dir = base_dir / "debug"

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

        # Log browser console messages voor debuggen
        page.on("console", lambda msg: print(f"  [browser] {msg.text[:200]}") if msg.type in ("log", "warn", "error") else None)

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
