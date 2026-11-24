# This script handles:

# Fetching the live active F&O stock list from NSE (fo_mktlots.csv).

# Priming an NSE web session to fetch the /api/event-calendar JSON.

# Filtering for Financial Results occurring between today and today + 15 days.

# Sending a clean HTML email table via Gmail SMTP (smtp.gmail.com:465).
import os
import time
import smtplib
from io import StringIO
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import pandas as pd
from dotenv import load_dotenv

# Load variables from .env when running locally in VS Code
load_dotenv()

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar",
}


def create_nse_session() -> requests.Session:
    """Initialize a session and prime NSE cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)

    for attempt in range(3):
        try:
            resp = session.get("https://www.nseindia.com", timeout=15)
            if resp.status_code == 200:
                time.sleep(1.5)
                return session
        except requests.RequestException as e:
            print(f"[Warn] Cookie prime attempt {attempt + 1} failed: {e}")
        time.sleep(2 * (attempt + 1))

    raise RuntimeError("Failed to initialize NSE session cookies.")


def get_active_fno_symbols(session: requests.Session) -> set[str]:
    """Fetch current F&O-eligible stock symbols from NSE's official lot size CSV."""
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()

    df = pd.read_csv(StringIO(resp.text))
    df.columns = [col.strip() for col in df.columns]

    # Exclude index derivatives (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50)
    index_symbols = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    symbols = {
        str(sym).strip().upper()
        for sym in df["SYMBOL"].dropna()
        if str(sym).strip().upper() not in index_symbols
        and "SYMBOL" not in str(sym).upper()
    }
    print(f"[Info] Loaded {len(symbols)} active F&O stocks.")
    return symbols


def get_upcoming_fno_results(session: requests.Session, fno_symbols: set[str], days_ahead: int = 15) -> pd.DataFrame:
    """Fetch NSE event calendar and filter for F&O Financial Results in the next N days."""
    url = "https://www.nseindia.com/api/event-calendar"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()

    events = resp.json()
    today = datetime.now(IST).date()
    cutoff_date = today + timedelta(days=days_ahead)

    matched_rows = []
    for item in events:
        symbol = str(item.get("symbol", "")).strip().upper()
        purpose = str(item.get("purpose", "")).strip()
        raw_date = str(item.get("date", "")).strip()

        if symbol not in fno_symbols:
            continue

        # Keep only quarterly/annual financial result announcements
        if "result" not in purpose.lower():
            continue

        try:
            event_date = datetime.strptime(raw_date, "%d-%b-%Y").date()
        except ValueError:
            continue

        if today <= event_date <= cutoff_date:
            matched_rows.append(
                {
                    "Date": event_date,
                    "Day": event_date.strftime("%a"),
                    "Symbol": symbol,
                    "Company": str(item.get("company", "")).strip(),
                    "Purpose": purpose,
                }
            )

    if not matched_rows:
        return pd.DataFrame(columns=["Date", "Day", "Symbol", "Company", "Purpose"])

    df_results = pd.DataFrame(matched_rows)
    df_results = df_results.drop_duplicates(subset=["Date", "Symbol"]).sort_values(
        by=["Date", "Symbol"]
    )
    return df_results


def build_email_html(df: pd.DataFrame, today_str: str, cutoff_str: str) -> str:
    """Build a clean HTML email body from the filtered results DataFrame."""
    if df.empty:
        return f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #222;">
            <h3>NSE F&amp;O Upcoming Results ({today_str} to {cutoff_str})</h3>
            <p>No F&amp;O stocks have scheduled financial results in the next 15 days.</p>
          </body>
        </html>
        """

    rows_html = ""
    for _, row in df.iterrows():
        rows_html += f"""
        <tr>
          <td style="padding: 8px; border: 1px solid #ddd;"><b>{row['Date'].strftime('%d-%b-%Y')}</b> ({row['Day']})</td>
          <td style="padding: 8px; border: 1px solid #ddd;"><b>{row['Symbol']}</b></td>
          <td style="padding: 8px; border: 1px solid #ddd;">{row['Company']}</td>
          <td style="padding: 8px; border: 1px solid #ddd;">{row['Purpose']}</td>
        </tr>
        """

    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #222;">
        <h3>Upcoming NSE F&amp;O Results (Next 15 Days: {today_str} to {cutoff_str})</h3>
        <p>Total F&amp;O stocks announcing results: <b>{len(df)}</b></p>
        <table style="border-collapse: collapse; width: 100%; max-width: 750px; font-size: 14px;">
          <thead>
            <tr style="background-color: #f4f6f8; text-align: left;">
              <th style="padding: 8px; border: 1px solid #ddd;">Result Date</th>
              <th style="padding: 8px; border: 1px solid #ddd;">Symbol</th>
              <th style="padding: 8px; border: 1px solid #ddd;">Company</th>
              <th style="padding: 8px; border: 1px solid #ddd;">Purpose</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
      </body>
    </html>
    """


def send_email(subject: str, html_body: str) -> None:
    """Send HTML email via Gmail SMTP."""
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        raise ValueError("Missing EMAIL_SENDER, EMAIL_PASSWORD, or EMAIL_RECEIVER in environment.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())

    print(f"[Success] Email sent to {EMAIL_RECEIVER}")


def main():
    today = datetime.now(IST).date()
    cutoff = today + timedelta(days=15)
    today_str = today.strftime("%d-%b-%Y")
    cutoff_str = cutoff.strftime("%d-%b-%Y")

    print(f"[Info] Running NSE F&O Results Check for {today_str} -> {cutoff_str}")
    session = create_nse_session()
    fno_symbols = get_active_fno_symbols(session)
    df_results = get_upcoming_fno_results(session, fno_symbols, days_ahead=15)

    print(f"[Info] Found {len(df_results)} upcoming F&O results.")
    if not df_results.empty:
        print(df_results.to_string(index=False))

    subject = f"NSE F&O Results Alert ({len(df_results)} Stocks) | {today_str}"
    html_body = build_email_html(df_results, today_str, cutoff_str)
    send_email(subject, html_body)


if __name__ == "__main__":
    main()