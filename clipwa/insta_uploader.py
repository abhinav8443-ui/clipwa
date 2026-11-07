"""
Automated Instagram Reels uploader using Playwright browser automation.
Uses the persistent profile directory (youtube_auth_profile) and insta_state.json
so user login sessions persist seamlessly across runs without credential re-entry.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config, atomic_write_json
from .scheduler import PeakSlotResolver, ScheduleLedger, STUDIO_UPLOAD_SAFETY_CAP
from .stealth_helpers import human_sleep, human_click, human_type, human_move_mouse_jitter
import random

from rich.console import Console
from rich.table import Table

console = Console()

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def _sanitize_insta_text(text: str) -> str:
    """Sanitizes text for Instagram caption."""
    if not text:
        return ""
    cleaned = re.sub(r"^\s*>>+\s*", "", str(text))
    cleaned = cleaned.replace("<", "").replace(">", "")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned.strip()


class DailyInstaLimitError(Exception):
    """Raised when Instagram enforces a temporary action limit or upload cap."""
    pass


class InstagramReelsUploader:
    """Manages Instagram Reels authentication and automated uploads via Playwright."""

    DESKTOP_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        user_data_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        temp_dir: Optional[Path] = None,
    ):
        self.user_data_dir = user_data_dir or (Config.BASE_DIR / "youtube_auth_profile")
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.user_data_dir / "insta_state.json"
        self.output_dir = output_dir or Config.OUTPUT_DIR
        self.temp_dir = temp_dir or Config.TEMP_DIR

    def _create_stealth_context(self, p, headless: bool = False):
        """
        Creates a stealth persistent browser context undetected by Instagram automation filters.
        Injects navigator.webdriver bypass on every frame and new tab.
        """
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-sandbox",
            "--disable-infobars",
        ]
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir.resolve()),
            headless=headless,
            args=browser_args,
            ignore_default_args=["--enable-automation"],
            user_agent=self.DESKTOP_USER_AGENT,
            viewport=None,
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return context

    def _sync_summary_clip(self, clip_record: Dict[str, Any]) -> None:
        """Updates pipeline_summary.json on disk with the latest clip Instagram status."""
        summary_path = self.output_dir / "pipeline_summary.json"
        if not summary_path.exists():
            return
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clips = data.get("clips", [])
            target_id = clip_record.get("clip_id")
            target_file = clip_record.get("file_name")

            for c in clips:
                if (target_id is not None and c.get("clip_id") == target_id) or (
                    target_file and c.get("file_name") == target_file
                ):
                    for k in [
                        "instagram_status",
                        "instagram_result",
                        "youtube_status",
                        "status",
                        "file_deleted",
                        "scheduled_slot",
                        "scheduled_date",
                        "scheduled_time",
                        "display_plan",
                    ]:
                        if k in clip_record:
                            c[k] = clip_record[k]
                    break
            atomic_write_json(summary_path, data)
        except Exception as e:
            print(f"[InstagramUploader] Warning updating summary file: {e}")

    def _unlock_profile(self) -> None:
        """
        Ensures persistent profile directory is completely unlocked before launching Playwright:
        1. Terminates any zombie chrome/chromium processes holding the user_data_dir.
        2. Deletes stale SingletonLock, SingletonCookie, SingletonSocket, and LOCK files.
        """
        profile_path_str = str(self.user_data_dir.resolve())
        if sys.platform == "win32":
            try:
                ps_script = f"""
                Get-CimInstance Win32_Process | Where-Object {{
                    ($_.Name -match 'chrome|chromium') -and ($_.CommandLine -like '*{profile_path_str}*')
                }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
                """
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
            except Exception:
                pass

        time.sleep(0.8)

        lock_names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile"]
        for fname in lock_names:
            lock_file = self.user_data_dir / fname
            try:
                if lock_file.exists() or lock_file.is_symlink():
                    lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        default_dir = self.user_data_dir / "Default"
        if default_dir.is_dir():
            for fname in ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK"]:
                lf = default_dir / fname
                try:
                    if lf.exists() or lf.is_symlink():
                        lf.unlink(missing_ok=True)
                except Exception:
                    pass

    def _dismiss_modals(self, page, auto_save_info: bool = True) -> None:
        """Dismiss common Instagram popups (Save info, Notifications, Cookies)."""
        # 1. Cookie consent
        for c_sel in [
            "button:has-text('Allow all cookies')",
            "button:has-text('Allow essential and optional cookies')",
            "button:has-text('Only allow essential cookies')",
            "button:has-text('Decline optional cookies')",
        ]:
            try:
                c_btn = page.locator(c_sel).first
                if c_btn.is_visible():
                    c_btn.click()
                    page.wait_for_timeout(600)
            except Exception:
                pass

        # 2. Save login info modal
        if auto_save_info:
            for s_sel in [
                "button:has-text('Save info')",
                "div[role='button']:has-text('Save info')",
                "button:has-text('Save Info')",
                "button:has-text('Save your login info?')",
            ]:
                try:
                    s_btn = page.locator(s_sel).first
                    if s_btn.is_visible():
                        s_btn.click()
                        page.wait_for_timeout(800)
                except Exception:
                    pass

        # 3. Notifications modal
        for n_sel in [
            "button:has-text('Not Now')",
            "button:has-text('Not now')",
            "div[role='button']:has-text('Not now')",
            "button:has-text('Cancel')",
        ]:
            try:
                n_btn = page.locator(n_sel).first
                if n_btn.is_visible():
                    n_btn.click()
                    page.wait_for_timeout(600)
            except Exception:
                pass

    def _auto_fill_credentials_if_available(self, page) -> bool:
        """Auto-fills credentials if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD are in environment."""
        ig_user = os.getenv("INSTAGRAM_USERNAME") or os.getenv("IG_USERNAME")
        ig_pass = os.getenv("INSTAGRAM_PASSWORD") or os.getenv("IG_PASSWORD")
        if not ig_user or not ig_pass:
            return False

        try:
            user_input = page.locator("input[name='username'], input[name='email'], input[aria-label*='username' i]").first
            pass_input = page.locator("input[name='password'], input[name='pass'], input[aria-label*='password' i]").first
            if user_input.is_visible() and pass_input.is_visible():
                print("[InstagramUploader] Auto-filling credentials from environment...")
                human_type(page, user_input, ig_user, clear_first=True, pause_after=0.8)
                human_type(page, pass_input, ig_pass, clear_first=True, pause_after=1.0)
                login_btn = page.locator("button[type='submit'], input[type='submit'], button:has-text('Log In'), button:has-text('Log in')").first
                if login_btn.is_visible():
                    login_btn.click()
                    print("[InstagramUploader] Clicked Log In button, awaiting session...")
                    page.wait_for_timeout(4000)
                    return True
        except Exception as e:
            print(f"[InstagramUploader] Auto-login notice: {e}")
        return False

    def is_logged_in(self, page, context=None) -> bool:
        """Check if Instagram account is currently logged in."""
        try:
            if page.locator("input[name='username'], input[name='email']").first.is_visible():
                return False
        except Exception:
            pass

        has_element = False
        for sel in [
            "svg[aria-label='New post']",
            "svg[aria-label='Create']",
            "span:has-text('Create')",
            "a[href='#']:has-text('Create')",
            "svg[aria-label='Home']",
            "svg[aria-label='Direct']",
            "svg[aria-label='Messages']",
            "a[href*='/direct/']",
            "a[href*='/explore/']",
            "a[href*='/']:has(img[alt*='profile picture'])",
        ]:
            try:
                if page.locator(sel).first.is_visible():
                    has_element = True
                    break
            except Exception:
                pass

        if context is not None:
            try:
                cookies = context.cookies("https://www.instagram.com")
                has_sessionid = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
                return has_element and has_sessionid
            except Exception:
                pass

        return has_element

    def login_insta(self, timeout_sec: int = 300) -> bool:
        """
        Open persistent browser to Instagram with stealth flags so the user can log in once.
        Ensures 5-minute timeout, auto-clicks 'Save info', verifies sessionid cookie,
        saves storage state to insta_state.json, and waits for user Enter to flush cleanly.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()
        print(f"\n[InstagramUploader] Launching browser with persistent profile at: {self.user_data_dir}")

        with sync_playwright() as p:
            context = self._create_stealth_context(p, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            print("[InstagramUploader] Navigating to https://www.instagram.com/ ...")
            try:
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
                page.bring_to_front()
            except Exception:
                pass

            page.wait_for_timeout(6000)
            self._dismiss_modals(page, auto_save_info=True)

            cookies = context.cookies("https://www.instagram.com")
            has_sessionid = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)

            if self.is_logged_in(page) and has_sessionid:
                print("[InstagramUploader] ✅ Already logged into Instagram with active sessionid!")
                context.storage_state(path=str(self.state_file))
                print(f"[InstagramUploader] 💾 Storage state saved cleanly to: {self.state_file}")
                context.close()
                return True

            # Attempt auto-login if credentials exist in env
            self._auto_fill_credentials_if_available(page)

            print("\n=======================================================")
            print(" 🔑 ACTION REQUIRED: Please log in to Instagram in the browser window.")
            print(f" Waiting up to {timeout_sec} seconds (5 minutes) for login & 2FA...")
            print(" Make sure to click 'Save info' when prompted!")
            print("=======================================================\n")

            start_t = time.time()
            logged_in = False
            last_prompt = start_t
            while time.time() - start_t < timeout_sec:
                self._dismiss_modals(page, auto_save_info=True)
                cookies = context.cookies("https://www.instagram.com")
                has_sessionid = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
                if self.is_logged_in(page) and has_sessionid:
                    logged_in = True
                    break

                if time.time() - last_prompt > 25:
                    rem = int(timeout_sec - (time.time() - start_t))
                    print(f"[InstagramUploader] Waiting for Instagram login... ({rem}s remaining)")
                    last_prompt = time.time()

                page.wait_for_timeout(2000)

            if logged_in:
                print("\n[InstagramUploader] ✅ Detected successful Instagram login with active sessionid!")
                print("[InstagramUploader] Waiting 5 seconds for session to stabilize...")
                page.wait_for_timeout(5000)
                self._dismiss_modals(page, auto_save_info=True)

                # Explicitly save storage state to insta_state.json
                context.storage_state(path=str(self.state_file))
                print(f"[InstagramUploader] 💾 Storage state saved cleanly to: {self.state_file}")

                print("\n" + "=" * 70)
                print(" ✅ Instagram login verified! Session and cookies saved.")
                print(" 👉 Press ENTER in this terminal to safely close the browser and flush session...")
                print("=" * 70)
                try:
                    input()
                except Exception:
                    time.sleep(5)

                page.wait_for_timeout(2000)
                context.close()
                return True
            else:
                print("\n[InstagramUploader] ❌ Timed out waiting for Instagram login.")
                context.close()
                return False

    def _display_schedule_plan_table(self, clips: List[Dict[str, Any]], title_prefix: str = "Upcoming") -> None:
        """Displays a clean Rich table of the scheduled peak windows for Instagram Reels."""
        try:
            table = Table(
                title=f"[bold]{title_prefix} Instagram Reels Schedule Plan[/bold]",
                show_header=True,
                header_style="bold magenta",
                border_style="magenta",
            )
            table.add_column("#", justify="center", width=4)
            table.add_column("Reel Title", style="bold white", width=38)
            table.add_column("Peak Window", style="yellow", width=24)
            table.add_column("Target Date & Time", style="cyan", width=26)
            table.add_column("Instagram Status", style="green", width=30)

            for idx, c in enumerate(clips, 1):
                slot = c.get("scheduled_slot") or {}
                slot_name = slot.get("slot_name") or "Peak Window"
                date_time = (
                    f"{slot.get('scheduled_date', '')} @ {slot.get('scheduled_time', '')}"
                    if slot.get("scheduled_date")
                    else "Immediate Share"
                )
                res = c.get("instagram_result") or {}
                ig_status = c.get("instagram_status") or ""

                if title_prefix == "Planned":
                    status_disp = "[cyan]Ready to Share[/cyan]"
                elif res.get("status") in ("scheduled", "uploaded", "shared") or "Shared" in ig_status or "Scheduled" in ig_status:
                    status_disp = "[green]Shared / Scheduled[/green]"
                elif res.get("status") == "dry_run_success":
                    status_disp = "[green]Dry-Run OK (Simulated)[/green]"
                elif ig_status == "Queued for Next Upload Window":
                    status_disp = "[yellow]Queued for Next Window[/yellow]"
                elif res.get("status") == "failed" or ig_status == "Error":
                    status_disp = f"[red]Failed ({res.get('error', 'Error')})[/red]"
                else:
                    status_disp = f"[dim]{ig_status or 'Planned'}[/dim]"

                table.add_row(
                    str(c.get("clip_id", idx)),
                    (c.get("youtube_title") or f"Reel #{idx}")[:38],
                    slot_name,
                    date_time,
                    status_disp,
                )

            console.print("\n")
            console.print(table)
            console.print("\n")
        except Exception as e:
            print(f"[InstagramUploader] Schedule table display notice: {e}")

    def _upload_single_reel_in_page(
        self,
        page,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        scheduled_slot: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads one vertical video to Instagram Reels inside an already-opened browser session.
        Enforces 9:16 aspect ratio, caption with hashtags, optional scheduling, and wait for confirmation.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        title = _sanitize_insta_text(title)
        description = _sanitize_insta_text(description)
        insta_tags = [
            "#reels",
            "#motivation",
            "#mindset",
            "#nevergiveup",
            "#discipline",
            "#viralreels",
            "#shorts",
        ]
        if tags:
            for t in tags:
                clean_t = _sanitize_insta_text(t).strip("#")
                if clean_t and f"#{clean_t}" not in insta_tags:
                    insta_tags.append(f"#{clean_t}")

        full_caption = f"{title}\n\n{description}\n\n{' '.join(insta_tags[:15])}"

        # 1. Dismiss any residual modals & ensure on home feed
        self._dismiss_modals(page, auto_save_info=True)
        page.wait_for_timeout(1000)

        # 2. Click Create ("New post")
        if progress_callback:
            progress_callback({"stage": "upload", "message": f"Opening Instagram Create modal for '{title}'..."})

        create_clicked = False
        for c_sel in [
            "svg[aria-label='New post']",
            "svg[aria-label='Create']",
            "span:has-text('Create')",
            "a[href='#']:has-text('Create')",
            "a[role='link']:has-text('Create')",
            "div[role='button']:has-text('Create')",
        ]:
            try:
                c_btn = page.locator(c_sel).first
                if c_btn.is_visible():
                    c_btn.click(force=True)
                    create_clicked = True
                    break
            except Exception:
                pass

        if not create_clicked:
            raise RuntimeError("Could not find or click Instagram 'Create' button. Ensure you are logged in.")

        page.wait_for_timeout(1500)

        # If sub-menu pops up with "Post" vs "Live video", click "Post"
        for sub_sel in [
            "span:has-text('Post')",
            "div[role='button']:has-text('Post')",
            "div[role='menuitem']:has-text('Post')",
            "a:has-text('Post')",
        ]:
            try:
                sub_item = page.locator(sub_sel).first
                if sub_item.is_visible():
                    sub_item.click(force=True)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # 3. Attach file directly onto hidden input[type='file']
        file_input = page.locator("input[type='file']").first
        file_input.wait_for(state="attached", timeout=15000)
        file_input.set_input_files(str(video_path))
        print(f"[InstagramUploader] Attached video file: {os.path.basename(video_path)}")
        page.wait_for_timeout(3500)

        # 4. Handle informational popups: "Video posts are now shared as reels"
        for ok_sel in [
            "button:has-text('OK')",
            "button:has-text('Ok')",
            "div[role='dialog'] button:has-text('OK')",
        ]:
            try:
                ok_btn = page.locator(ok_sel).first
                if ok_btn.is_visible():
                    ok_btn.click(force=True)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # 5. Aspect Ratio: Enforce 9:16 (Zero-Crop Vertical Reel)
        try:
            crop_btn = page.locator(
                "svg[aria-label='Select crop'], button:has(svg[aria-label='Select crop']), [aria-label*='crop' i], div[role='button']:has(svg[aria-label='Select crop'])"
            ).first
            if crop_btn.is_visible():
                crop_btn.click(force=True)
                page.wait_for_timeout(800)

                for r_sel in [
                    "button:has-text('9:16')",
                    "div[role='button']:has-text('9:16')",
                    "span:has-text('9:16')",
                    "button:has-text('Original')",
                    "span:has-text('Original')",
                ]:
                    try:
                        r_btn = page.locator(r_sel).first
                        if r_btn.is_visible():
                            r_btn.click(force=True)
                            print("[InstagramUploader] Enforced 9:16 vertical Reel aspect ratio.")
                            page.wait_for_timeout(800)
                            break
                    except Exception:
                        pass
        except Exception as e:
            print(f"[InstagramUploader] Aspect ratio notice: {e}")

        # 6. Click 'Next' (top right of modal) -> Filters/Cover screen
        next_btn = page.locator("div[role='button']:has-text('Next'), button:has-text('Next')").first
        next_btn.wait_for(state="visible", timeout=15000)
        next_btn.click(force=True)
        page.wait_for_timeout(2500)

        # 7. Click 'Next' again -> Details / Caption screen
        next_btn2 = page.locator("div[role='button']:has-text('Next'), button:has-text('Next')").first
        if next_btn2.is_visible():
            next_btn2.click(force=True)
            page.wait_for_timeout(2500)

        # 8. Fill Caption with stealth typing
        print(f"[InstagramUploader] Entering caption and hashtags for '{title}'...")
        caption_box = page.locator(
            "div[aria-label*='Write a caption' i], div[aria-label*='caption' i][contenteditable='true'], div[role='textbox']"
        ).first
        caption_box.wait_for(state="visible", timeout=15000)
        human_sleep(1.0, 1.5)

        try:
            human_type(page, caption_box, full_caption[:2200], clear_first=True, pause_after=2.0)
            print("[InstagramUploader] Caption and hashtags added with stealth typing.")
        except Exception as e:
            print(f"[InstagramUploader] Warning entering caption: {e}")
            try:
                caption_box.fill(full_caption[:2200])
            except Exception:
                pass

        human_sleep(1.0, 2.0)

        # 9. Smart Schedule vs. Share Handling
        is_scheduled = False
        action_verb = "Share"

        if scheduled_slot:
            target_date = scheduled_slot.get("scheduled_date")
            target_time = scheduled_slot.get("scheduled_time")
            slot_name = scheduled_slot.get("slot_name", "Peak Window")

            try:
                adv_btn = page.locator(
                    "div:has-text('Advanced settings'), span:has-text('Advanced settings')"
                ).first
                if adv_btn.is_visible():
                    adv_btn.click(force=True)
                    page.wait_for_timeout(1000)

                    sched_toggle = page.locator(
                        "input[type='checkbox'][aria-label*='Schedule' i], "
                        "div:has-text('Schedule this reel') input[type='checkbox'], "
                        "div[role='switch']:has-text('Schedule')"
                    ).first
                    if sched_toggle.is_visible():
                        sched_toggle.click(force=True)
                        page.wait_for_timeout(1000)
                        is_scheduled = True
                        action_verb = "Schedule"
                        print(f"[InstagramUploader] 📅 Configured Instagram scheduled reel for {target_date} @ {target_time} ({slot_name})")
            except Exception as e:
                print(f"[InstagramUploader] Scheduling option notice: {e}")

        # 10. Click Share / Schedule (Defaults to Share on standard web)
        print(f"[InstagramUploader] Submitting Reel via '{action_verb}' button...")
        if progress_callback:
            progress_callback({"stage": "upload_progress", "percent": 50, "message": f"{action_verb}ing Reel to Instagram..."})

        submit_btn = page.locator(
            f"div[role='button']:has-text('{action_verb}'), button:has-text('{action_verb}'), "
            "div[role='button']:has-text('Share'), button:has-text('Share')"
        ).first
        submit_btn.wait_for(state="visible", timeout=15000)
        submit_btn.click(force=True)

        # 11. Wait for confirmation ("Your reel has been shared." / checkmark)
        print("[InstagramUploader] Waiting for Instagram upload confirmation (up to 120s)...")
        upload_complete = False
        reel_url = None
        start_wait = time.time()

        while time.time() - start_wait < 120:
            for s_sel in [
                "div:has-text('Your reel has been shared.')",
                "div:has-text('Your reel has been scheduled.')",
                "div:has-text('Your post has been shared.')",
                "div:has-text('Reel shared')",
                "div:has-text('Post shared')",
                "img[alt*='checkmark' i]",
                "svg[aria-label*='Animated checkmark' i]",
            ]:
                try:
                    conf = page.locator(s_sel).first
                    if conf.is_visible():
                        upload_complete = True
                        print(f"[InstagramUploader] ✅ Confirmation detected: '{s_sel}'")
                        break
                except Exception:
                    pass

            if upload_complete:
                break

            # Check if modal closed itself indicating completion
            try:
                modal = page.locator("div[role='dialog']").first
                if not modal.is_visible() and (time.time() - start_wait > 15):
                    upload_complete = True
                    break
            except Exception:
                pass

            page.wait_for_timeout(2000)

        if not upload_complete:
            raise RuntimeError("Instagram Reel upload confirmation did not appear within 120s timeout.")

        page.wait_for_timeout(3000)

        # Extract post URL if present
        try:
            link = page.locator("a[href*='/reel/'], a[href*='/p/']").first
            if link.is_visible():
                reel_url = link.get_attribute("href")
                if reel_url and not reel_url.startswith("http"):
                    reel_url = f"https://www.instagram.com{reel_url}"
        except Exception:
            pass

        # Close any lingering dialogs
        try:
            close_btn = page.locator(
                "div[role='dialog'] svg[aria-label='Close'], div[role='dialog'] button:has-text('Close'), svg[aria-label='Close']"
            ).first
            if close_btn.is_visible():
                close_btn.click(force=True)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        final_url = reel_url or "https://www.instagram.com/reels/"
        return {
            "status": "scheduled" if is_scheduled else "shared",
            "platform": "instagram",
            "engine": "insta_playwright",
            "reel_url": final_url,
            "title": title,
            "scheduled_date": scheduled_slot.get("scheduled_date") if scheduled_slot else None,
            "scheduled_time": scheduled_slot.get("scheduled_time") if scheduled_slot else None,
            "scheduled_slot": scheduled_slot.get("slot_name") if scheduled_slot else None,
            "display_plan": scheduled_slot.get("display_plan") if scheduled_slot else None,
        }

    def upload_multiple_reels(
        self,
        clips: List[Dict[str, Any]],
        scheduled: bool = True,
        max_per_day: int = 2,
        force: bool = False,
        dry_run: bool = False,
        auto_delete: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Uploads multiple vertical clips to Instagram Reels sequentially within the SAME browser session.
        Respects rolling 24-hour rate limit (4 uploads max) and peak window scheduling.
        """
        ledger = ScheduleLedger(self.output_dir / "schedule_ledger.json")

        # 1. Check Rolling 24-Hour Safety Cap (Max 4 uploads per 24 hours)
        recent_uploads = ledger.get_recent_upload_count(hours=24, platform="instagram")
        print(f"\n[InstagramUploader] 📊 24-Hour Rolling Reel Count: {recent_uploads}/{STUDIO_UPLOAD_SAFETY_CAP} permitted uploads.")
        if not dry_run and not force and recent_uploads >= STUDIO_UPLOAD_SAFETY_CAP:
            print(f"[InstagramUploader] 🛑 Safety Cap Reached: {STUDIO_UPLOAD_SAFETY_CAP} uploads already completed in the last 24 hours!")
            print("[InstagramUploader] 💾 All clips are preserved safely in output/.\n")
            for c in clips:
                c["instagram_status"] = "Queued for Next Upload Window"
                self._sync_summary_clip(c)
            self._display_schedule_plan_table(clips, title_prefix="Upload Safety Limit Reached")
            return clips

        # 2. Compute Peak Slot Schedule Plan if not already assigned
        resolver = PeakSlotResolver(ledger)
        if scheduled:
            slots = resolver.get_next_available_slots(num_clips=len(clips), max_per_day=max_per_day)
            for i, c in enumerate(clips):
                if not c.get("scheduled_slot") and i < len(slots):
                    c["scheduled_slot"] = slots[i]
                    c["scheduled_date"] = slots[i]["scheduled_date"]
                    c["scheduled_time"] = slots[i]["scheduled_time"]
                    c["display_plan"] = slots[i]["display_plan"]

        self._display_schedule_plan_table(clips, title_prefix="Planned")

        if dry_run:
            for idx, c in enumerate(clips, 1):
                s_slot = c.get("scheduled_slot") or {}
                c["instagram_result"] = {
                    "status": "dry_run_success",
                    "platform": "instagram",
                    "engine": "insta_playwright",
                    "reel_url": f"https://www.instagram.com/reels/dry_run_{idx}",
                    "title": c.get("youtube_title", f"Reel #{idx}"),
                    "scheduled_date": s_slot.get("scheduled_date"),
                    "scheduled_time": s_slot.get("scheduled_time"),
                    "scheduled_slot": s_slot.get("slot_name"),
                }
                c["instagram_status"] = "Dry-Run OK (Simulated)"
                self._sync_summary_clip(c)
            self._display_schedule_plan_table(clips, title_prefix="Dry-Run Confirmed")
            return clips

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()

        with sync_playwright() as p:
            context = self._create_stealth_context(p, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # Check if saved state exists and load cookies
            if self.state_file.exists():
                try:
                    with open(self.state_file, "r", encoding="utf-8") as f:
                        saved_state = json.load(f)
                    if "cookies" in saved_state:
                        context.add_cookies(saved_state["cookies"])
                        print(f"[InstagramUploader] Restored session cookies from {self.state_file.name}")
                except Exception as e:
                    print(f"[InstagramUploader] Notice reading state file: {e}")

            print("[InstagramUploader] Navigating to Instagram...")
            try:
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
                page.bring_to_front()
            except Exception:
                pass

            # Wait 6 seconds for React app to hydrate
            print("[InstagramUploader] Waiting 6 seconds for Instagram app to hydrate...")
            page.wait_for_timeout(6000)
            self._dismiss_modals(page, auto_save_info=True)

            cookies = context.cookies("https://www.instagram.com")
            has_sessionid = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
            logged_in = self.is_logged_in(page) and has_sessionid

            if not logged_in and self.state_file.exists():
                try:
                    with open(self.state_file, "r", encoding="utf-8") as f:
                        saved_state = json.load(f)
                    if "cookies" in saved_state:
                        context.add_cookies(saved_state["cookies"])
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(5000)
                        self._dismiss_modals(page, auto_save_info=True)
                        cookies = context.cookies("https://www.instagram.com")
                        has_sessionid = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
                        logged_in = self.is_logged_in(page) and has_sessionid
                except Exception as e:
                    print(f"[InstagramUploader] Notice reloading state: {e}")

            if not logged_in:
                # Attempt auto-login if credentials exist in env
                self._auto_fill_credentials_if_available(page)

            if not logged_in:
                print("\n" + "=" * 75)
                print(" [InstagramUploader] ⚠️ Please log into Instagram in the open browser window")
                print(" (waiting up to 180s for login & 'Save info')...")
                print("=" * 75 + "\n")
                wait_login_start = time.time()
                while time.time() - wait_login_start < 180:
                    self._dismiss_modals(page, auto_save_info=True)
                    cookies = context.cookies("https://www.instagram.com")
                    has_sessionid = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
                    if self.is_logged_in(page) and has_sessionid:
                        logged_in = True
                        print("\n[InstagramUploader] ✅ Instagram login detected in open browser!")
                        page.wait_for_timeout(5000)
                        self._dismiss_modals(page, auto_save_info=True)
                        context.storage_state(path=str(self.state_file))
                        print(f"[InstagramUploader] 💾 Storage state updated cleanly to: {self.state_file}")
                        break
                    page.wait_for_timeout(2000)

            if not logged_in:
                context.close()
                raise RuntimeError("Instagram login required. Run 'clipwa login-insta' first.")

            total_clips = len(clips)
            for idx, clip in enumerate(clips, 1):
                file_path = clip.get("file_path") or str(self.output_dir / clip.get("file_name", ""))
                if not os.path.exists(file_path):
                    alt_path = str(self.output_dir / clip.get("file_name", ""))
                    if os.path.exists(alt_path):
                        file_path = alt_path
                    else:
                        print(f"⚠️ Clip [{idx}/{total_clips}] file not found on disk: {file_path}, skipping.")
                        continue

                title = clip.get("youtube_title") or f"Short #{idx}"
                desc = clip.get("youtube_description") or clip.get("hook_text") or ""
                tags = clip.get("tags") or []
                s_slot = clip.get("scheduled_slot")
                slot_label = f" -> Scheduled: {s_slot['display_plan']}" if s_slot else ""

                print(f"\n=======================================================")
                print(f" 📸 UPLOADING TO INSTAGRAM REELS [{idx}/{total_clips}]: {title}{slot_label}")
                print(f"=======================================================")

                if progress_callback:
                    progress_callback({
                        "stage": "uploading_insta",
                        "message": f"Uploading to Instagram [{idx}/{total_clips}]: {title}...",
                        "clip_index": idx,
                        "total_clips": total_clips,
                    })

                # Refresh home page between clips
                if idx > 1:
                    try:
                        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                        self._dismiss_modals(page, auto_save_info=True)
                    except Exception:
                        pass

                try:
                    res = self._upload_single_reel_in_page(
                        page=page,
                        video_path=file_path,
                        title=title,
                        description=desc,
                        tags=tags,
                        scheduled_slot=s_slot,
                        progress_callback=progress_callback,
                    )
                    clip["instagram_result"] = res
                    recent_uploads += 1

                    sched_str = f" (Scheduled for {s_slot['display_plan']})" if s_slot and res.get("status") == "scheduled" else ""
                    print(f"✅ Clip [{idx}/{total_clips}] Instagram Reel shared: {res.get('reel_url')}{sched_str}")

                    # Record to persistent schedule ledger
                    if s_slot and res.get("status") in ("scheduled", "shared", "uploaded"):
                        ledger.record_scheduled_clip(
                            title=title,
                            video_id=res.get("reel_url", f"insta_{idx}").split("/")[-2] or f"insta_{idx}",
                            video_url=res.get("reel_url", ""),
                            scheduled_date=s_slot["scheduled_date"],
                            scheduled_time=s_slot["scheduled_time"],
                            iso_timestamp=s_slot["iso_timestamp"],
                            slot_name=s_slot["slot_name"],
                            channel_id="instagram",
                            clip_id=clip.get("clip_id", idx),
                            platform="instagram",
                        )

                    ig_status_label = "Scheduled" if res.get("status") == "scheduled" else "Shared"
                    clip["instagram_status"] = ig_status_label

                    # Auto-delete ONLY if requested by caller (e.g. if YouTube also succeeded)
                    if auto_delete and res.get("status") in ("scheduled", "shared", "uploaded"):
                        try:
                            if os.path.exists(file_path):
                                os.remove(file_path)
                                print(f"[InstagramUploader] 🗑️ Cleaned up local file: {os.path.basename(file_path)}")
                            clip["file_deleted"] = True
                            clip["instagram_status"] = f"{ig_status_label} & Deleted Locally"
                        except Exception as ex:
                            print(f"[InstagramUploader] Warning deleting local file: {ex}")

                    self._sync_summary_clip(clip)

                except Exception as e:
                    print(f"❌ Clip [{idx}/{total_clips}] Instagram upload failed: {e}")
                    clip["instagram_result"] = {"error": str(e), "status": "failed"}
                    clip["instagram_status"] = "Error"
                    self._sync_summary_clip(clip)

                # Inter-Clip Cooldown: randomized 15-25 seconds before next Reel
                if idx < total_clips:
                    cooldown = random.uniform(15.0, 25.0)
                    print(f"\n[InstagramUploader] ⏳ Inter-clip stealth cooldown: pausing {cooldown:.1f}s before next Reel...")
                    human_move_mouse_jitter(page, count=2)
                    time.sleep(cooldown)
                else:
                    human_sleep(2.0, 3.5)

            context.close()

        self._display_schedule_plan_table(clips, title_prefix="Final Confirmed")
        return clips
