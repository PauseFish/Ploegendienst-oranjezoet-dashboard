"""
TicketSwap doorverkoop prijsscraper – Ploegendienst & Oranje Zoet 2026

Strategie:
1. Onderschep GraphQL API-responses terwijl de pagina laadt
2. DOM-scraping als fallback
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_prices_from_obj(obj, prices: list, depth: int = 0):
    """Doorzoek een object recursief op prijsvelden (skip officiële secties)."""
    if depth > 15 or obj is None:
        return
    official_re = re.compile(r"official|face.?value|originele", re.I)
    if isinstance(obj, list):
        for item in obj:
            _extract_prices_from_obj(item, prices, depth + 1)
    elif isinstance(obj, dict):
        type_val = str(obj.get("type") or obj.get("__typename") or "")
        if official_re.search(type_val):
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
            _extract_prices_from_obj(v, prices, depth + 1)


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def get_lowest_resale_price(page, url: str, debug_dir: Path | None = None) -> float | None:
    print(f"  → Navigeren naar: {url}")

    api_prices: list[float] = []
    api_log: list[dict] = []

    async def on_response(response):
        try:
            if "ticketswap" not in response.url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await response.json()
            entry = {"url": response.url, "status": response.status, "body": body}
            api_log.append(entry)
            _extract_prices_from_obj(body, api_prices)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)

        # Scroll stap voor stap naar beneden om lazy-loading te triggeren
        for scroll_y in [400, 800, 1200]:
            await page.evaluate(f"window.scrollTo(0, {scroll_y})")
            await page.wait_for_timeout(1500)

        # Wacht max 12s tot een prijs zichtbaar is in de DOM
        try:
            await page.wait_for_function(
                r"""() => /\u20ac\s*\d{{1,3}}[,.]\d{{2}}/.test(document.body.innerText || '')""",
                timeout=12000,
            )
            print("  ℹ Prijs zichtbaar in DOM")
        except Exception:
            print("  ⚠ Geen prijs in DOM na wachttijd")

        await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  ✗ Laden mislukt: {e}")
        page.remove_listener("response", on_response)
        return None
    finally:
        page.remove_listener("response", on_response)

    # Debug: screenshot + HTML + API-log
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", url.rstrip("/").split("/")[-2])
        await page.screenshot(path=str(debug_dir / f"{slug}.png"), full_page=True)
        (debug_dir / f"{slug}.html").write_text(await page.content(), encoding="utf-8")
        api_log_path = debug_dir / f"{slug}_api.json"
        api_log_path.write_text(
            json.dumps(api_log, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  ℹ Debug: {len(api_log)} API-responses, {len(api_prices)} prijzen onderschept")
        print(f"  ℹ API-log: {api_log_path}")

    # Strategie 1: onderschepte API-prijzen
    if api_prices:
        uniq = sorted(set(api_prices))
        print(f"  ℹ API-prijzen: {uniq}")
        result = round(min(api_prices), 2)
        print(f"  ✓ Laagste (API): €{result:.2f}")
        return result

    # Strategie 2: DOM-scraping
    dom_prices: list[float] = await page.evaluate(r"""() => {
        const officialRe = /officieel|official|face.?value|officiële.?ticketshop/i;

        function parsePrices(text) {
            const re = /\u20ac\s*(\d{1,3})[,.](\d{2})/g;
            const out = [];
            let m;
            while ((m = re.exec(text)) !== null) {
                const p = parseFloat(m[1] + '.' + m[2]);
                if (p > 5 && p < 500) out.push(p);
            }
            return out;
        }

        // Sectie na 'Doorverkooptickets' kop
        const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')];
        for (const h of headings) {
            if (/doorverkoop/i.test(h.innerText || '')) {
                // Loop door volgende siblings
                let el = h.nextElementSibling;
                const sectionPrices = [];
                while (el && !el.matches('h1,h2,h3,h4,h5,h6')) {
                    if (!officialRe.test(el.innerText || '')) {
                        sectionPrices.push(...parsePrices(el.innerText || ''));
                    }
                    el = el.nextElementSibling;
                }
                if (sectionPrices.length) {
                    console.log('Doorverkoop sectie prijzen:', sectionPrices);
                    return sectionPrices;
                }
            }
        }

        // Groepeer li/article per ouder, pak grootste groep
        const candidates = [...document.querySelectorAll('li, article')].filter(el => {
            const text = el.innerText || '';
            if (!/\u20ac\s*\d/.test(text)) return false;
            let node = el;
            for (let i = 0; i < 5; i++) {
                if (!node) break;
                if (officialRe.test((node.className || '') + ' ' + (node.getAttribute?.('aria-label') || ''))) return false;
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

        let best = [];
        for (const items of groups.values()) {
            if (items.length > best.length) best = items;
        }

        if (best.length) {
            const pp = [];
            for (const el of best) pp.push(...parsePrices(el.innerText || ''));
            if (pp.length) {
                console.log('DOM groep prijzen:', pp);
                return pp;
            }
        }

        // Volledig fallback: alle prijzen, sla officiële sectie over
        const full = document.body.innerText || '';
        const splitIdx = full.indexOf('Officiële ticketshop');
        const afterIdx = splitIdx >= 0 ? full.indexOf('Doorverkoop', splitIdx) : -1;
        const searchText = afterIdx >= 0 ? full.substring(afterIdx) : full;
        const fallback = parsePrices(searchText);
        console.log('Fallback prijzen:', fallback);
        return fallback;
    }""")

    if dom_prices:
        uniq = sorted(set(round(p, 2) for p in dom_prices))
        print(f"  ℹ DOM-prijzen: {uniq}")
        result = round(min(dom_prices), 2)
        print(f"  ✓ Laagste (DOM): €{result:.2f}")
        return result

    print("  ✗ Geen doorverkoop-prijzen gevonden")
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
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
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
            extra_http_headers={
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        page = await context.new_page()
        await stealth_async(page)

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
