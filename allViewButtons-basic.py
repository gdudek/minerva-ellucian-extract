import base64
import re
import sys
import time
import threading
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


OUTPUT_DIR = Path("pdf_output")
OUTPUT_DIR.mkdir(exist_ok=True)


def setup_driver():
    options = webdriver.ChromeOptions()
    # IMPORTANT: we’re *not* launching Chrome here, just attaching to the one you started
    #
    # Via:
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


def wait_for_navigation(driver, old_url: str, clicked_element=None, timeout: float = 8.0, poll: float = 0.2) -> bool:
    """
    Wait for either a URL change or the clicked element to go stale.
    Returns True if navigation/staleness detected, False on timeout.
    Uses a shorter timeout to avoid long stalls when the site is slow.
    Safely ignores "Node does not belong to the document" inspector errors after many clicks.
    """
    nav_wait = WebDriverWait(driver, timeout, poll_frequency=poll, ignored_exceptions=(WebDriverException,))
    stale_check = EC.staleness_of(clicked_element) if clicked_element else None

    def condition(d):
        if d.current_url != old_url:
            return True
        if stale_check:
            try:
                return stale_check(d)
            except WebDriverException:
                return True
        return False

    try:
        nav_wait.until(condition)
        return True
    except TimeoutException:
        print("[WARN] Navigation not detected after click; continuing.")
        return False


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
    def click_submit_if_present():
        """If a submit-style button is present, click it and wait briefly; return True if clicked."""
        try:
            submit_btn = driver.find_element(
                By.XPATH,
                "//input[(translate(@type,'SUBMIT','submit')='submit' or "
                "contains(translate(@value,'submit','SUBMIT'),'SUBMIT'))]"
            )
        except NoSuchElementException:
            return False

        submit_btn.click()
        try:
            wait.until(lambda d: "View All Requests" in d.page_source or bool(get_view_buttons(d)))
        except TimeoutException:
            pass
        return True

    for attempt in range(3):
        html = driver.page_source
        if "View All Requests" in html and "Select Document or Request" in html:
            return True  # this is the list

        # If we land on an intermediate Search Results page, step back once.
        # If the page says "no exact matches", do NOT click submit (it may be hidden); just back out.
        if "search results" in html.lower():
            no_exact = "your search results returned no exact matches" in html.lower()
            action = "back only (no exact matches)" if no_exact else "back then submit"
            print(f"[WARN] Detected 'Search Results' page (attempt {attempt + 1}/3); action: {action}.")
            driver.back()
            try:
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
            except TimeoutException:
                pass
            html = driver.page_source
            if not no_exact:
                click_submit_if_present()
                html = driver.page_source
            if "View All Requests" in html and "Select Document or Request" in html:
                return True
            if get_view_buttons(driver):
                return True

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
        index_path = OUTPUT_DIR / f"{years}_index.pdf"
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

            # Wait for navigation or element staleness with a shorter timeout to avoid stalls
            wait_for_navigation(driver, old_url, btn, timeout=8, poll=0.2)

            try:
                wait.until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except TimeoutException:
                pass

            print(f"[INFO] Saving PDF for row {idx + 1} → {out_path}")
            print_current_page_to_pdf(driver, out_path)

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


main()
