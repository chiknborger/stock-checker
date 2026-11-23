#!/usr/bin/env python3
"""
Stock checker for skoringen.no (Mac friendly).

Checks if the wanted sizes are in stock and notifies you by:
  - email (Gmail with an app password)
  - push notification to your phone via ntfy.sh (free, works like an SMS)
  - a normal Mac notification

Usage:
  python3 stock_checker.py           # check once
  python3 stock_checker.py --loop    # keep checking every CHECK_EVERY_MIN minutes
  python3 stock_checker.py --debug   # print what the script finds + save the page to page.html
  python3 stock_checker.py --test    # send a test notification
"""

import json
import re
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------- SETTINGS ----------------

URL = "https://www.skoringen.no/tamaris-korte-damestovletter-svart-1-26244-41?productid=5263550810&variantid=47"
WANTED_SIZES = ["38", "37"]      # in order of preference
CHECK_EVERY_MIN = 30             # only used with --loop

# Email (leave EMAIL_ENABLED = False if you don't want it)
EMAIL_ENABLED = True
SENDER_EMAIL = "you@gmail.com"
APP_PASSWORD = "xxxx xxxx xxxx xxxx"   # Gmail app password, NOT your normal password
RECEIVER_EMAIL = "you@gmail.com"

# Push to phone via ntfy.sh. Install the "ntfy" app, subscribe to the same topic.
# Pick something random so nobody else uses it. Set to "" to turn off.
NTFY_TOPIC = ""                  # e.g. "skoringen-tamaris-k83jd2"

MAC_NOTIFICATION = True

# ------------------------------------------

# When running on GitHub, the passwords come from GitHub "secrets" instead of this file
import os
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", SENDER_EMAIL)
APP_PASSWORD = os.environ.get("APP_PASSWORD", APP_PASSWORD)
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", RECEIVER_EMAIL)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", NTFY_TOPIC)
if os.environ.get("GITHUB_ACTIONS"):
    MAC_NOTIFICATION = False
    EMAIL_ENABLED = bool(os.environ.get("APP_PASSWORD"))

STATE_FILE = Path(__file__).with_name(".stock_state.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
}

SIZE_KEYS = {"size", "sizename", "size_name", "storrelse", "størrelse", "label",
             "name", "title", "value", "displayname", "variantname"}
STOCK_KEYS = {"stock", "instock", "in_stock", "isinstock", "available", "isavailable",
              "availability", "quantity", "qty", "stockstatus", "stocklevel",
              "stockquantity", "buyable", "isbuyable", "soldout", "issoldout", "outofstock"}
SOLD_OUT_WORDS = ("utsolgt", "sold out", "soldout", "sold-out", "out-of-stock",
                  "outofstock", "unavailable", "disabled", "ikke på lager")


def fetch_page():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def value_means_in_stock(key, value):
    """Turn a stock-ish value into True/False/None."""
    key = key.lower()
    negative_key = key in {"soldout", "issoldout", "outofstock"}
    if isinstance(value, bool):
        return (not value) if negative_key else value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        v = value.lower()
        if any(w in v for w in ("outofstock", "out of stock", "soldout", "utsolgt", "false")):
            return False
        if any(w in v for w in ("instock", "in stock", "på lager", "true", "available")):
            return True
        if v.isdigit():
            return int(v) > 0
    return None


def size_matches(value, size):
    if not isinstance(value, (str, int, float)):
        return False
    text = str(value)
    return re.search(rf"(?<![\d.,]){re.escape(size)}(?![\d.,])", text) is not None


def walk_json(obj, found, path="json"):
    """Look for dicts that have both a size-like key and a stock-like key."""
    if isinstance(obj, dict):
        lower = {k.lower(): k for k in obj if isinstance(k, str)}
        size_vals = [obj[lower[k]] for k in SIZE_KEYS if k in lower]
        stock_items = [(k, obj[lower[k]]) for k in STOCK_KEYS if k in lower]
        if size_vals and stock_items:
            for size in WANTED_SIZES:
                if any(size_matches(v, size) for v in size_vals):
                    for k, v in stock_items:
                        result = value_means_in_stock(k, v)
                        if result is not None:
                            found.setdefault(size, []).append((result, f"{path}: {k}={v!r}"))
                            break
        for k, v in obj.items():
            walk_json(v, found, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_json(v, found, f"{path}[{i}]")


def json_from_scripts(soup):
    """Yield every JSON blob we can pull out of <script> tags."""
    for tag in soup.find_all("script"):
        text = tag.string or tag.get_text() or ""
        text = text.strip()
        if not text:
            continue
        # Plain JSON (ld+json, __NEXT_DATA__, application/json, etc.)
        try:
            yield json.loads(text)
            continue
        except ValueError:
            pass
        # JSON assigned to a variable: window.foo = {...};
        for m in re.finditer(r"=\s*(\{.*?\}|\[.*?\])\s*;?\s*(?:\n|$)", text, re.S):
            try:
                yield json.loads(m.group(1))
            except ValueError:
                pass


def check_html_buttons(soup, found):
    """Fallback: look at size buttons/options in the HTML itself."""
    for el in soup.find_all(["button", "option", "li", "a", "label", "span", "div", "input"]):
        own_text = el.get_text(" ", strip=True)
        attrs_text = " ".join(
            f"{k}={' '.join(v) if isinstance(v, list) else v}" for k, v in el.attrs.items()
        ).lower()
        for size in WANTED_SIZES:
            is_size = own_text == size or el.get("data-size") == size or el.get("value") == size
            if not is_size:
                continue
            sold_out = (
                el.has_attr("disabled")
                or el.get("aria-disabled") == "true"
                or any(w in attrs_text for w in SOLD_OUT_WORDS)
            )
            found.setdefault(size, []).append(
                (not sold_out, f"html <{el.name} {attrs_text[:120]}> '{own_text}'")
            )


def check_stock(html, debug=False):
    soup = BeautifulSoup(html, "html.parser")
    found = {}

    for blob in json_from_scripts(soup):
        walk_json(blob, found)

    # Only use HTML buttons if JSON gave nothing
    if not found:
        check_html_buttons(soup, found)

    if debug:
        print("What the script found:")
        if not found:
            print("  Nothing. Open page.html and search for '38' to see how sizes are shown.")
        for size, hits in found.items():
            for ok, where in hits[:5]:
                print(f"  size {size}: {'IN STOCK' if ok else 'sold out'}  <- {where}")

    # A size counts as in stock if any source says so
    return {size: any(ok for ok, _ in hits) for size, hits in found.items()}


def notify(title, body):
    if MAC_NOTIFICATION and sys.platform == "darwin":
        script = f'display notification {json.dumps(body)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)

    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Click": URL, "Priority": "high"},
                timeout=15,
            )
        except requests.RequestException as e:
            print("ntfy failed:", e)

    if EMAIL_ENABLED:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = SENDER_EMAIL
        msg["To"] = RECEIVER_EMAIL
        msg.set_content(f"{body}\n\n{URL}")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
                s.login(SENDER_EMAIL, APP_PASSWORD)
                s.send_message(msg)
        except Exception as e:
            print("Email failed:", e)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def run_once(debug=False):
    html = fetch_page()
    if debug:
        Path("page.html").write_text(html, encoding="utf-8")
        print("Saved page to page.html")

    stock = check_stock(html, debug)
    stamp = time.strftime("%Y-%m-%d %H:%M")

    if not stock:
        print(f"[{stamp}] Could not find stock info for {WANTED_SIZES}. Run with --debug.")
        return

    in_stock = [s for s in WANTED_SIZES if stock.get(s)]
    print(f"[{stamp}] " + ", ".join(f"{s}: {'yes' if stock.get(s) else 'no'}" for s in WANTED_SIZES))

    # Only notify when a size goes from sold out -> in stock (no spam every run)
    state = load_state()
    newly = [s for s in in_stock if not state.get(s)]
    if newly and not debug:
        notify("Tamaris stovletter på lager!", f"Størrelse {', '.join(newly)} er på lager nå.")
    STATE_FILE.write_text(json.dumps({s: s in in_stock for s in WANTED_SIZES}))


def main():
    args = sys.argv[1:]
    if "--test" in args:
        notify("Test fra stock checker", "Hvis du ser denne, funker varslene.")
        print("Test sent.")
    elif "--loop" in args:
        while True:
            try:
                run_once()
            except Exception as e:
                print("Error:", e)
            time.sleep(CHECK_EVERY_MIN * 60)
    else:
        run_once(debug="--debug" in args)


if __name__ == "__main__":
    main()
