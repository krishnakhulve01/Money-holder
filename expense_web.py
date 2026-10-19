"""
==========================================================================
  BACHELORS' FOOD EXPENSE MANAGER  -  WEB VERSION
==========================================================================
The account holder runs this program on his laptop. The other three
bachelors open a link on their phones and see the live report (same
Wi-Fi, or anywhere in the world through a tunnel - see the end of the
start-up text). Only the holder, with a PIN, can add or delete expenses.
Viewers can be protected with an access code.

Needs expense_manager.py in the SAME folder (it shares the same saved data).

To run:   python expense_web.py
"""

import calendar
import hmac
import html
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import traceback
from datetime import date
from decimal import Decimal
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import expense_manager as em

if os.environ.get("DATA_DIR"):          # optional: keep the data somewhere else
    em.DATA_DIR = os.environ["DATA_DIR"]

LOCK = threading.RLock()     # only one change to the data files at a time
TOKENS = {}                  # logged-in holder sessions: token -> expiry time
VIEWERS = {}                 # bachelors who typed the access code: token -> expiry
PIN = ""                     # holder PIN, chosen when the server starts
VIEWER_CODE = ""             # access code to view the report ("" = open to all)
FAILS = []                   # times of recent wrong PIN / code attempts
MAX_FAILS = 8                # this many wrong attempts in 10 minutes = lock-out
NEG_MONEY = re.compile(r"-?[0-9]+(\.[0-9]{1,2})?")
CONFIRM_DELETE = "return confirm('Delete this entry?')"

CSS = """
:root{--bg:#f4f6fa;--card:#fff;--ink:#1c2733;--mute:#6b7785;--line:#dbe1ea;--head:#1f3a5f;--brand:#1f3a5f;--bad:#b00020}
@media (prefers-color-scheme:dark){:root{--bg:#10151c;--card:#19212b;--ink:#e8edf3;--mute:#9aa7b5;--line:#2a3542;--head:#2b4a73;--brand:#8db8ff;--bad:#ff6b81}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:.2rem 0;color:var(--brand)}
h2{font-size:1.05rem;margin:1.5rem 0 .5rem;color:var(--brand)}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card small{color:var(--mute);display:block}.card b{font-size:1.25rem}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:var(--head);color:#fff;font-weight:600;white-space:normal;vertical-align:bottom}
td.p{white-space:normal;min-width:80px;overflow-wrap:anywhere}
th.r,td.r{text-align:right}
tr.tot td{font-weight:600;background:rgba(127,127,127,.1)}
.neg{color:var(--bad);font-weight:600}
form.grid{display:grid;gap:10px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
label{font-size:.9rem;color:var(--mute);display:block;margin-bottom:3px}
input,select{font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);width:100%}
select{width:auto}
input[type=checkbox]{width:auto;margin-right:6px}
.btn{display:inline-block;padding:10px 14px;border-radius:8px;background:var(--head);color:#fff;text-decoration:none;border:0;font:inherit;cursor:pointer}
.btn.sec{background:transparent;color:var(--ink);border:1px solid var(--line)}
.btn.del{background:var(--bad);padding:4px 10px;font-size:.85rem}
.msg{padding:10px 12px;border-radius:8px;margin:10px 0}
.ok{background:#e3f1e6;color:#14532d}.err{background:#fde7ea;color:#7a0016}
@media (prefers-color-scheme:dark){.ok{background:#12301d;color:#b7ebc6}.err{background:#3a1119;color:#ffc2cc}}
@media (max-width:480px){th,td{padding:7px 6px;font-size:.9rem}.wrap{padding:12px}}
@media (max-width:380px){th,td{padding:7px 4px;font-size:.85rem}}
footer{color:var(--mute);font-size:.85rem;margin:24px 0}
a{color:var(--brand)}
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def h(text):
    return html.escape(str(text), quote=True)


def amt(value):
    css = ' class="neg"' if Decimal(value) < 0 else ""
    return f"<span{css}>{em.rs(value)}</span>"


def num(value):
    css = ' class="neg"' if Decimal(value) < 0 else ""
    return f"<span{css}>{Decimal(value):,.2f}</span>"


def ym_str(ym):
    return f"{ym[0]}-{ym[1]:02d}"


def parse_month(text):
    found = re.fullmatch(r"(\d{4})-(\d{2})", text or "")
    if found and 1 <= int(found[2]) <= 12 and 2000 <= int(found[1]) <= 2100:
        return int(found[1]), int(found[2])
    today = date.today()
    return today.year, today.month


def saved_months():
    months = set()
    if os.path.isdir(em.DATA_DIR):
        for name in os.listdir(em.DATA_DIR):
            found = re.fullmatch(r"(\d{4})-(\d{2})\.json", name)
            if found:
                months.add((int(found[1]), int(found[2])))
    return months


def load(ym):
    with LOCK:
        return em.load_data(ym[0], ym[1])


def valid_name(raw, taken):
    name = " ".join(raw.split())
    if not name:
        return None, "Names cannot be empty."
    if len(name) > 20:
        return None, "Names can have at most 20 characters."
    if not name[0].isalpha() or not all(c.isalpha() or c in " .'-" for c in name):
        return None, "Names can only have letters (spaces and . ' - are allowed)."
    if name.lower() in {t.lower() for t in taken}:
        return None, "Each of the four names must be different."
    return name, None


def parse_money(raw, label, positive=False):
    text = raw.strip().replace(",", "")
    if not em.MONEY_PATTERN.fullmatch(text):
        return None, f"{label}: please type a valid amount like 250 or 99.50."
    value = Decimal(text).quantize(em.CENT)
    if positive and value <= 0:
        return None, f"{label} must be more than 0."
    if value > em.MAX_AMOUNT:
        return None, f"{label} is too large. Please check it."
    return value, None


# --------------------------------------------------------------------------
# Changing the data (each returns ("msg" or "err", text))
# --------------------------------------------------------------------------
def do_setup(ym, f):
    with LOCK:
        if em.load_data(ym[0], ym[1]) is not None:
            return "err", "This month is already set up."
        names = []
        for i in range(4):
            name, error = valid_name(f.get(f"name{i}", ""), names)
            if error:
                return "err", error
            names.append(name)
        received = {}
        for i, name in enumerate(names):
            value, error = parse_money(f.get(f"paid{i}", ""), f"Money received from {name}")
            if error:
                return "err", error
            received[name] = str(value)
        prev_text = f.get("prev", "").strip().replace(",", "")
        if prev_text == "":
            previous = em.ZERO
        elif NEG_MONEY.fullmatch(prev_text) and abs(Decimal(prev_text)) <= em.MAX_AMOUNT:
            previous = Decimal(prev_text).quantize(em.CENT)
        else:
            return "err", "Money left from last month must be a valid amount."
        d = {"year": ym[0], "month": ym[1], "holder": names[0], "members": names,
             "received": received, "previous_balance": str(previous),
             "expenses": [], "extra_received": [], "next_id": 1}
        em.save_data(d)
        return "msg", f"{em.month_label(ym[0], ym[1])} is ready. Total money available: {em.rs(em.total_fund(d))}"


def do_add(ym, f):
    with LOCK:
        d = em.load_data(ym[0], ym[1])
        if d is None:
            return "err", "This month is not set up yet."
        try:
            day = date.fromisoformat(f.get("date", ""))
        except ValueError:
            return "err", "Please choose a valid date."
        if (day.year, day.month) != ym:
            return "err", f"The date must be inside {em.month_label(ym[0], ym[1])}."
        product = " ".join(f.get("product", "").split())
        if not product:
            return "err", "Please type the product name."
        if len(product) > 60:
            return "err", "The product name is too long (maximum 60 characters)."
        cost, error = parse_money(f.get("cost", ""), "Cost", positive=True)
        if error:
            return "err", error
        left = em.balance(d)
        if cost > left and not f.get("allow"):
            return "err", (f"Only {em.rs(left)} is left but this costs {em.rs(cost)}. "
                           "Tick 'Allow even if money is not enough' to add it anyway.")
        d["expenses"].append({"id": d["next_id"], "date": day.isoformat(),
                              "product": product, "cost": str(cost)})
        d["next_id"] += 1
        em.save_data(d)
        return "msg", f"Added {product} ({em.rs(cost)}). Money left now: {em.rs(em.balance(d))}"


def do_delete(ym, f):
    with LOCK:
        d = em.load_data(ym[0], ym[1])
        if d is None:
            return "err", "This month is not set up yet."
        try:
            entry_id = int(f.get("id", ""))
        except ValueError:
            return "err", "That entry was not found."
        kept = [e for e in d["expenses"] if e["id"] != entry_id]
        if len(kept) == len(d["expenses"]):
            return "err", "That entry was not found (maybe it was already deleted)."
        d["expenses"] = kept
        em.save_data(d)
        return "msg", f"Entry deleted. Money left now: {em.rs(em.balance(d))}"


def do_extra(ym, f):
    with LOCK:
        d = em.load_data(ym[0], ym[1])
        if d is None:
            return "err", "This month is not set up yet."
        who = f.get("who", "")
        if who not in d["members"]:
            return "err", "Please choose one of the four bachelors."
        try:
            day = date.fromisoformat(f.get("date", ""))
        except ValueError:
            return "err", "Please choose a valid date."
        if (day.year, day.month) != ym:
            return "err", f"The date must be inside {em.month_label(ym[0], ym[1])}."
        value, error = parse_money(f.get("amount", ""), "Amount", positive=True)
        if error:
            return "err", error
        d.setdefault("extra_received", []).append(
            {"id": d["next_id"], "date": day.isoformat(), "name": who, "amount": str(value)})
        d["next_id"] += 1
        em.save_data(d)
        return "msg", f"Added {em.rs(value)} received from {who}. Total money available: {em.rs(em.total_fund(d))}"


def do_delete_extra(ym, f):
    with LOCK:
        d = em.load_data(ym[0], ym[1])
        if d is None:
            return "err", "This month is not set up yet."
        try:
            extra_id = int(f.get("id", ""))
        except ValueError:
            return "err", "That amount was not found."
        old = d.get("extra_received", [])
        kept = [x for x in old if x["id"] != extra_id]
        if len(kept) == len(old):
            return "err", "That amount was not found (maybe it was already deleted)."
        d["extra_received"] = kept
        em.save_data(d)
        return "msg", f"Amount deleted. Money left now: {em.rs(em.balance(d))}"


def make_pdf_bytes(d):
    handle, path = tempfile.mkstemp(suffix=".pdf")
    os.close(handle)
    try:
        em.build_pdf(d, path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


# --------------------------------------------------------------------------
# Page building
# --------------------------------------------------------------------------
def page(title, body, refresh=False):
    script = "<script>setInterval(function(){if(!document.hidden)location.reload()},60000)</script>" if refresh else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{h(title)}</title><style>{CSS}</style></head>"
            f'<body><div class="wrap">{body}</div>{script}</body></html>')


def flash(q):
    out = ""
    if q.get("err"):
        out += '<div class="msg err">' + h(q["err"]) + "</div>"
    if q.get("msg"):
        out += '<div class="msg ok">' + h(q["msg"]) + "</div>"
    return out


def table(headers, rows, right=(), wrap=(), foot=()):
    def cell(tag, i, text):
        css = " ".join(c for c, on in (("r", i in right), ("p", i in wrap)) if on)
        return f'<{tag} class="{css}">{text}</{tag}>'

    head = "".join(cell("th", i, t) for i, t in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", i, t) for i, t in enumerate(r)) + "</tr>"
                   for r in rows)
    tail = "".join('<tr class="tot">' + "".join(cell("td", i, t) for i, t in enumerate(r)) + "</tr>"
                   for r in foot)
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody><tfoot>{tail}</tfoot></table></div>")


def month_picker(ym):
    options = ""
    for y, m in sorted(saved_months() | {ym}):
        chosen = " selected" if (y, m) == ym else ""
        options += f'<option value="{y}-{m:02d}"{chosen}>{h(em.month_label(y, m))}</option>'
    return f"<select onchange=\"location.href='?m='+this.value\">{options}</select>"


def report_page(ym):
    d = load(ym)
    top = ('<div class="top"><h1>Food Expense Report</h1>'
           f"{month_picker(ym)}</div>")
    foot = ('<footer>This page refreshes by itself every minute while it is open. '
            '<a href="/manage">Holder login</a></footer>')
    if d is None:
        label = h(em.month_label(ym[0], ym[1]))
        return page("Food Expense Report",
                    top + f'<div class="msg err">No account has been set up for {label} yet.</div>' + foot,
                    refresh=True)

    holder = h(d["holder"])
    cards = ('<div class="cards">'
             f'<div class="card"><small>Total money available</small><b>{amt(em.total_fund(d))}</b></div>'
             f'<div class="card"><small>Total spent</small><b>{amt(em.total_spent(d))}</b></div>'
             f'<div class="card"><small>Money left</small><b>{amt(em.balance(d))}</b></div></div>')

    money_rows = []
    for name in d["members"]:
        tag = " (holder)" if name == d["holder"] else ""
        money_rows.append([h(name + tag), num(d["received"][name])])
    for x in em.extras(d):
        who = x["name"]
        when = em.fmt_date(x["date"])
        money_rows.append([h(f"Additional amount from {who} on {when}"), num(x["amount"])])
    money = table(["Money received from", "Amount (Rs.)"], money_rows, right=(1,), wrap=(0,), foot=[
        ["Total received this month", num(em.total_received(d))],
        ["Left from previous month", num(d["previous_balance"])],
        ["Total available", num(em.total_fund(d))],
    ])

    day_rows = []
    for iso, day in em.daywise_rows(d):
        day_rows.append([em.fmt_date(iso), str(day["items"]), num(day["spent"]), num(day["left"])])
    days = table(["Date", "Items", "Spent (Rs.)", "Left (Rs.)"], day_rows, right=(1, 2, 3)) if day_rows else ""

    ledger = []
    for e, left in em.ledger_rows(d):
        ledger.append([em.fmt_date(e["date"]), h(e["product"]), num(e["cost"]), num(left)])
    if ledger:
        expenses = table(["Date", "Product bought", "Cost (Rs.)", "Left (Rs.)"], ledger,
                         right=(2, 3), wrap=(1,),
                         foot=[["", "Total spent", num(em.total_spent(d)), ""]])
    else:
        expenses = '<p class="msg">No expenses have been entered yet.</p>'

    pdf = f'<p><a class="btn" href="/pdf?m={ym_str(ym)}">Download PDF report</a></p>'
    body = (top + f"<p>Account holder: <b>{holder}</b></p>" + cards
            + "<h2>Money received</h2>" + money
            + ("<h2>Day-wise totals</h2>" + days if days else "")
            + "<h2>All expenses</h2>" + expenses + pdf + foot)
    return page("Food Expense Report", body, refresh=True)


def login_page(q):
    body = ("<h1>Holder login</h1><p>Only the account holder can add or delete expenses.</p>"
            + flash(q)
            + '<form method="post" action="/login" class="grid"><div><label>Holder PIN</label>'
              '<input type="password" name="pin" autocomplete="off" autofocus required></div>'
              '<button class="btn">Log in</button></form>'
              '<p><a href="/">&larr; Back to the report</a></p>')
    return page("Holder login", body)


def enter_page(q):
    body = ("<h1>Food Expense Report</h1>"
            "<p>Please enter the access code you got from the account holder.</p>"
            + flash(q)
            + '<form method="post" action="/enter" class="grid"><div><label>Access code</label>'
              '<input type="password" name="code" autocomplete="off" autofocus required></div>'
              '<button class="btn">View report</button></form>'
              '<p><a href="/login">Holder login</a></p>')
    return page("Access code", body)


def setup_form(ym):
    py, pm = (ym[0] - 1, 12) if ym[1] == 1 else (ym[0], ym[1] - 1)
    previous = load((py, pm))
    guess = em.balance(previous) if previous is not None else None
    prev_value = f"{guess:.2f}" if guess is not None else ""
    note = (f"<small>{h(em.month_label(py, pm))} ended with {em.rs(guess)}.</small>"
            if guess is not None else "")
    fields = ""
    for i in range(4):
        who = " (account holder)" if i == 0 else ""
        fields += (f'<div class="two"><div><label>Bachelor {i + 1} name{who}</label>'
                   f'<input name="name{i}" maxlength="20" required></div>'
                   '<div><label>Money received</label>'
                   f'<input name="paid{i}" inputmode="decimal" placeholder="0" required></div></div>')
    return (f"<h2>Set up {h(em.month_label(ym[0], ym[1]))}</h2>"
            f'<form method="post" action="/setup" class="grid"><input type="hidden" name="m" value="{ym_str(ym)}">'
            f'{fields}<div><label>Money left from previous month</label>'
            f'<input name="prev" inputmode="decimal" value="{prev_value}" placeholder="0">{note}</div>'
            '<button class="btn">Create month</button></form>')


def add_form(ym):
    first = date(ym[0], ym[1], 1)
    last = date(ym[0], ym[1], calendar.monthrange(ym[0], ym[1])[1])
    today = date.today()
    start = today if first <= today <= last else first
    return ('<h2>Add an expense</h2><form method="post" action="/add" class="grid">'
            f'<input type="hidden" name="m" value="{ym_str(ym)}">'
            '<div class="two"><div><label>Date</label>'
            f'<input type="date" name="date" value="{start}" min="{first}" max="{last}" required></div>'
            '<div><label>Cost (Rs.)</label><input name="cost" inputmode="decimal" placeholder="0.00" required></div></div>'
            '<div><label>Product bought</label><input name="product" maxlength="60" required></div>'
            '<label><input type="checkbox" name="allow" value="1">Allow even if money is not enough</label>'
            '<button class="btn">Add expense</button></form>')


def extra_form(d, ym):
    first = date(ym[0], ym[1], 1)
    last = date(ym[0], ym[1], calendar.monthrange(ym[0], ym[1])[1])
    today = date.today()
    start = today if first <= today <= last else first
    options = "".join(f"<option>{h(name)}</option>" for name in d["members"])
    return ('<h2>Add additional money received</h2><form method="post" action="/extra" class="grid">'
            f'<input type="hidden" name="m" value="{ym_str(ym)}">'
            f'<div><label>Received from</label><select name="who" style="width:100%">{options}</select></div>'
            '<div class="two"><div><label>Date</label>'
            f'<input type="date" name="date" value="{start}" min="{first}" max="{last}" required></div>'
            '<div><label>Amount (Rs.)</label><input name="amount" inputmode="decimal" placeholder="0.00" required></div></div>'
            '<button class="btn">Add amount</button></form>')


def extra_list(d, ym):
    items = em.extras(d)
    if not items:
        return ""
    rows = []
    for n, x in enumerate(items, 1):
        delete = (f'<form method="post" action="/delete_extra" onsubmit="{CONFIRM_DELETE}">'
                  f'<input type="hidden" name="m" value="{ym_str(ym)}">'
                  f'<input type="hidden" name="id" value="{x["id"]}">'
                  '<button class="btn del">Delete</button></form>')
        rows.append([em.fmt_date(x["date"]), h(x["name"]), num(x["amount"]), delete])
    return ("<h2>Additional amounts received</h2>"
            + table(["Date", "From", "Amount (Rs.)", ""], rows, right=(2, 3), wrap=(1,)))


def manage_page(ym, q):
    d = load(ym)
    ymstr = ym_str(ym)
    top = ('<div class="top"><h1>Holder panel</h1>' + month_picker(ym) + "</div>"
           f'<p><a class="btn sec" href="/?m={ymstr}">View report</a> '
           '<a class="btn sec" href="/logout">Log out</a></p>' + flash(q))
    if d is None:
        return page("Holder panel", top + setup_form(ym))

    cards = ('<div class="cards">'
             f'<div class="card"><small>Total money available</small><b>{amt(em.total_fund(d))}</b></div>'
             f'<div class="card"><small>Money left now</small><b>{amt(em.balance(d))}</b></div></div>')
    rows = []
    for n, (e, _left) in enumerate(em.ledger_rows(d), 1):
        delete = (f'<form method="post" action="/delete" onsubmit="{CONFIRM_DELETE}">'
                  f'<input type="hidden" name="m" value="{ymstr}">'
                  f'<input type="hidden" name="id" value="{e["id"]}">'
                  '<button class="btn del">Delete</button></form>')
        rows.append([em.fmt_date(e["date"]), h(e["product"]), num(e["cost"]), delete])
    entries = (table(["Date", "Product bought", "Cost (Rs.)", ""], rows,
                     right=(2, 3), wrap=(1,)) if rows else '<p class="msg">No expenses yet.</p>')
    pdf = f'<p><a class="btn" href="/pdf?m={ymstr}">Download PDF report</a></p>'
    return page("Holder panel", top + cards + add_form(ym) + extra_form(d, ym)
                + "<h2>Entries</h2>" + entries + extra_list(d, ym) + pdf)


# --------------------------------------------------------------------------
# The web server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    timeout = 15  # drop connections that stay silent (protects against slow clients)

    def log_message(self, *args):
        pass  # keep the console quiet

    def reply(self, body, status=200, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, where, headers=()):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()

    def cookie(self, name):
        try:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return ""
        return jar[name].value if name in jar else ""

    def session_cookie(self, name, token, max_age):
        flags = f"Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https":
            flags += "; Secure"  # the tunnel is using https
        return ("Set-Cookie", f"{name}={token}; {flags}")

    def is_holder(self):
        return TOKENS.get(self.cookie("holder"), 0) > time.time()

    def can_view(self):
        if not VIEWER_CODE or self.is_holder():
            return True
        return VIEWERS.get(self.cookie("viewer"), 0) > time.time()

    def read_form(self):
        try:
            size = min(int(self.headers.get("Content-Length") or 0), 20000)
        except ValueError:
            size = 0
        raw = self.rfile.read(size).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def check_secret(self, path, f):
        """Holder PIN (/login) or viewer access code (/enter), with lock-out."""
        is_login = path == "/login"
        if not is_login and not VIEWER_CODE:
            self.redirect("/")
            return
        secret = PIN if is_login else VIEWER_CODE
        back = path + "?err="
        now = time.time()
        with LOCK:
            FAILS[:] = [t for t in FAILS if now - t < 600]
            locked = len(FAILS) >= MAX_FAILS
        if locked:
            self.redirect(back + quote("Too many wrong attempts. Please wait 10 minutes and try again."))
            return
        given = f.get("pin" if is_login else "code", "").encode()
        if hmac.compare_digest(given, secret.encode()):
            token = secrets.token_urlsafe(24)
            if is_login:
                TOKENS[token] = now + 12 * 3600
                self.redirect("/manage", [self.session_cookie("holder", token, 12 * 3600)])
            else:
                VIEWERS[token] = now + 30 * 86400
                self.redirect("/", [self.session_cookie("viewer", token, 30 * 86400)])
        else:
            with LOCK:
                FAILS.append(now)
            time.sleep(1)  # slows down guessing
            wrong = "Wrong PIN. Please try again." if is_login else "Wrong access code. Please try again."
            self.redirect(back + quote(wrong))

    def do_GET(self):
        try:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            ym = parse_month(q.get("m"))
            if url.path == "/":
                if self.can_view():
                    self.reply(report_page(ym))
                else:
                    self.redirect("/enter")
            elif url.path == "/manage":
                if self.is_holder():
                    self.reply(manage_page(ym, q))
                else:
                    self.redirect("/login")
            elif url.path == "/login":
                self.reply(login_page(q))
            elif url.path == "/enter":
                self.reply(enter_page(q))
            elif url.path == "/logout":
                TOKENS.pop(self.cookie("holder"), None)
                VIEWERS.pop(self.cookie("viewer"), None)
                self.redirect("/", [("Set-Cookie", "holder=; Path=/; Max-Age=0"),
                                    ("Set-Cookie", "viewer=; Path=/; Max-Age=0")])
            elif url.path == "/pdf":
                self.send_pdf(ym)
            else:
                self.reply(page("Not found", '<h1>Page not found</h1><p><a href="/">Go to the report</a></p>'), 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.reply(page("Error", "<h1>Something went wrong</h1><p>Please go back and try again.</p>"), 500)

    def do_POST(self):
        try:
            url = urlparse(self.path)
            f = self.read_form()
            ym = parse_month(f.get("m"))
            if url.path in ("/login", "/enter"):
                self.check_secret(url.path, f)
                return
            if not self.is_holder():
                self.redirect("/login")
                return
            actions = {"/setup": do_setup, "/add": do_add, "/delete": do_delete,
                       "/extra": do_extra, "/delete_extra": do_delete_extra}
            if url.path not in actions:
                self.reply(page("Not found", "<h1>Page not found</h1>"), 404)
                return
            kind, text = actions[url.path](ym, f)
            self.redirect(f"/manage?m={ym_str(ym)}&{kind}={quote(text)}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.reply(page("Error", "<h1>Something went wrong</h1><p>Please go back and try again.</p>"), 500)

    def send_pdf(self, ym):
        if not self.can_view():
            self.redirect("/enter")
            return
        d = load(ym)
        if d is None:
            self.reply(page("No data", "<h1>No account for this month yet</h1>"), 404)
            return
        try:
            data = make_pdf_bytes(d)
        except ImportError:
            self.reply(page("PDF library missing", "<h1>PDF library missing</h1>"
                            "<p>On the holder's laptop run: <code>python -m pip install reportlab</code></p>"), 500)
            return
        name = f"Food_Expense_Report_{ym_str(ym)}.pdf"
        self.reply(data, ctype="application/pdf",
                   headers=[("Content-Disposition", f'attachment; filename="{name}"')])


def lan_ip():
    """The laptop's address on the Wi-Fi (no data is actually sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main():
    global PIN, VIEWER_CODE
    print("=" * 66)
    print("  BACHELORS' FOOD EXPENSE MANAGER - WEB VERSION")
    print("=" * 66)
    PIN = os.environ.get("HOLDER_PIN", "").strip()
    while len(PIN) < 4:
        PIN = input("Choose a PIN for the holder (at least 4 characters): ").strip()

    if "VIEWER_CODE" in os.environ:
        VIEWER_CODE = os.environ["VIEWER_CODE"].strip()
        if VIEWER_CODE and (len(VIEWER_CODE) < 4 or VIEWER_CODE == PIN):
            print("The viewer code needs 4+ characters and must differ from the holder PIN.")
            return
    else:
        while True:
            VIEWER_CODE = input("Access code for the other bachelors to VIEW the report\n"
                                "(press Enter for none - only safe on home Wi-Fi): ").strip()
            if VIEWER_CODE == "" or (len(VIEWER_CODE) >= 4 and VIEWER_CODE != PIN):
                break
            print("  ! The code needs 4+ characters and must be different from the holder PIN.")

    host = os.environ.get("HOST", "0.0.0.0")
    ports = [int(os.environ["PORT"])] if os.environ.get("PORT", "").isdigit() else range(8000, 8011)
    server = None
    for port in ports:
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("Could not start: the port is busy. Close other programs and retry.")
        return

    print("\nThe server is running.\n")
    print(f"  Holder (on this laptop):   http://localhost:{port}/manage")
    print(f"  Others on the SAME Wi-Fi:  http://{lan_ip()}:{port}")
    print("\n  To share it over the INTERNET, open a SECOND terminal and run:")
    print(f"      cloudflared tunnel --url http://localhost:{port}")
    print("  then send the https://....trycloudflare.com link to the others.")
    if not VIEWER_CODE:
        print("\n  ! No viewer code is set: anyone who gets the link can see the report.")
        print("    Restart and set a code before sharing it over the internet.")
    print("\n  Keep this window open. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. All data is saved.")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nClosed.")
