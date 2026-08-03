"""Run an optional local desktop/mobile UI smoke test with Microsoft Edge."""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as conditions
from selenium.webdriver.support.ui import Select, WebDriverWait


def browser(width: int, height: int) -> webdriver.Edge:
    options = webdriver.EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.set_capability("ms:loggingPrefs", {"browser": "ALL"})
    driver = webdriver.Edge(options=options)
    driver.set_window_size(width, height)
    return driver


def assert_no_errors(driver: webdriver.Edge) -> None:
    expected_network_failures = ("/api/bootstrap", "/api/catalog", "/favicon.ico")
    errors = [
        entry
        for entry in driver.get_log("browser")
        if entry["level"] == "SEVERE"
        and not any(path in entry["message"] for path in expected_network_failures)
    ]
    if errors:
        raise AssertionError(f"Browser console errors: {errors}")


def stable_click(driver: webdriver.Edge, locator: tuple[str, str]) -> None:
    for _attempt in range(10):
        try:
            driver.find_element(*locator).click()
            return
        except StaleElementReferenceException:
            time.sleep(0.1)
    raise AssertionError(f"Element kept being replaced before click: {locator}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--local-token", help="Use a local launch token instead of email login")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--exercise-admin",
        action="store_true",
        help="Create, edit, and delete a temporary account. Use only against a test instance.",
    )
    args = parser.parse_args()
    if args.credentials_file:
        credentials = {}
        for line in args.credentials_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            credentials[key.strip().casefold()] = value.strip()
        args.email = credentials.get("email")
        args.password = credentials.get("password")
    if not args.local_token and (not args.email or not args.password):
        parser.error("provide --email/--password or --credentials-file")
    args.output.mkdir(parents=True, exist_ok=True)

    desktop = browser(1440, 1000)
    try:
        wait = WebDriverWait(desktop, 15)
        if args.local_token:
            separator = "&" if "?" in args.base_url else "?"
            desktop.get(f"{args.base_url}{separator}token={args.local_token}")
        else:
            desktop.get(args.base_url)
            wait.until(conditions.visibility_of_element_located((By.ID, "homeTitle")))
            desktop.find_element(By.TAG_NAME, "body").send_keys(Keys.TAB)
            if "skip-link" not in desktop.switch_to.active_element.get_attribute("class"):
                raise AssertionError("Skip link is not the first keyboard focus target")
            desktop.switch_to.active_element.send_keys(Keys.ENTER)
            desktop.save_screenshot(str(args.output / "homepage-desktop.png"))
            desktop.find_element(By.CSS_SELECTOR, ".public-actions [data-open-login]").click()
            desktop.find_element(By.CSS_SELECTOR, '#loginForm input[name="email"]').send_keys(
                args.email
            )
            desktop.find_element(By.CSS_SELECTOR, '#loginForm input[name="password"]').send_keys(
                args.password
            )
            desktop.find_element(By.CSS_SELECTOR, "#loginForm .button.primary").click()
        wait.until(conditions.visibility_of_element_located((By.ID, "appView")))
        try:
            wait.until(lambda page: page.find_element(By.ID, "connectionBadge").text == "Live")
        except Exception as exc:
            badge = desktop.find_element(By.ID, "connectionBadge").text
            diagnostics = desktop.execute_script(
                "return {transport: typeof window.LiveTransport, "
                "eventSource: typeof window.EventSource, appHidden: document.querySelector('#appView').hidden, "
                "loginError: document.querySelector('#loginError').textContent, "
                "resources: performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/'))}"
            )
            raise AssertionError(
                f"Live connection did not stabilize (badge={badge!r}, diagnostics={diagnostics}, "
                f"logs={desktop.get_log('browser')})"
            ) from exc
        date_limits = desktop.execute_script(
            "const input=document.querySelector('[name=date_from]');"
            "const now=new Date();"
            "const today=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;"
            "return {min:input.min,value:input.value,today};"
        )
        if date_limits["min"] != date_limits["today"] or date_limits["value"] != date_limits["today"]:
            raise AssertionError(f"Start date is not limited to local today: {date_limits}")
        booking_choice = desktop.find_element(By.CSS_SELECTOR, 'input[name="action_mode"][value="book"]')
        desktop.execute_script("arguments[0].click()", booking_choice)
        if desktop.find_element(By.ID, "metricMode").text != "Boka åt mig":
            raise AssertionError("Booking choice did not update the selected action")
        if not desktop.execute_script("return monitorPayload().auto_book"):
            raise AssertionError("Booking choice was not included in the monitor payload")
        status_labels = desktop.execute_script(
            "renderStatus(clientStatus('monitoring'));"
            "return ['statusBadge','statusTitle','metricEvent','footerStatus'].map(id=>document.getElementById(id).textContent);"
        )
        if len(set(status_labels)) != 1 or status_labels[0] != "Bevakning aktiv":
            raise AssertionError(f"Status surfaces disagree: {status_labels}")
        if desktop.find_element(By.ID, "stopButton").is_enabled() is False:
            raise AssertionError("Stop must be enabled for the monitoring state")
        desktop.execute_script("renderStatus(clientStatus('monitor_starting'))")
        if desktop.find_element(By.ID, "startButton").is_enabled():
            raise AssertionError("Start must be disabled while monitoring is starting")
        visible_text = desktop.find_element(By.TAG_NAME, "body").text.casefold()
        if "intervall" in visible_text or "15 sek" in visible_text:
            raise AssertionError("The fixed polling interval is still visible to the user")
        if not args.local_token:
            desktop.find_element(By.CSS_SELECTOR, '[data-admin-view="users"]').click()
            wait.until(conditions.visibility_of_element_located((By.ID, "userList")))
            wait.until(lambda page: page.find_elements(By.CSS_SELECTOR, ".user-row"))
            users_section = desktop.find_element(By.ID, "users")
            desktop.execute_script(
                "arguments[0].scrollIntoView({behavior:'instant',block:'start'})", users_section
            )
            wait.until(
                lambda page: (
                    0
                    <= page.execute_script(
                        "return arguments[0].getBoundingClientRect().top", users_section
                    )
                    < page.execute_script("return innerHeight")
                )
            )
        if args.exercise_admin:
            email = f"ui-smoke-{uuid.uuid4().hex[:10]}@example.test"
            desktop.find_element(By.ID, "createUser").click()
            wait.until(conditions.visibility_of_element_located((By.ID, "userDialog")))
            desktop.find_element(By.CSS_SELECTOR, '#userForm input[name="email"]').send_keys(email)
            desktop.find_element(By.CSS_SELECTOR, '#userForm input[name="display_name"]').send_keys(
                "UI Smoke User"
            )
            desktop.find_element(By.CSS_SELECTOR, "#userForm .button.primary").click()
            wait.until(conditions.visibility_of_element_located((By.ID, "resetLinkDialog")))
            if "?reset=" not in desktop.find_element(By.ID, "resetLink").get_attribute("value"):
                raise AssertionError("Admin create did not produce a password reset link")
            desktop.find_element(By.CSS_SELECTOR, "[data-close-reset-dialog]").click()

            search = desktop.find_element(By.ID, "userSearch")
            search.send_keys(email)
            desktop.find_element(By.CSS_SELECTOR, "#userSearchForm .button.secondary").click()
            card_xpath = f'//article[contains(@class,"user-row")][.//strong[text()="{email}"]]'
            card = wait.until(conditions.visibility_of_element_located((By.XPATH, card_xpath)))
            if email not in card.text:
                raise AssertionError("Admin search returned the wrong account")
            wait.until(conditions.element_to_be_clickable((By.XPATH, f"{card_xpath}//button")))
            stable_click(desktop, (By.XPATH, f"{card_xpath}//button"))
            wait.until(conditions.visibility_of_element_located((By.ID, "userDialog")))
            name = desktop.find_element(By.CSS_SELECTOR, '#userForm input[name="display_name"]')
            name.clear()
            name.send_keys("Edited UI User")
            Select(
                desktop.find_element(By.CSS_SELECTOR, '#userForm select[name="status"]')
            ).select_by_value("active")
            paid = desktop.find_element(By.CSS_SELECTOR, '#userForm input[name="paid"]')
            if not paid.is_selected():
                paid.click()
            desktop.find_element(By.CSS_SELECTOR, "#userForm .button.primary").click()
            wait.until(conditions.invisibility_of_element_located((By.ID, "userDialog")))
            card = wait.until(conditions.visibility_of_element_located((By.XPATH, card_xpath)))
            if "Edited UI User" not in card.text or "active" not in card.text:
                raise AssertionError("Admin edit was not reflected in the user list")
            wait.until(conditions.element_to_be_clickable((By.XPATH, f"{card_xpath}//button")))
            stable_click(desktop, (By.XPATH, f"{card_xpath}//button"))
            wait.until(conditions.visibility_of_element_located((By.ID, "userDialog")))
            desktop.find_element(By.ID, "deleteUser").click()
            wait.until(conditions.alert_is_present()).accept()
            wait.until(conditions.invisibility_of_element_located((By.CSS_SELECTOR, ".user-row")))
        desktop.save_screenshot(str(args.output / "admin-desktop.png"))
        assert_no_errors(desktop)
    finally:
        desktop.quit()

    mobile = browser(390, 844)
    try:
        mobile.get(args.base_url)
        wait = WebDriverWait(mobile, 15)
        wait.until(conditions.visibility_of_element_located((By.ID, "homeTitle")))
        overflow = mobile.execute_script(
            "return document.documentElement.scrollWidth-document.documentElement.clientWidth"
        )
        if overflow > 1:
            raise AssertionError(f"Mobile page overflows horizontally by {overflow}px")
        mobile.find_element(By.CSS_SELECTOR, "[data-open-login]").click()
        email_input = mobile.find_element(By.CSS_SELECTOR, '#loginForm input[name="email"]')
        font_size = mobile.execute_script(
            "return parseFloat(getComputedStyle(arguments[0]).fontSize)", email_input
        )
        if font_size < 16:
            raise AssertionError(f"Mobile input font size is only {font_size}px")
        mobile.save_screenshot(str(args.output / "login-mobile.png"))
        assert_no_errors(mobile)
    finally:
        mobile.quit()

    print("Desktop and mobile UI smoke tests: OK")


if __name__ == "__main__":
    main()
