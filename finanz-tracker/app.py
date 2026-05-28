import sqlite3
import os
import json
import threading
import time
from datetime import datetime, date
from urllib.request import urlopen, Request
from urllib.error import URLError
from flask import Flask, render_template, request, redirect, url_for, jsonify

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanz.db"))

ACCOUNTS = [
    ("DKB Girokonto", "girokonto"),
    ("Tagesgeld Gemeinschaftskonto", "tagesgeld"),
    ("Tagesgeld Rosa", "tagesgeld"),
    ("Tagesgeld Janosch", "tagesgeld"),
    ("Depot Gemeinschaftskonto", "depot"),
    ("Depot TradeRepublic", "depot"),
    ("Depot Rosa", "depot"),
    ("Depot Janosch", "depot"),
]

CATEGORIES = [
    "Wohnen",
    "Lebensmittel",
    "Mobilität",
    "Versicherungen",
    "Freizeit",
    "Kinder",
    "Kleidung",
    "Sonstiges",
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (account_id) REFERENCES accounts(id),
            UNIQUE(account_id, date)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            shares REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR',
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS depot_cash (
            account_id INTEGER PRIMARY KEY,
            amount REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT PRIMARY KEY,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            last_updated TEXT NOT NULL
        );
    """)

    for name, acc_type in ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (name, type) VALUES (?, ?)",
            (name, acc_type),
        )

    existing = conn.execute("SELECT COUNT(*) as cnt FROM holdings").fetchone()["cnt"]
    if existing == 0:
        depot_ids = {}
        for row in conn.execute("SELECT id, name FROM accounts WHERE type = 'depot'"):
            depot_ids[row["name"]] = row["id"]

        default_holdings = [
            (depot_ids["Depot Gemeinschaftskonto"], "EUNL.DE", "MSCI World", 190, "EUR"),
            (depot_ids["Depot Gemeinschaftskonto"], "CHIP.DE", "MSCI Semiconductors", 125, "EUR"),
            (depot_ids["Depot TradeRepublic"], "CAT", "Caterpillar", 4.024, "USD"),
            (depot_ids["Depot TradeRepublic"], "DCED.DE", "Data Center REITs", 93.59, "EUR"),
            (depot_ids["Depot TradeRepublic"], "SMSN.L", "Samsung", 0.5, "USD"),
            (depot_ids["Depot TradeRepublic"], "000660.KS", "SK Hynix", 1.52, "KRW"),
            (depot_ids["Depot TradeRepublic"], "JBL", "Jabil Circuit", 3.31, "USD"),
            (depot_ids["Depot TradeRepublic"], "RGTI", "Rigetti Computing", 25, "USD"),
            (depot_ids["Depot Rosa"], "EUNL.DE", "MSCI World", 166, "EUR"),
            (depot_ids["Depot Janosch"], "EUNL.DE", "MSCI World", 150, "EUR"),
        ]
        for acc_id, ticker, name, shares, currency in default_holdings:
            conn.execute(
                "INSERT INTO holdings (account_id, ticker, name, shares, currency) VALUES (?, ?, ?, ?, ?)",
                (acc_id, ticker, name, shares, currency),
            )

    conn.commit()
    conn.close()


def fetch_price(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return None


def fetch_exchange_rate(pair):
    price = fetch_price(f"{pair}=X")
    return price if price else 1.0


def update_depot_values():
    conn = get_db()
    today = date.today().isoformat()

    holdings = conn.execute("""
        SELECT h.account_id, h.ticker, h.shares, h.currency
        FROM holdings h
        JOIN accounts a ON a.id = h.account_id
        WHERE a.type = 'depot'
    """).fetchall()

    if not holdings:
        conn.close()
        return

    tickers = set(row["ticker"] for row in holdings)
    currencies_needed = set(row["currency"] for row in holdings)

    rates = {"EUR": 1.0}
    if "USD" in currencies_needed:
        rates["USD"] = fetch_exchange_rate("EURUSD")
    if "KRW" in currencies_needed:
        rates["KRW"] = fetch_exchange_rate("EURKRW")
    if "GBP" in currencies_needed:
        rates["GBP"] = fetch_exchange_rate("EURGBP")

    prices = {}
    for ticker in tickers:
        price = fetch_price(ticker)
        if price is not None:
            prices[ticker] = price
            conn.execute("""
                INSERT INTO prices (ticker, price, currency, last_updated)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET price = excluded.price, last_updated = excluded.last_updated
            """, (ticker, price, "fetched", today))

    account_values = {}
    for row in holdings:
        acc_id = row["account_id"]
        ticker = row["ticker"]
        if ticker in prices:
            currency = row["currency"]
            rate = rates.get(currency, 1.0)
            if currency != "EUR" and rate > 1:
                value_eur = (prices[ticker] * row["shares"]) / rate
            else:
                value_eur = prices[ticker] * row["shares"]
            account_values[acc_id] = account_values.get(acc_id, 0) + value_eur

    cash_rows = conn.execute("SELECT account_id, amount FROM depot_cash").fetchall()
    for row in cash_rows:
        acc_id = row["account_id"]
        if acc_id in account_values:
            account_values[acc_id] += row["amount"]
        else:
            account_values[acc_id] = row["amount"]

    for acc_id, total in account_values.items():
        conn.execute("""
            INSERT INTO balances (account_id, date, balance)
            VALUES (?, ?, ?)
            ON CONFLICT(account_id, date)
            DO UPDATE SET balance = excluded.balance
        """, (acc_id, today, round(total, 2)))

    conn.commit()
    conn.close()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Depot-Werte aktualisiert: {len(account_values)} Konten")


def price_updater():
    while True:
        try:
            update_depot_values()
        except Exception as e:
            print(f"Fehler beim Kurs-Update: {e}")
        time.sleep(3600)


@app.route("/")
def dashboard():
    conn = get_db()
    today = date.today().isoformat()

    balances = conn.execute("""
        SELECT a.name, a.type, b.balance, b.date
        FROM accounts a
        LEFT JOIN balances b ON a.id = b.account_id
            AND b.date = (SELECT MAX(b2.date) FROM balances b2 WHERE b2.account_id = a.id)
        ORDER BY a.type, a.name
    """).fetchall()

    total_main = 0
    total_rosa = 0
    total_janosch = 0
    for row in balances:
        bal = row["balance"] or 0
        name = row["name"]
        if "Rosa" in name:
            total_rosa += bal
        elif "Janosch" in name:
            total_janosch += bal
        else:
            total_main += bal

    prev_balances = conn.execute("""
        SELECT a.name, b.balance
        FROM accounts a
        LEFT JOIN balances b ON a.id = b.account_id
            AND b.date = (
                SELECT MAX(b2.date) FROM balances b2
                WHERE b2.account_id = a.id
                AND b2.date < (SELECT MAX(b3.date) FROM balances b3 WHERE b3.account_id = a.id)
            )
    """).fetchall()

    prev_main = 0
    prev_rosa = 0
    prev_janosch = 0
    for row in prev_balances:
        bal = row["balance"] or 0
        name = row["name"]
        if "Rosa" in name:
            prev_rosa += bal
        elif "Janosch" in name:
            prev_janosch += bal
        else:
            prev_main += bal

    change_main = total_main - prev_main
    change_rosa = total_rosa - prev_rosa
    change_janosch = total_janosch - prev_janosch

    recent_expenses = conn.execute("""
        SELECT * FROM expenses ORDER BY date DESC, id DESC LIMIT 10
    """).fetchall()

    now = datetime.now()
    monthly_expenses = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM expenses
        WHERE substr(date, 1, 7) = ?
    """, (f"{now.year:04d}-{now.month:02d}",)).fetchone()

    conn.close()

    return render_template(
        "dashboard.html",
        balances=balances,
        total_main=total_main,
        total_rosa=total_rosa,
        total_janosch=total_janosch,
        change_main=change_main,
        change_rosa=change_rosa,
        change_janosch=change_janosch,
        recent_expenses=recent_expenses,
        monthly_expenses=monthly_expenses["total"],
        today=today,
        categories=CATEGORIES,
    )


@app.route("/balances", methods=["GET", "POST"])
def balances():
    conn = get_db()

    if request.method == "POST":
        entry_date = request.form["date"]

        accounts = conn.execute("SELECT * FROM accounts ORDER BY type, name").fetchall()
        for account in accounts:
            balance_str = request.form.get(f"balance_{account['id']}", "0")
            balance = float(balance_str.replace(",", ".")) if balance_str else 0

            conn.execute("""
                INSERT INTO balances (account_id, date, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id, date)
                DO UPDATE SET balance = excluded.balance
            """, (account["id"], entry_date, balance))

        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    entry_date = request.args.get("date", date.today().isoformat())

    accounts = conn.execute("""
        SELECT a.*, b.balance
        FROM accounts a
        LEFT JOIN balances b ON a.id = b.account_id AND b.date = ?
        ORDER BY a.type, a.name
    """, (entry_date,)).fetchall()

    conn.close()

    return render_template(
        "balances.html",
        accounts=accounts,
        entry_date=entry_date,
    )


@app.route("/balances/history")
def balance_history():
    conn = get_db()

    entries = conn.execute("""
        SELECT b.date, a.name, a.type, b.balance
        FROM balances b
        JOIN accounts a ON a.id = b.account_id
        ORDER BY b.date DESC, a.type, a.name
    """).fetchall()

    dates = []
    grouped = {}
    for row in entries:
        d = row["date"]
        if d not in grouped:
            grouped[d] = {"date": d, "accounts": [], "total": 0}
            dates.append(d)
        grouped[d]["accounts"].append(row)
        grouped[d]["total"] += row["balance"]

    conn.close()

    return render_template(
        "balance_history.html",
        dates=dates,
        grouped=grouped,
    )


@app.route("/balances/delete/<date>", methods=["POST"])
def delete_balances(date):
    conn = get_db()
    conn.execute("DELETE FROM balances WHERE date = ?", (date,))
    conn.commit()
    conn.close()
    return redirect(url_for("balance_history"))


@app.route("/holdings")
def holdings_page():
    conn = get_db()
    depot_accounts = conn.execute(
        "SELECT * FROM accounts WHERE type = 'depot' ORDER BY name"
    ).fetchall()

    holdings = conn.execute("""
        SELECT h.*, a.name as account_name, p.price as last_price, p.last_updated
        FROM holdings h
        JOIN accounts a ON a.id = h.account_id
        LEFT JOIN prices p ON p.ticker = h.ticker
        ORDER BY a.name, h.name
    """).fetchall()

    cash = {}
    cash_rows = conn.execute("SELECT account_id, amount FROM depot_cash").fetchall()
    for row in cash_rows:
        cash[row["account_id"]] = row["amount"]

    conn.close()

    return render_template(
        "holdings.html",
        depot_accounts=depot_accounts,
        holdings=holdings,
        cash=cash,
    )


@app.route("/holdings/add", methods=["POST"])
def add_holding():
    conn = get_db()
    account_id = int(request.form["account_id"])
    ticker = request.form["ticker"].strip().upper()
    name = request.form["name"].strip()
    shares = float(request.form["shares"].replace(",", "."))
    currency = request.form.get("currency", "EUR").upper()

    conn.execute(
        "INSERT INTO holdings (account_id, ticker, name, shares, currency) VALUES (?, ?, ?, ?, ?)",
        (account_id, ticker, name, shares, currency),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("holdings_page"))


@app.route("/holdings/edit/<int:holding_id>", methods=["POST"])
def edit_holding(holding_id):
    conn = get_db()
    ticker = request.form["ticker"].strip().upper()
    name = request.form["name"].strip()
    shares = float(request.form["shares"].replace(",", "."))
    currency = request.form.get("currency", "EUR").upper()

    conn.execute(
        "UPDATE holdings SET ticker = ?, name = ?, shares = ?, currency = ? WHERE id = ?",
        (ticker, name, shares, currency, holding_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("holdings_page"))


@app.route("/holdings/delete/<int:holding_id>", methods=["POST"])
def delete_holding(holding_id):
    conn = get_db()
    conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("holdings_page"))


@app.route("/holdings/cash", methods=["POST"])
def update_cash():
    conn = get_db()
    account_id = int(request.form["account_id"])
    amount = float(request.form["amount"].replace(",", ".")) if request.form["amount"] else 0

    conn.execute("""
        INSERT INTO depot_cash (account_id, amount) VALUES (?, ?)
        ON CONFLICT(account_id) DO UPDATE SET amount = excluded.amount
    """, (account_id, amount))
    conn.commit()
    conn.close()
    return redirect(url_for("holdings_page"))


@app.route("/holdings/refresh", methods=["POST"])
def refresh_prices():
    update_depot_values()
    return redirect(url_for("holdings_page"))


@app.route("/expenses", methods=["GET", "POST"])
def expenses():
    conn = get_db()

    if request.method == "POST":
        exp_date = request.form["date"]
        amount = float(request.form["amount"].replace(",", "."))
        category = request.form["category"]
        description = request.form.get("description", "")

        conn.execute("""
            INSERT INTO expenses (date, amount, category, description)
            VALUES (?, ?, ?, ?)
        """, (exp_date, amount, category, description))
        conn.commit()
        conn.close()
        return redirect(url_for("expenses"))

    page = int(request.args.get("page", 1))
    per_page = 25
    offset = (page - 1) * per_page

    total_count = conn.execute("SELECT COUNT(*) as cnt FROM expenses").fetchone()["cnt"]
    all_expenses = conn.execute("""
        SELECT * FROM expenses ORDER BY date DESC, id DESC
        LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()

    conn.close()

    total_pages = (total_count + per_page - 1) // per_page

    return render_template(
        "expenses.html",
        expenses=all_expenses,
        categories=CATEGORIES,
        page=page,
        total_pages=total_pages,
    )


@app.route("/expenses/delete/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("expenses"))


@app.route("/charts")
def charts():
    return render_template("charts.html")


@app.route("/api/chart-data")
def chart_data():
    conn = get_db()

    history = conn.execute("""
        SELECT b.date, a.name, a.type, b.balance
        FROM balances b
        JOIN accounts a ON a.id = b.account_id
        ORDER BY b.date, a.type, a.name
    """).fetchall()

    main_history = [row for row in history if "Rosa" not in row["name"] and "Janosch" not in row["name"]]

    dates_set = sorted(set(row["date"] for row in main_history))
    labels = [d[5:] + "/" + d[:4] for d in dates_set]

    totals = []
    by_type = {"girokonto": [], "tagesgeld": [], "depot": []}

    for d in dates_set:
        day_total = 0
        type_totals = {"girokonto": 0, "tagesgeld": 0, "depot": 0}
        for row in main_history:
            if row["date"] == d:
                day_total += row["balance"]
                type_totals[row["type"]] += row["balance"]
        totals.append(day_total)
        for t in by_type:
            by_type[t].append(type_totals[t])

    depot_accounts = [row for row in main_history if row["type"] == "depot"]
    depot_names = sorted(set(row["name"] for row in depot_accounts))
    by_depot = {name: [] for name in depot_names}

    for d in dates_set:
        depot_day = {name: 0 for name in depot_names}
        for row in depot_accounts:
            if row["date"] == d:
                depot_day[row["name"]] = row["balance"]
        for name in depot_names:
            by_depot[name].append(depot_day[name])

    kids_history = [row for row in history if "Rosa" in row["name"] or "Janosch" in row["name"]]
    kids_dates = sorted(set(row["date"] for row in kids_history))
    kids_labels = [d[5:] + "/" + d[:4] for d in kids_dates]
    by_kid = {"Rosa": [], "Janosch": []}

    for d in kids_dates:
        kid_totals = {"Rosa": 0, "Janosch": 0}
        for row in kids_history:
            if row["date"] == d:
                if "Rosa" in row["name"]:
                    kid_totals["Rosa"] += row["balance"]
                elif "Janosch" in row["name"]:
                    kid_totals["Janosch"] += row["balance"]
        by_kid["Rosa"].append(kid_totals["Rosa"])
        by_kid["Janosch"].append(kid_totals["Janosch"])

    expense_data = conn.execute("""
        SELECT substr(date, 1, 7) as month, category, SUM(amount) as total
        FROM expenses
        GROUP BY substr(date, 1, 7), category
        ORDER BY month
    """).fetchall()

    expense_months = sorted(set(row["month"] for row in expense_data))
    expense_by_category = {}
    for row in expense_data:
        cat = row["category"]
        if cat not in expense_by_category:
            expense_by_category[cat] = {m: 0 for m in expense_months}
        expense_by_category[cat][row["month"]] = row["total"]

    conn.close()

    return jsonify({
        "balance": {
            "labels": labels,
            "totals": totals,
            "by_type": by_type,
            "by_depot": by_depot,
            "kids_labels": kids_labels,
            "by_kid": by_kid,
        },
        "expenses": {
            "labels": expense_months,
            "by_category": {
                cat: [vals[m] for m in expense_months]
                for cat, vals in expense_by_category.items()
            },
        },
    })


if __name__ == "__main__":
    init_db()
    updater = threading.Thread(target=price_updater, daemon=True)
    updater.start()
    print("Finanz-Tracker laeuft auf http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
