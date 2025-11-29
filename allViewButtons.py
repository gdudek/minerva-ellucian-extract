"""
scrapes all the data from the Minerva (Ellucian) “View all requests page”, opens each request in turn, and saves the data.  It is attached here.  If you are interested, read on. 
 
As you all know, the current expense report system is being discontinued.  As far as I know, all data there will be discarded including unresolved expense reports which presumably would have to be manually transferred to the new system when it becomes available someday after Feb 1.  This process seems suboptimal, but I accept it.  I discovered I had at least two expense reports which had never been fulfilled without explanation (and in one case the ER had “timed out’ and was coded “automatically suppressed”. 
 
I think it would be prudent to go over all your past ER’s and check.  Moreover, I decided I needed to archive all the historical data in case of problems later.  I wrote code to do this. 
 
The attached program will open a browser session (using Selenium/Chrome). 
 
Here how: 
0.  Launch Chrome with debugging (see below)
1.	python3 -m pip install selenium --break-system-packages 
2.	python3  allViewButtons-basic.py 
3.	Navigate to Minerva, then “finance”, then “Advances and Expense Reports Menu”, the “View All Requests”.   
4.	Select a data range and enter your McGill ID.  You will see a list of all your requests.   
5.	Hit return in the python program window and it will iterate over the requests and save each in turn as a PDF file. 
 
I believe the “View All Requests” system can only present a limited requests at once, so you may need to use suitable data ranges (e.g one year at a time). 
 
To automate Chrome, you first need to launch it to enable remote control. On MacOS it’s: 
 
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \ 
      --remote-debugging-port=9222 \ 
      --user-data-dir=/tmp/chrome-minerva-profile 

  How to use the SQL database:
      - Each processed report has a requests.id. Find it (e.g., SELECT id, reference_num, start_date FROM requests;).
      - Get all items for that report:
        SELECT * FROM summary_items WHERE request_id = ? ORDER BY row_order;
      - Totals/Grand Total/Due to Claimant are marked with row_type='total'; line items have row_type='item'.


Gregory Dudek
"""

import base64
import re
import sys
import time
import threading
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


OUTPUT_DIR = Path("pdf_output")
OUTPUT_DIR.mkdir(exist_ok=True)
DB_PATH = OUTPUT_DIR / "details.db"


def setup_driver():
    options = webdriver.ChromeOptions()
    # IMPORTANT: we’re *not* launching Chrome here, just attaching to the one you started
    #
    # Via (note: escapes only for shell example, not executed here):
    #   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    #     --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-minerva-profile
    #

    options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(5)
    return driver

def print_current_page_to_pdf(driver: webdriver.Chrome, output_path: Path):
    """Use Chrome DevTools Page.printToPDF to dump the current page to a PDF file."""
    pdf = driver.execute_cdp_cmd(
        "Page.printToPDF",
        {
            "printBackground": True,
            "landscape": False,
            "preferCSSPageSize": True,
        },
    )
    pdf_bytes = base64.b64decode(pdf["data"])
    output_path.write_bytes(pdf_bytes)


def sanitize_filename(text: str) -> str:
    text = text.strip()
    if not text:
        return "unnamed"
    # Replace anything not alphanumeric, dash, or underscore
    text = re.sub(r"[^\w\-]+", "_", text)
    return text[:80]  # keep filenames reasonable in length


def normalize_header(text: str) -> str:
    """Normalize table header text for matching."""
    return re.sub(r"\s+", " ", text.strip()).lower()


def extract_year(date_text: str) -> str:
    """Return the first 4-digit year found in the date text, or "" if none."""
    m = re.search(r"(\d{4})", date_text)
    return m.group(1) if m else ""


def table_to_pretty_lines(table_tag) -> list[str]:
    """Return a list of strings with padded columns for readability."""
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = [c.get_text(strip=True, separator=" ") for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
        else:
            # preserve blank spacer rows for readability
            rows.append([""])
    if not rows:
        return []

    # Normalize column count to the widest row
    max_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_cols:
            r.append("")

    widths = [max(len(r[c]) for r in rows) for c in range(max_cols)]

    lines = []
    # Detect header row (any <th> in first row)
    has_header = bool(table_tag.find("tr").find("th"))

    for i, r in enumerate(rows):
        padded = [r[c].ljust(widths[c]) for c in range(max_cols)]
        lines.append(" | ".join(padded).rstrip())
        if has_header and i == 0:
            # add underline after header
            underline = " | ".join("-" * widths[c] for c in range(max_cols))
            lines.append(underline)
    return lines


def parse_table_rows(table_tag):
    headers = [normalize_header(h.get_text(" ", strip=True)) for h in table_tag.find_all("th")]
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            rows.append([""])
            continue
        row = [c.get_text(" ", strip=True) for c in cells]
        rows.append(row)
    return headers, rows


def extract_summary_items(table_tag, label: str):
    headers, rows = parse_table_rows(table_tag)

    key_map = {
        "item_no": ["item #", "item"],
        "trans_date": ["trans. date", "trans date", "transaction date"],
        "description": ["description"],
        "trans_amount": ["trans. amount $", "trans amount $", "trans. amount"],
        "non_mc_expense": ["non-mcgill expense", "non mcgill expense"],
        "allowable_expense": ["allowable expenses", "allowable expense"],
        "currency": ["curr.", "currency"],
        "exch_rate": ["exch. rate", "exchange rate"],
        "cad_amount": ["expenses cad $", "cad $", "cad"]
    }

    col_index = {}
    for key, aliases in key_map.items():
        for alias in aliases:
            if alias in headers:
                col_index[key] = headers.index(alias)
                break

    def get(cell_row, key, default=""):
        idx = col_index.get(key)
        if idx is not None and idx < len(cell_row):
            return cell_row[idx]
        # Fallback positional mapping if headers missing
        positional = [
            "item_no",
            "trans_date",
            "description",
            "trans_amount",
            "non_mc_expense",
            "allowable_expense",
            "currency",
            "exch_rate",
            "cad_amount",
        ]
        if key in positional:
            idx = positional.index(key)
            return cell_row[idx] if idx < len(cell_row) else default
        return default

    items = []
    for i, row in enumerate(rows):
        # Skip header row that matches headers length/values
        if headers and all(normalize_header(x) in headers for x in row):
            continue
        if all(not cell.strip() for cell in row):
            continue
        row_type = "total" if (normalize_header(row[0]).startswith("total") or "grand total" in normalize_header(row[0]) or "due to claimant" in normalize_header(row[0])) else "item"
        items.append(
            {
                "row_order": i,
                "row_type": row_type,
                "item_no": get(row, "item_no"),
                "trans_date": get(row, "trans_date"),
                "description": get(row, "description"),
                "trans_amount": get(row, "trans_amount"),
                "non_mc_expense": get(row, "non_mc_expense"),
                "allowable_expense": get(row, "allowable_expense"),
                "currency": get(row, "currency"),
                "exch_rate": get(row, "exch_rate"),
                "cad_amount": get(row, "cad_amount"),
                "label": label,
            }
        )

    return items


def table_label(table_tag) -> str:
    """Infer a human-friendly label for a table using nearby text."""
    # Caption takes priority
    if table_tag.caption and table_tag.caption.get_text(strip=True):
        return table_tag.caption.get_text(strip=True)
    # Check preceding siblings for a short label
    prev = table_tag.previous_sibling
    steps = 0
    while prev and steps < 5:
        text = " ".join(prev.stripped_strings) if hasattr(prev, "stripped_strings") else ""
        if text:
            return text
        prev = prev.previous_sibling
        steps += 1

    # Check parent heading tags
    for tag_name in ["h1", "h2", "h3", "h4", "strong", "b"]:
        heading = table_tag.find_previous(tag_name)
        if heading and heading.get_text(strip=True):
            return heading.get_text(strip=True)

    # Fallback: first row text
    first_row = table_tag.find("tr")
    if first_row:
        return " ".join(first_row.stripped_strings)
    return "table"


def find_tables_after_heading(soup: BeautifulSoup, heading_text: str):
    """Return all tables that appear after a heading containing heading_text; fall back to all tables."""
    heading = soup.find(string=lambda t: t and heading_text.lower() in t.lower())
    if heading and hasattr(heading, "parent"):
        start_node = heading.parent
        tables = list(start_node.find_all("table"))
        if tables:
            return tables
    return list(soup.find_all("table"))


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            years TEXT,
            row_index INTEGER,
            request_date TEXT,
            start_date TEXT,
            reference_num TEXT,
            queue_title TEXT,
            pdf_path TEXT,
            txt_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            section_name TEXT,
            content TEXT,
            FOREIGN KEY(request_id) REFERENCES requests(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            row_order INTEGER,
            row_type TEXT,
            item_no TEXT,
            trans_date TEXT,
            description TEXT,
            trans_amount TEXT,
            non_mc_expense TEXT,
            allowable_expense TEXT,
            currency TEXT,
            exch_rate TEXT,
            cad_amount TEXT,
            label TEXT,
            FOREIGN KEY(request_id) REFERENCES requests(id)
        )
        """
    )
    conn.commit()
    conn.close()


def save_detail_text(driver, path: Path):
    """Save a readable text version of the current detail page and return structured sections and items."""
    soup = BeautifulSoup(driver.page_source, "html.parser")

    tables = find_tables_after_heading(soup, "Request for Expense Reimbursement")

    wanted = [
        "paid to and requested by responsible mcgill person",
        "payment information",
        "summary of expenses",
        "summary of expenses item",
        "foapal distribution",
        "approval information",
    ]

    def table_matches(tbl):
        label = table_label(tbl).strip().lower()
        text = tbl.get_text(" ", strip=True).lower()
        return any(key in label or key in text for key in wanted)

    lines = []
    sections = []
    summary_items = []
    for tbl in tables:
        if table_matches(tbl):
            label = table_label(tbl).strip() or "Table"
            lines.append(f"=== {label} ===")
            pretty = table_to_pretty_lines(tbl) or ["(table empty)"]
            lines.extend(pretty)
            lines.append("")
            sections.append((label, "\n".join(pretty)))

            if "summary of expenses" in label.lower():
                summary_items.extend(extract_summary_items(tbl, label))

    if not lines:
        # Fallback: dump up to first 5 tables with labels for debugging
        for tbl in tables[:5]:
            label = table_label(tbl).strip() or "Table"
            lines.append(f"=== {label} ===")
            pretty = table_to_pretty_lines(tbl) or ["(table empty)"]
            lines.extend(pretty)
            lines.append("")
            sections.append((label, "\n".join(pretty)))

    if not lines:
        lines = ["(no tables found)"]
        sections.append(("(no tables found)", ""))

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return sections, summary_items


def start_blinking_prompt(prompt: str = "> ", interval: float = 0.5):
    """Show a blinking block cursor after the prompt until the returned event is set."""
    stop_event = threading.Event()

    def run():
        visible = True
        while not stop_event.is_set():
            block = "\u2588" if visible else " "
            sys.stdout.write(f"\r{prompt}{block}")
            sys.stdout.flush()
            visible = not visible
            # wait allows early exit when stop_event is set
            stop_event.wait(interval)
        # clear block and leave prompt visible
        sys.stdout.write(f"\r{prompt}")
        sys.stdout.flush()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return stop_event


def extract_row_fields(btn):
    """
    Extract request date, reference #, and queue title from the data row that
    follows the View button row. This matches the structure seen on the site
    where the first `<tr>` holds buttons, and the next `<tr>` holds data cells
    with class `dddefault`.
    """
    try:
        # Grab the first few dddefault cells that appear *after* this button.
        cells = btn.find_elements(
            By.XPATH,
            "./following::td[contains(@class,'dddefault')][position()<=7]"
        )

        # Expected order (index-based) from the provided markup example:
        # 0 name, 1 request date, 2 location, 3 travel/start date, 4 code,
        # 5 reference (with title attribute), 6 amount
        request_date = cells[1].text.strip() if len(cells) > 1 else ""
        start_date = cells[3].text.strip() if len(cells) > 3 else ""
        reference_cell = cells[5] if len(cells) > 5 else None
        reference_num = reference_cell.text.strip() if reference_cell else ""
        queue_title = reference_cell.get_attribute("title") if reference_cell else ""

        return request_date, reference_num, queue_title, start_date

    except NoSuchElementException:
        return "", "", "", ""


def ensure_list_page(driver, wait) -> bool:
    """
    Make sure we're on the 'View All Requests' list page, not a detail view.
    Returns True if found, False otherwise.
    """
    for attempt in range(3):
        html = driver.page_source
        if "View All Requests" in html and "Select Document or Request" in html:
            return True  # this is the list

        # If we're on a 'View' / detail page, try going back
        driver.back()
        try:
            wait.until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass

    return False

def get_view_buttons(driver):
    # This matches value="View", value ="View ", etc.
    return driver.find_elements(
        By.XPATH,
        "//input[@type='button' and contains(normalize-space(@value), 'View')]"
    )



def main():
    driver = setup_driver()
    wait = WebDriverWait(driver, 15)
    init_db()

    try:
        print()
        print("Make sure the current tab is on the 'View All Requests' page with the View buttons.")
        print("Log in / navigate if needed, then press Enter here to start processing...")
        # Show a flashing block cursor on the same line so it's obvious input is expected.
        stop_prompt = start_blinking_prompt("> ", interval=0.5)
        try:
            input()
        finally:
            stop_prompt.set()
            # Move to the next line after stopping the prompt
            print()

        print("[DEBUG] Current URL:", driver.current_url)

        # Ensure we are on the list page, not a detail 'View' page
        if not ensure_list_page(driver, wait):
            print("[ERROR] Could not get to 'View All Requests' list page "
                  "after a few back() attempts.")
            print(driver.page_source[:1000])
            return

        # Now we *know* we're on the list page with the buttons
        try:
            wait.until(
                EC.presence_of_all_elements_located(
                    (By.XPATH,
                     "//input[@type='button' and contains(normalize-space(@value), 'View')]")
                )
            )
        except TimeoutException:
            print("[ERROR] No View buttons found on the list page.")
            print(driver.page_source[:1000])
            return

        initial_buttons = get_view_buttons(driver)
        num = len(initial_buttons)
        print(f"[INFO] Found {num} View buttons.")

        if num == 0:
            return

        # Determine year range from first and last request dates on the page
        first_req, _, _, first_start = extract_row_fields(initial_buttons[0])
        last_req, _, _, last_start = extract_row_fields(initial_buttons[-1])
        y1, y2 = extract_year(first_start), extract_year(last_start)
        if y1 and y2:
            years = y1 if y1 == y2 else f"{y1}-{y2}"
        elif y1:
            years = y1
        elif y2:
            years = y2
        else:
            years = "unknown-years"

        print(f"[INFO] Year range determined as: {years}")

        # Save the initial list page as an index PDF
        # Ensure the index filename is unique; append a counter if needed.
        base_index_name = f"{years}_index"
        index_path = OUTPUT_DIR / f"{base_index_name}.pdf"
        counter = 1
        while index_path.exists():
            index_path = OUTPUT_DIR / f"{base_index_name}-{counter}.pdf"
            counter += 1

        print(f"[INFO] Saving index page → {index_path}")
        print_current_page_to_pdf(driver, index_path)

        for idx in range(num):
            # Re-fetch on each iteration (page changes after click/back)
            view_buttons = get_view_buttons(driver)
            if idx >= len(view_buttons):
                print(f"[WARN] After navigation, only {len(view_buttons)} View buttons remain; "
                      f"skipping index {idx + 1}.")
                break

            btn = view_buttons[idx]
            request_date, reference_num, queue_title, start_date = extract_row_fields(btn)

            # Log the specific columns for the user
            print(
                f"[INFO] Row {idx + 1}: Request date='{request_date or 'N/A'}' | "
                f"Start date='{start_date or 'N/A'}' | "
                f"Reference #='{reference_num or 'N/A'}' | Queue='{queue_title or 'N/A'}'"
            )

            label_parts = [p for p in [request_date, start_date, reference_num, queue_title] if p]
            if label_parts:
                safe_label = sanitize_filename("_".join(label_parts))
            else:
                # Fallback to whatever text we can get from the enclosing row(s)
                try:
                    outer_row = btn.find_element(
                        By.XPATH,
                        "./ancestor::tr[1]"
                    )
                    row_text = outer_row.text.strip().replace("\n", " | ")
                except NoSuchElementException:
                    row_text = f"row_{idx + 1}"
                safe_label = sanitize_filename(row_text) or f"row_{idx + 1}"

            out_path = OUTPUT_DIR / f"{years}_{idx + 1:03d}_{safe_label}.pdf"

            old_url = driver.current_url
            print(f"[DEBUG] Clicking View for row {idx + 1}…")
            btn.click()

            # Wait for navigation
            try:
                wait.until(lambda d: d.current_url != old_url)
            except TimeoutException:
                pass

            try:
                wait.until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except TimeoutException:
                pass

            print(f"[INFO] Saving PDF for row {idx + 1} → {out_path}")
            print_current_page_to_pdf(driver, out_path)

            txt_path = out_path.with_suffix(".txt")
            print(f"[INFO] Saving text for row {idx + 1} → {txt_path}")
            sections, summary_items = save_detail_text(driver, txt_path)

            # Persist to SQLite
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO requests (years, row_index, request_date, start_date, reference_num, queue_title, pdf_path, txt_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    years,
                    idx + 1,
                    request_date,
                    start_date,
                    reference_num,
                    queue_title,
                    str(out_path),
                    str(txt_path),
                ),
            )
            req_id = cur.lastrowid
            for name, content in sections:
                cur.execute(
                    """
                    INSERT INTO sections (request_id, section_name, content)
                    VALUES (?, ?, ?)
                    """,
                    (req_id, name, content),
                )
            for item in summary_items:
                cur.execute(
                    """
                    INSERT INTO summary_items (
                        request_id, row_order, row_type, item_no, trans_date, description,
                        trans_amount, non_mc_expense, allowable_expense, currency, exch_rate,
                        cad_amount, label
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        req_id,
                        item.get("row_order"),
                        item.get("row_type"),
                        item.get("item_no"),
                        item.get("trans_date"),
                        item.get("description"),
                        item.get("trans_amount"),
                        item.get("non_mc_expense"),
                        item.get("allowable_expense"),
                        item.get("currency"),
                        item.get("exch_rate"),
                        item.get("cad_amount"),
                        item.get("label"),
                    ),
                )
            conn.commit()
            conn.close()

            # Go back to the list page for the next row
            print(f"[DEBUG] Going back to list after row {idx + 1}")
            driver.back()

            if not ensure_list_page(driver, wait):
                print("[ERROR] After back(), could not return to list page; stopping.")
                print(driver.page_source[:1000])
                break

        print("[INFO] Finished processing all View buttons.")

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
