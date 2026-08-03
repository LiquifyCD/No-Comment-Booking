"""Run an optional local desktop/mobile UI smoke test with Microsoft Edge."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from selenium import webdriver
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--exercise-admin",
        action="store_true",
        help="Create, edit, and delete a temporary account. Use only against a test instance.",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    desktop = browser(1440, 1000)
    try:
        desktop.get(args.base_url)
        wait = WebDriverWait(desktop, 15)
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
        desktop.find_element(By.CSS_SELECTOR, "#loginForm button[type=submit]").click()
        wait.until(conditions.visibility_of_element_located((By.ID, "appView")))
        desktop.find_element(By.ID, "adminNav").click()
        wait.until(conditions.visibility_of_element_located((By.ID, "userList")))
        wait.until(lambda page: page.find_elements(By.CSS_SELECTOR, ".user-card"))
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
            desktop.find_element(By.CSS_SELECTOR, "#userForm button[type=submit]").click()
            wait.until(conditions.visibility_of_element_located((By.ID, "resetLinkDialog")))
            if "?reset=" not in desktop.find_element(By.ID, "resetLink").get_attribute("value"):
                raise AssertionError("Admin create did not produce a password reset link")
            desktop.find_element(By.CSS_SELECTOR, "[data-close-reset-dialog]").click()

            search = desktop.find_element(By.ID, "userSearch")
            search.send_keys(email)
            desktop.find_element(By.CSS_SELECTOR, "#userSearchForm button[type=submit]").click()
            card = wait.until(
                conditions.visibility_of_element_located((By.CSS_SELECTOR, ".user-card"))
            )
            if email not in card.text:
                raise AssertionError("Admin search returned the wrong account")
            card.find_element(By.TAG_NAME, "button").click()
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
            desktop.find_element(By.CSS_SELECTOR, "#userForm button[type=submit]").click()
            wait.until(conditions.invisibility_of_element_located((By.ID, "userDialog")))
            card = wait.until(
                conditions.visibility_of_element_located((By.CSS_SELECTOR, ".user-card"))
            )
            if "Edited UI User" not in card.text or "Betald" not in card.text:
                raise AssertionError("Admin edit was not reflected in the user list")
            card.find_element(By.TAG_NAME, "button").click()
            wait.until(conditions.visibility_of_element_located((By.ID, "userDialog")))
            desktop.find_element(By.ID, "deleteUser").click()
            wait.until(conditions.alert_is_present()).accept()
            wait.until(conditions.invisibility_of_element_located((By.CSS_SELECTOR, ".user-card")))
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
