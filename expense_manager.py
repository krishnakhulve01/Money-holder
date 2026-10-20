"""
==========================================================================
  BACHELORS' MONTHLY FOOD EXPENSE MANAGER
==========================================================================
One bachelor (the "account holder") manages the food money of four
bachelors for a month.

What it does
  1. Asks the holder's name and the month to manage.
  2. Asks the money received from all four bachelors and the money left
     over from the previous month  ->  total money for the month.
  3. Every day: asks the date, the product bought and its cost, shows
     them, and shows the money still remaining.
  4. Saves everything after EVERY entry (data/ folder), so nothing is
     lost even if the program is closed.
  5. At month end: shows the full monthly expenses and offers a PDF
     report (reports/ folder) with money received, spent and left.

To run:   python expense_manager.py
PDF needs one library (only once):   pip install reportlab
"""

import calendar
import json
import os
import psycopg2
import re
import subprocess
import sys
import textwrap
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")        # monthly data files
REPORT_DIR = os.path.join(BASE_DIR, "reports")   # PDF reports

CUR = "Rs."                                      # currency label
WIDTH = 72                                       # width of console screen
MAX_AMOUNT = Decimal("10000000")                 # sanity limit for typos
MONEY_PATTERN = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


# --------------------------------------------------------------------------
# Small display helpers
# --------------------------------------------------------------------------
def line(char="-"):
    print(char * WIDTH)


def heading(text):
    print()
    line("=")
    print(text.center(WIDTH))
    line("=")


def rs(value):
    """Format a number as money, e.g.  Rs. 1,250.50"""
    return f"{CUR} {Decimal(value):,.2f}"


def row(label, value):
    print(f"  {label:<44}{value:>26}")


def month_label(year, month):
    return f"{calendar.month_name[month]} {year}"


def fmt_date(iso):
    return date.fromisoformat(iso).strftime("%d-%m-%Y")


# --------------------------------------------------------------------------
# Safe input helpers (they keep asking until the answer is valid)
# --------------------------------------------------------------------------
def ask_yes_no(prompt):
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  ! Please type y or n.")


def pause():
    """Keep the result on screen until the user presses Enter."""
    input("\nPress Enter to go back to the menu...")


def ask_name(prompt, taken=()):
    taken_lower = {t.lower() for t in taken}
    while True:
        name = " ".join(input(prompt).split())
        if not name:
            print("  ! The name cannot be empty.")
        elif len(name) > 20:
            print("  ! Please use a shorter name (maximum 20 characters).")
        elif not name[0].isalpha() or not all(c.isalpha() or c in " .'-" for c in name):
            print("  ! Please use letters only (spaces, . ' - are allowed).")
        elif name.lower() in taken_lower:
            print("  ! This name is already used. Please enter a different one.")
        else:
            return name


def ask_money(prompt, positive=False, blank_means_zero=False):
    """Ask for an amount. Only numbers like 250 or 99.50 are accepted."""
    while True:
        raw = input(prompt).strip().replace(",", "")
        if raw == "" and blank_means_zero:
            return ZERO
        if not MONEY_PATTERN.fullmatch(raw):
            print("  ! Please enter a valid amount, for example 250 or 99.50")
            continue
        value = Decimal(raw).quantize(CENT)
        if positive and value <= 0:
            print("  ! The amount must be more than 0.")
        elif value > MAX_AMOUNT:
            print(f"  ! That is too large (maximum {rs(MAX_AMOUNT)}). Please check.")
        else:
            return value


def ask_month():
    today = date.today()
    while True:
        raw = input(f"Enter the month to manage as MM-YYYY "
                    f"(press Enter for {today:%B %Y}): ").strip()
        if not raw:
            return today.year, today.month
        parts = raw.replace("/", "-").split("-")
        try:
            if len(parts) == 1:
                month, year = int(parts[0]), today.year
            elif len(parts) == 2:
                month, year = int(parts[0]), int(parts[1])
            else:
                raise ValueError
            if year < 100:
                year += 2000
            if not (1 <= month <= 12 and 2000 <= year <= 2100):
                raise ValueError
            return year, month
        except ValueError:
            print("  ! Please type like 10-2026 (month-year).")


def parse_date(text, year, month):
    """Accepts DD, DD-MM or DD-MM-YYYY. Date must be inside the month."""
    parts = text.replace("/", "-").replace(".", "-").split("-")
    try:
        numbers = [int(p) for p in parts]
        if len(numbers) == 1:
            day, m, y = numbers[0], month, year
        elif len(numbers) == 2:
            day, m, y = numbers[0], numbers[1], year
        elif len(numbers) == 3:
            day, m, y = numbers
            if y < 100:
                y += 2000
        else:
            raise ValueError
        result = date(y, m, day)
    except ValueError:
        raise ValueError("That is not a valid date. Type like 05, 05-10 or 05-10-2026.")
    if (result.year, result.month) != (year, month):
        raise ValueError(f"The date must be inside {month_label(year, month)}.")
    return result


# --------------------------------------------------------------------------
# Saving and loading data (one JSON file per month)
# --------------------------------------------------------------------------
REQUIRED_KEYS = {"year", "month", "holder", "members", "received",
                 "previous_balance", "expenses", "next_id"}


def data_path(year, month):
    return os.path.join(DATA_DIR, f"{year}-{month:02d}.json")


def save_data(d):
    """Write safely: first to a temp file, then replace the real file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = data_path(d["year"], d["month"])
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def load_data(year, month):
    """Return the saved month, or None if there is none / it is damaged."""
    path = data_path(year, month)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not REQUIRED_KEYS <= set(d):
            raise ValueError("missing fields")
        if d["year"] != year or d["month"] != month:
            raise ValueError("wrong month")
        Decimal(d["previous_balance"])
        for value in d["received"].values():
            Decimal(value)
        for e in d["expenses"]:
            Decimal(e["cost"])
            date.fromisoformat(e["date"])
            e["id"], e["product"]
        for x in d.get("extra_received", []):
            Decimal(x["amount"])
            date.fromisoformat(x["date"])
            x["id"], x["name"]
        return d
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        broken = path + ".damaged"
        try:
            os.replace(path, broken)
            print(f"\n  ! The saved file for {month_label(year, month)} was damaged.")
            print(f"    It was kept as: {broken}")
        except OSError:
            pass
        return None


# --------------------------------------------------------------------------
# Money calculations (Decimal is used, so there are no rounding errors)
# --------------------------------------------------------------------------
def extras(d):
    """Additional amounts received during the month, in date order.
    (Files saved by older versions have none.)"""
    return sorted(d.get("extra_received", []), key=lambda x: (x["date"], x["id"]))


def total_extra(d):
    return sum((Decimal(x["amount"]) for x in extras(d)), ZERO)


def total_received(d):
    """Money received at the start of the month + additional amounts."""
    return sum((Decimal(v) for v in d["received"].values()), ZERO) + total_extra(d)


def total_fund(d):
    return total_received(d) + Decimal(d["previous_balance"])


def total_spent(d):
    return sum((Decimal(e["cost"]) for e in d["expenses"]), ZERO)


def balance(d):
    return total_fund(d) - total_spent(d)


def ledger_rows(d):
    """Expenses arranged by date (then by entry order) with the money left
    after each purchase (additional amounts count from their own date)."""
    ordered = sorted(d["expenses"], key=lambda e: (e["date"], e["id"]))
    start = total_fund(d) - total_extra(d)       # money available on day one
    extra_list = extras(d)
    spent = ZERO
    rows = []
    for e in ordered:
        # an additional amount counts from the day it was received
        arrived = sum((Decimal(x["amount"]) for x in extra_list if x["date"] <= e["date"]), ZERO)
        spent += Decimal(e["cost"])
        rows.append((e, start + arrived - spent))
    return rows


def daywise_rows(d):
    days = {}
    for e, left in ledger_rows(d):
        day = days.setdefault(e["date"], {"items": 0, "spent": ZERO, "left": left})
        day["items"] += 1
        day["spent"] += Decimal(e["cost"])
        day["left"] = left
    return list(days.items())


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------
def print_summary(d, final=False):
    print("\nMONEY SUMMARY")
    line()
    for name in d["members"]:
        tag = " (holder)" if name == d["holder"] else ""
        row(f"Received from {name}{tag}", rs(d["received"][name]))
    for x in extras(d):
        row(f"Extra from {x['name']} ({fmt_date(x['date'])})", rs(x["amount"]))
    line()
    row("Total money received this month", rs(total_received(d)))
    row("Money left from previous month", rs(d["previous_balance"]))
    row("TOTAL MONEY AVAILABLE", rs(total_fund(d)))
    row("Total spent this month" if final else "Total spent so far", rs(total_spent(d)))
    line("=")
    row("MONEY LEFT AT END OF MONTH" if final else "MONEY LEFT NOW", rs(balance(d)))
    line("=")


def print_ledger(d):
    rows = ledger_rows(d)
    if not rows:
        print("\n  No expenses entered yet.")
        return
    print(f"\n{'No.':<4} {'Date':<11} {'Product bought':<28} {'Cost':>11} {'Money left':>13}")
    line()
    for n, (e, left) in enumerate(rows, 1):
        parts = textwrap.wrap(e["product"], 28) or [""]
        print(f"{n:<4} {fmt_date(e['date']):<11} {parts[0]:<28} "
              f"{Decimal(e['cost']):>11,.2f} {left:>13,.2f}")
        for extra in parts[1:]:
            print(f"{'':<4} {'':<11} {extra:<28}")
    line()
    print(f"{'':<4} {'':<11} {'TOTAL SPENT':<28} {total_spent(d):>11,.2f}")


def print_daywise(d):
    days = daywise_rows(d)
    if not days:
        return
    print("\nDAY-WISE TOTALS")
    print(f"{'Date':<12} {'Items':>6} {'Spent that day':>16} {'Money left':>16}")
    line()
    for iso, day in days:
        print(f"{fmt_date(iso):<12} {day['items']:>6} {day['spent']:>16,.2f} {day['left']:>16,.2f}")
    line()


# --------------------------------------------------------------------------
# Starting a new month
# --------------------------------------------------------------------------
def ask_previous_balance(year, month):
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    previous = load_data(py, pm)
    if previous is not None:
        left = balance(previous)
        print(f"\nSaved data for {month_label(py, pm)} found. "
              f"It ended with {rs(left)} left.")
        if ask_yes_no("  Use this as the money left from the previous month? (y/n): "):
            return left
    return ask_money("\nMoney left from the previous month "
                     f"(press Enter if nothing): {CUR} ", blank_means_zero=True)


def setup_new_month(year, month, holder):
    heading(f"NEW MONTH SETUP - {month_label(year, month)}")
    while True:
        members = [holder]
        print(f"\nAccount holder: {holder}")
        print("Enter the names of the other three bachelors.")
        for number in range(2, 5):
            members.append(ask_name(f"  Name of bachelor {number}: ", taken=members))

        print("\nMoney received this month from each bachelor (type 0 if none):")
        received = {}
        for name in members:
            received[name] = ask_money(f"  Money received from {name}: {CUR} ")

        previous = ask_previous_balance(year, month)

        d = {
            "year": year,
            "month": month,
            "holder": holder,
            "members": members,
            "received": {k: str(v) for k, v in received.items()},
            "previous_balance": str(previous),
            "expenses": [],
            "extra_received": [],
            "next_id": 1,
        }
        print_summary(d)
        if ask_yes_no("\nIs everything correct? (y/n): "):
            break
        print("\nOkay, let us enter the details again.")
    save_data(d)
    print("\nMonth created and saved. You can now start entering expenses.")
    return d


# --------------------------------------------------------------------------
# Adding / deleting expenses
# --------------------------------------------------------------------------
def ask_date(d):
    today = date.today()
    inside = (today.year, today.month) == (d["year"], d["month"])
    hint = ", Enter = today" if inside else ""
    while True:
        raw = input(f"\nEnter the date (DD, DD-MM or DD-MM-YYYY{hint}; m = menu): ").strip()
        if raw.lower() in ("m", "menu"):
            return None
        if not raw:
            if inside:
                return today
            print("  ! Today is not in this month. Please type the date.")
            continue
        try:
            return parse_date(raw, d["year"], d["month"])
        except ValueError as err:
            print(f"  ! {err}")


def add_expenses(d):
    heading("ADD EXPENSES")
    day = ask_date(d)
    if day is None:
        return
    iso = day.isoformat()
    print(f"\nPurchases for {fmt_date(iso)}.  Money available: {rs(balance(d))}")

    while True:
        product = " ".join(
            input("\n  Product bought (press Enter when finished for this day): ").split())
        if not product:
            break
        if len(product) > 60:
            print("  ! Please use a shorter name (maximum 60 characters).")
            continue

        cost = ask_money(f"  Cost of {product}: {CUR} ", positive=True)

        left = balance(d)
        if cost > left:
            print(f"  ! Only {rs(left)} is left, but this costs {rs(cost)}.")
            if not ask_yes_no("    Add it anyway? The balance will become negative (y/n): "):
                print("    Not added.")
                continue

        d["expenses"].append({"id": d["next_id"], "date": iso,
                              "product": product, "cost": str(cost)})
        d["next_id"] += 1
        save_data(d)

        print()
        line()
        print(f"  Date    : {fmt_date(iso)}")
        print(f"  Product : {product}")
        print(f"  Cost    : {rs(cost)}")
        print(f"  Money left now : {rs(balance(d))}")
        line()

    last_day = calendar.monthrange(day.year, day.month)[1]
    if day.day == last_day:
        print("\nToday is the LAST DAY of the month.")
        if ask_yes_no("Do you want to see the monthly expenses now? (y/n): "):
            month_end(d)


def add_extra_money(d):
    heading("ADD ADDITIONAL MONEY RECEIVED")
    print("\nWho gave the additional money?")
    for number, name in enumerate(d["members"], 1):
        print(f"  {number}. {name}")
    while True:
        raw = input("\nEnter the number (press Enter to cancel): ").strip()
        if not raw:
            return
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(d["members"]):
            name = d["members"][int(raw) - 1]
            break
        print("  ! Please enter a number from the list.")
    day = ask_date(d)
    if day is None:
        return
    value = ask_money(f"Additional money received from {name}: {CUR} ", positive=True)
    d.setdefault("extra_received", []).append(
        {"id": d["next_id"], "date": day.isoformat(), "name": name, "amount": str(value)})
    d["next_id"] += 1
    save_data(d)
    print()
    line()
    print(f"  From    : {name}")
    print(f"  Date    : {fmt_date(day.isoformat())}")
    print(f"  Amount  : {rs(value)}")
    print(f"  Total money available now : {rs(total_fund(d))}")
    print(f"  Money left now            : {rs(balance(d))}")
    line()
    pause()


def delete_extra_money(d):
    heading("DELETE AN ADDITIONAL AMOUNT")
    items = extras(d)
    print(f"\n{'No.':<4} {'Date':<11} {'Received from':<22} {'Amount':>12}")
    line()
    for n, x in enumerate(items, 1):
        print(f"{n:<4} {fmt_date(x['date']):<11} {x['name']:<22} {Decimal(x['amount']):>12,.2f}")
    line()
    while True:
        raw = input("\nEnter the No. to delete (press Enter to cancel): ").strip()
        if not raw:
            return
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(items):
            break
        print("  ! Please enter a number from the list.")
    chosen = items[int(raw) - 1]
    if balance(d) - Decimal(chosen["amount"]) < 0:
        print("  ! Money has already been spent, so the money left will become negative.")
    if ask_yes_no("  Delete this amount? (y/n): "):
        d["extra_received"] = [x for x in d["extra_received"] if x["id"] != chosen["id"]]
        save_data(d)
        print(f"  Deleted. Money left now: {rs(balance(d))}")
    else:
        print("  Nothing was deleted.")
    pause()


def delete_entry(d):
    """Option 4: delete a wrong expense (or a wrong additional amount)."""
    if not extras(d):
        delete_expense(d)
        return
    heading("DELETE A WRONG ENTRY")
    print("\n  1. An expense")
    print("  2. An additional amount received")
    while True:
        choice = input("\nChoose 1 or 2 (press Enter to cancel): ").strip()
        if choice == "":
            return
        if choice == "1":
            delete_expense(d, show_heading=False)
            return
        if choice == "2":
            delete_extra_money(d)
            return
        print("  ! Please type 1 or 2.")


def delete_expense(d, show_heading=True):
    if show_heading:
        heading("DELETE A WRONG ENTRY")
    rows = ledger_rows(d)
    if not rows:
        print("\n  There is nothing to delete yet.")
        pause()
        return
    print_ledger(d)
    while True:
        raw = input("\nEnter the No. of the entry to delete (press Enter to cancel): ").strip()
        if not raw:
            return
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(rows):
            break
        print("  ! Please enter a number from the list.")
    entry, _ = rows[int(raw) - 1]
    print(f"\n  {fmt_date(entry['date'])} | {entry['product']} | {rs(entry['cost'])}")
    if ask_yes_no("  Delete this entry? (y/n): "):
        d["expenses"] = [e for e in d["expenses"] if e["id"] != entry["id"]]
        save_data(d)
        print(f"  Deleted. Money left now: {rs(balance(d))}")
    else:
        print("  Nothing was deleted.")
    pause()


# --------------------------------------------------------------------------
# Month end + PDF
# --------------------------------------------------------------------------
def month_end(d):
    heading(f"MONTHLY EXPENSES - {month_label(d['year'], d['month'])}")
    print(f"Account holder: {d['holder']}")
    print_ledger(d)
    print_daywise(d)
    print_summary(d, final=True)
    if ask_yes_no("\nDo you want to print (save) the PDF of the monthly expenses? (y/n): "):
        make_pdf(d)
    pause()


def latin(text):
    """The built-in PDF font supports Latin letters only."""
    return text.encode("latin-1", "replace").decode("latin-1")


def build_pdf(d, path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    label = month_label(d["year"], d["month"])
    navy = colors.HexColor("#1F3A5F")
    zebra = colors.HexColor("#F3F6FA")
    grid = colors.HexColor("#C8D0DA")

    base = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=base["Title"], fontSize=21, textColor=navy, spaceAfter=2)
    sub_style = ParagraphStyle("S", parent=base["Normal"], alignment=1, fontSize=12,
                               textColor=colors.grey, spaceAfter=12)
    h2 = ParagraphStyle("H2", parent=base["Heading2"], fontSize=13, textColor=navy,
                        spaceBefore=16, spaceAfter=6)
    cell = ParagraphStyle("C", parent=base["Normal"], fontSize=9, leading=11)
    cell_r = ParagraphStyle("CR", parent=cell, alignment=2)
    head = ParagraphStyle("HD", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    head_r = ParagraphStyle("HDR", parent=head, alignment=2)

    def text(value, bold=False, style=cell):
        safe = escape(latin(str(value)))
        return Paragraph(f"<b>{safe}</b>" if bold else safe, style)

    def amount(value, bold=False, prefix=""):
        v = Decimal(value)
        shown = f"{prefix}{v:,.2f}"
        if v < 0:
            shown = f'<font color="#B00020">{shown}</font>'
        return Paragraph(f"<b>{shown}</b>" if bold else shown, cell_r)

    def basic_style(extra=()):
        return TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, grid),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            *extra,
        ])

    header_style = [("BACKGROUND", (0, 0), (-1, 0), navy)]
    zebra_style = [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra])]

    story = [Paragraph("Monthly Food Expense Report", title_style),
             Paragraph(escape(label), sub_style)]

    # -- who / when ---------------------------------------------------------
    info = Table([
        [text("Account holder", True), text(d["holder"])],
        [text("Bachelors", True), text(", ".join(d["members"]))],
        [text("Report printed on", True), text(datetime.now().strftime("%d-%m-%Y  %I:%M %p"))],
    ], colWidths=[45 * mm, 135 * mm])
    info.setStyle(basic_style([("BACKGROUND", (0, 0), (0, -1), zebra)]))
    story.append(info)

    # -- money received / left ---------------------------------------------
    story.append(Paragraph("1. Money Received and Money Left", h2))
    summary = [[text("Particulars", style=head), Paragraph("Amount", head_r)]]
    for name in d["members"]:
        tag = " (holder)" if name == d["holder"] else ""
        summary.append([text(f"Money received from {name}{tag}"),
                        amount(d["received"][name], prefix=f"{CUR} ")])
    for x in extras(d):
        summary.append([text(f"Additional amount from {x['name']} on {fmt_date(x['date'])}"),
                        amount(x["amount"], prefix=f"{CUR} ")])
    summary += [
        [text("Total money received this month", True), amount(total_received(d), True, f"{CUR} ")],
        [text("Money left from previous month"), amount(d["previous_balance"], prefix=f"{CUR} ")],
        [text("TOTAL MONEY AVAILABLE", True), amount(total_fund(d), True, f"{CUR} ")],
        [text("Total expenses of the month"), amount(total_spent(d), prefix=f"{CUR} ")],
        [text("MONEY LEFT AT THE END OF THE MONTH", True), amount(balance(d), True, f"{CUR} ")],
    ]
    n = len(d["members"]) + len(extras(d))
    summary_table = Table(summary, colWidths=[120 * mm, 60 * mm])
    summary_table.setStyle(basic_style(header_style + [
        ("BACKGROUND", (0, n + 1), (-1, n + 1), zebra),
        ("BACKGROUND", (0, n + 3), (-1, n + 3), zebra),
        ("BACKGROUND", (0, n + 5), (-1, n + 5), colors.HexColor("#E3F1E6")),
    ]))
    story.append(summary_table)

    # -- every purchase -----------------------------------------------------
    story.append(Paragraph("2. Daily Expense Details", h2))
    rows = ledger_rows(d)
    if rows:
        data = [[text("No.", style=head), text("Date", style=head), text("Product bought", style=head),
                 Paragraph(f"Cost ({CUR})", head_r), Paragraph(f"Money left ({CUR})", head_r)]]
        for number, (e, left) in enumerate(rows, 1):
            data.append([text(number), text(fmt_date(e["date"])), text(e["product"]),
                         amount(e["cost"]), amount(left)])
        data.append([text(""), text(""), text("TOTAL SPENT", True), amount(total_spent(d), True), text("")])
        table = Table(data, colWidths=[12 * mm, 26 * mm, 72 * mm, 32 * mm, 38 * mm], repeatRows=1)
        table.setStyle(basic_style(header_style + [
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, zebra]),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E3E8EF")),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("No expenses were recorded this month.", cell))

    # -- day-wise totals ------------------------------------------------------
    days = daywise_rows(d)
    if days:
        story.append(Paragraph("3. Day-wise Totals", h2))
        data = [[text("Date", style=head), Paragraph("Items bought", head_r),
                 Paragraph(f"Spent that day ({CUR})", head_r),
                 Paragraph(f"Money left ({CUR})", head_r)]]
        for iso, day in days:
            data.append([text(fmt_date(iso)), text(day["items"], style=cell_r),
                         amount(day["spent"]), amount(day["left"])])
        table = Table(data, colWidths=[40 * mm, 30 * mm, 55 * mm, 55 * mm], repeatRows=1)
        table.setStyle(basic_style(header_style + zebra_style))
        story.append(table)

    story.append(Spacer(1, 10))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm,
                          latin(f"Food Expense Report - {label} - Holder: {d['holder']}"))
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=18 * mm,
                            title=f"Food Expense Report - {label}", author=latin(d["holder"]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def open_file(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        print("  (Could not open it automatically - please open it from the folder.)")


def make_pdf(d):
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"Food_Expense_Report_{d['year']}-{d['month']:02d}.pdf")
    try:
        build_pdf(d, path)
    except ImportError:
        print("\n  ! The PDF library is missing. Install it with:  pip install reportlab")
        print("    Then choose option 5 again to print the PDF.")
        return
    except PermissionError:
        print("\n  ! Could not write the PDF. If it is open in a PDF viewer, close it and try again.")
        return
    except OSError as err:
        print(f"\n  ! Could not create the PDF: {err}")
        return
    print(f"\n  PDF saved successfully:\n  {path}")
    if ask_yes_no("  Open it now? (y/n): "):
        open_file(path)


# --------------------------------------------------------------------------
# Main menu
# --------------------------------------------------------------------------
def main_menu(d):
    label = month_label(d["year"], d["month"])
    while True:
        heading(f"MAIN MENU - {label}  |  Holder: {d['holder']}")
        print(f"  Money left right now: {rs(balance(d))}\n")
        print("  1. Add expenses (date, product, cost)")
        print("  2. View all expenses so far")
        print("  3. View money summary (received / spent / left)")
        print("  4. Delete a wrongly entered expense or amount")
        print("  5. Month end: view monthly expenses and print PDF")
        print("  6. Add additional money received")
        print("  7. Exit")
        choice = input("\nChoose an option (1-7): ").strip()

        if choice == "1":
            add_expenses(d)
        elif choice == "2":
            heading("ALL EXPENSES")
            print_ledger(d)
            print_daywise(d)
            pause()
        elif choice == "3":
            heading("MONEY SUMMARY")
            print_summary(d)
            pause()
        elif choice == "4":
            delete_entry(d)
        elif choice == "5":
            month_end(d)
        elif choice == "6":
            add_extra_money(d)
        elif choice == "7":
            print("\nAll your data is saved. Goodbye!")
            return
        else:
            print("  ! Please choose a number from 1 to 7.")


def main():
    heading("BACHELORS' MONTHLY FOOD EXPENSE MANAGER")
    print("  Welcome! Let us manage this month's food expenses.\n")
    holder = ask_name("Enter the name of the account holder: ")
    year, month = ask_month()
    label = month_label(year, month)

    d = load_data(year, month)
    if d is not None:
        print(f"\nA saved account for {label} was found (holder: {d['holder']}).")
        if d["holder"].lower() != holder.lower():
            print(f"  ! You typed '{holder}', but this account belongs to {d['holder']}.")
            if not ask_yes_no("  Open it anyway? (y/n): "):
                print("\nGoodbye!")
                return
        if not ask_yes_no("Continue with the saved account? (y = continue, "
                          "n = start this month again): "):
            if ask_yes_no("  This replaces the saved data (a backup copy is kept). Sure? (y/n): "):
                path = data_path(year, month)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                os.replace(path, path.replace(".json", f".backup-{stamp}.json"))
                d = None

    if d is None:
        d = setup_new_month(year, month, holder)

    main_menu(d)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n\nProgram closed. Everything you entered is already saved.")
