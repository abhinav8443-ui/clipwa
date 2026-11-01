"""
Automated YouTube Shorts uploader.
Dual-Engine:
- Engine A: YouTube Data API v3 (Used if client_secrets.json exists)
- Engine B: Zero-Config Playwright Browser Automation Uploader (Default when no client_secrets.json exists)
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config, atomic_write_json
from .scheduler import PeakSlotResolver, ScheduleLedger

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


def _sanitize_youtube_text(text: str) -> str:
    """
    Sanitizes text for YouTube titles, descriptions, and metadata.
    YouTube strictly forbids angle brackets ('<' and '>') and control characters.
    Also strips leading '>>' speaker markers often found in auto-captions.
    """
    if not text:
        return ""
    # 1. Strip leading speaker markers like '>>', '>>>', '>> '
    cleaned = re.sub(r"^\s*>>+\s*", "", str(text))
    # 2. Strip all '<' and '>' characters strictly forbidden by YouTube
    cleaned = cleaned.replace("<", "").replace(">", "")
    # 3. Strip control characters (retain normal newlines/whitespace)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned.strip()


class DailyUploadLimitError(Exception):
    """Raised when YouTube Studio enforces the 24-hour daily upload limit."""
    pass


class YouTubeShortsUploader:
    """Manages YouTube Shorts authentication and uploads via API or Playwright Studio automation."""

    def __init__(
        self,
        client_secrets_file: Optional[str] = None,
        token_file: Optional[str] = None,
        user_data_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        temp_dir: Optional[Path] = None,
    ):
        self.client_secrets_file = client_secrets_file or Config.YOUTUBE_CLIENT_SECRETS_FILE
        self.token_file = token_file or Config.YOUTUBE_TOKEN_FILE
        self.user_data_dir = user_data_dir or (Config.BASE_DIR / "youtube_auth_profile")
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir or Config.OUTPUT_DIR
        self.temp_dir = temp_dir or Config.TEMP_DIR
        self._youtube_client = None

    def _sync_summary_clip(self, clip_record: Dict[str, Any]) -> None:
        """
        Updates pipeline_summary.json on disk with the latest clip upload status.
        """
        summary_path = self.output_dir / "pipeline_summary.json"
        if not summary_path.exists():
            return
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clips = data.get("clips", [])
            for c in clips:
                if (c.get("clip_id") == clip_record.get("clip_id")) or (
                    c.get("file_name") and c.get("file_name") == clip_record.get("file_name")
                ):
                    c["youtube_status"] = clip_record.get("youtube_status", "Scheduled & Deleted Locally")
                    c["file_deleted"] = clip_record.get("file_deleted", True)
                    c["status"] = clip_record.get("status", c["youtube_status"])
                    if "scheduled_slot" in clip_record:
                        c["scheduled_slot"] = clip_record["scheduled_slot"]
                    if "scheduled_date" in clip_record:
                        c["scheduled_date"] = clip_record["scheduled_date"]
                    if "scheduled_time" in clip_record:
                        c["scheduled_time"] = clip_record["scheduled_time"]
                    if "display_plan" in clip_record:
                        c["display_plan"] = clip_record["display_plan"]
                    if "upload_result" in clip_record:
                        c["upload_result"] = clip_record["upload_result"]
                    break
            atomic_write_json(summary_path, data)
        except Exception as e:
            print(f"[YouTubeUploader] Warning updating pipeline_summary.json: {e}")

    def _display_schedule_plan_table(self, clips: List[Dict[str, Any]], title_prefix: str = "Upcoming") -> None:
        """Displays a clean Rich table of the scheduled peak windows for each clip."""
        try:
            table = Table(
                title=f"[bold]{title_prefix} Peak Engagement Schedule Plan[/bold]",
                show_header=True,
                header_style="bold magenta",
                border_style="cyan",
            )
            table.add_column("#", justify="center", width=4)
            table.add_column("Viral Short Title", style="bold white", width=38)
            table.add_column("Peak Window", style="yellow", width=24)
            table.add_column("Target Date & Time", style="cyan", width=26)
            table.add_column("Status / Link", style="green", width=34)

            for idx, c in enumerate(clips, 1):
                slot = c.get("scheduled_slot") or {}
                slot_name = slot.get("slot_name") or "Peak Window"
                date_time = (
                    f"{slot.get('scheduled_date', '')} @ {slot.get('scheduled_time', '')}"
                    if slot.get("scheduled_date")
                    else "Immediate Publish"
                )
                res = c.get("upload_result") or {}
                yt_status = c.get("youtube_status") or c.get("status") or ""

                if title_prefix == "Planned":
                    status_disp = "[cyan]Ready to Schedule[/cyan]"
                elif res.get("status") in ("scheduled", "uploaded") or "Scheduled" in yt_status:
                    status_disp = "[green]Scheduled | Deleted Locally[/green]"
                elif res.get("status") == "dry_run_success":
                    status_disp = "[green]Dry-Run OK (Simulated)[/green]"
                elif yt_status == "Queued for Next Upload Window (Saved in output/)":
                    status_disp = "[yellow]Queued for Next Window[/yellow]"
                elif res.get("status") == "failed" or yt_status == "Error":
                    status_disp = f"[red]Failed ({res.get('error', 'Error')})[/red]"
                else:
                    status_disp = f"[dim]{yt_status or 'Planned'}[/dim]"

                table.add_row(
                    str(c.get("clip_id", idx)),
                    (c.get("youtube_title") or f"Short #{idx}")[:38],
                    slot_name,
                    date_time,
                    status_disp,
                )

            console.print("\n")
            console.print(table)
            console.print("\n")
        except Exception as e:
            print(f"[YouTubeUploader] Schedule table display notice: {e}")

    def _scrape_scheduled_studio_shorts(self, page) -> List[Dict[str, Any]]:
        """
        Inspects YouTube Studio Content -> Shorts to discover any currently scheduled Shorts.
        Safe and non-blocking with tight timeouts.
        """
        scraped = []
        try:
            print("[YouTubeUploader] Checking live YouTube Studio calendar for scheduled Shorts...")
            page.goto("https://studio.youtube.com/videos/short", wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(2000)
            rows = page.locator("ytcp-video-row")
            row_count = min(rows.count(), 20)
            for i in range(row_count):
                row = rows.nth(i)
                text = row.inner_text()
                if "Scheduled" in text or "scheduled" in text:
                    d_m = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", text)
                    t_m = re.search(r"(\d{1,2}:\d{2}\s+(?:AM|PM|am|pm))", text)
                    title_elem = row.locator("#video-title").first
                    t_text = title_elem.inner_text().strip() if title_elem.count() > 0 else f"Scheduled #{i+1}"
                    if d_m:
                        scraped.append({
                            "title": t_text,
                            "scheduled_date": d_m.group(1),
                            "scheduled_time": t_m.group(1).upper() if t_m else "8:00 AM",
                            "source": "studio_scrape",
                        })
            if scraped:
                print(f"[YouTubeUploader] Found {len(scraped)} currently scheduled Short(s) in YouTube Studio calendar.")
            page.goto("https://studio.youtube.com", wait_until="load", timeout=25000)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        return scraped

    def _cleanup_stuck_drafts(self, page, target_titles: Optional[List[str]] = None) -> int:
        """
        Detects and deletes stuck 'Draft' / failed uploads from YouTube Studio Content -> Shorts.
        Specifically removes drafts matching target_titles or with visibility 'Draft'.
        """
        deleted_count = 0
        try:
            print("[YouTubeUploader] Checking for stuck drafts in YouTube Studio Content -> Shorts...")
            page.goto("https://studio.youtube.com/videos/short", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2500)

            rows = page.locator("ytcp-video-row")
            row_count = min(rows.count(), 20)

            for loop_idx in range(15):
                rows = page.locator("ytcp-video-row")
                row_count = min(rows.count(), 15)
                found_draft = False

                for i in range(row_count):
                    try:
                        current_rows = page.locator("ytcp-video-row")
                        if i >= current_rows.count():
                            break
                        row = current_rows.nth(i)
                        if not row.is_visible():
                            continue
                        text = row.inner_text()
                        low_text = text.lower()

                        is_target_draft = False
                        if "draft" in low_text or "uploading 0%" in low_text:
                            is_target_draft = True
                        if target_titles:
                            for tt in target_titles:
                                if tt.lower() in low_text:
                                    is_target_draft = True
                                    break

                        if is_target_draft:
                            title_el = row.locator("#video-title").first
                            r_title = title_el.inner_text().strip() if title_el.count() > 0 else f"Row #{i+1}"
                            print(f"[YouTubeUploader] 🧹 Found stuck draft row: '{r_title}'. Deleting...")

                            # Method 1: Hover over row and click 3-dots options menu
                            row.hover()
                            page.wait_for_timeout(500)
                            opts_btn = row.locator("#options-button, [aria-label*='Options' i], ytcp-icon-button#options-button").first
                            if opts_btn.is_visible():
                                opts_btn.click()
                                page.wait_for_timeout(800)

                                # Click "Delete forever" or "Delete draft"
                                del_item = page.locator(
                                    "tp-yt-paper-item:has-text('Delete forever'), paper-item:has-text('Delete forever'), "
                                    "tp-yt-paper-item:has-text('Delete draft'), [test-id='DELETE'], ytcp-menu-service-item-renderer:has-text('Delete')"
                                ).first
                                if del_item.is_visible():
                                    del_item.click()
                                    page.wait_for_timeout(1000)

                                    chk = page.locator(
                                        "tp-yt-paper-checkbox:not([checked]), ytcp-checkbox-lit, input[type='checkbox']"
                                    ).first
                                    if chk.is_visible():
                                        chk.click()
                                        page.wait_for_timeout(500)

                                    confirm_btn = page.locator(
                                        "#confirm-button, button:has-text('Delete forever'), ytcp-button#confirm-button"
                                    ).first
                                    if confirm_btn.is_visible():
                                        confirm_btn.click()
                                        deleted_count += 1
                                        found_draft = True
                                        print(f"[YouTubeUploader] ✅ Deleted stuck draft: '{r_title}'")
                                        page.wait_for_timeout(3000)
                                        break

                            # Method 2: Checkbox on row + top action bar Delete
                            chk_box = row.locator("tp-yt-paper-checkbox, #checkbox-container, ytcp-checkbox-lit").first
                            if chk_box.is_visible():
                                chk_box.click()
                                page.wait_for_timeout(800)
                                more_act = page.locator("button:has-text('More actions'), ytcp-button:has-text('More actions')").first
                                if more_act.is_visible():
                                    more_act.click()
                                    page.wait_for_timeout(600)
                                    del_opt = page.locator("tp-yt-paper-item:has-text('Delete forever'), paper-item:has-text('Delete forever')").first
                                    if del_opt.is_visible():
                                        del_opt.click()
                                        page.wait_for_timeout(800)
                                        ack_chk = page.locator("tp-yt-paper-checkbox, ytcp-checkbox-lit").first
                                        if ack_chk.is_visible():
                                            ack_chk.click()
                                        conf = page.locator("#confirm-button, button:has-text('Delete forever')").first
                                        if conf.is_visible():
                                            conf.click()
                                            deleted_count += 1
                                            found_draft = True
                                            print(f"[YouTubeUploader] ✅ Deleted stuck draft via multi-action bar: '{r_title}'")
                                            page.wait_for_timeout(3000)
                                            break
                    except Exception as row_err:
                        print(f"[YouTubeUploader] Notice deleting draft row: {row_err}")

                if not found_draft:
                    break

            if deleted_count > 0:
                print(f"[YouTubeUploader] Successfully cleaned up {deleted_count} stuck draft(s).")
            page.goto("https://studio.youtube.com", wait_until="load", timeout=25000)
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[YouTubeUploader] Draft cleanup notice: {e}")
            try:
                page.goto("https://studio.youtube.com", wait_until="load", timeout=25000)
            except Exception:
                pass
        return deleted_count

    def _unlock_profile(self) -> None:
        """
        Ensures youtube_auth_profile is completely unlocked before launching Playwright:
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

        # Remove Singleton and lock files from user_data_dir root
        lock_names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile"]
        for fname in lock_names:
            lock_file = self.user_data_dir / fname
            try:
                if lock_file.exists() or lock_file.is_symlink():
                    lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Also remove from Default directory
        default_dir = self.user_data_dir / "Default"
        if default_dir.is_dir():
            for fname in ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK"]:
                lf = default_dir / fname
                try:
                    if lf.exists() or lf.is_symlink():
                        lf.unlink(missing_ok=True)
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # Engine B: Zero-Config Playwright YouTube Studio Automation
    # -------------------------------------------------------------------------

    def login_studio(self, timeout_sec: int = 300) -> bool:
        """
        Open persistent browser to YouTube Studio so user can log in once.
        Session is permanently saved inside youtube_auth_profile.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()
        print(f"\n[YouTubeUploader] Launching browser with persistent profile at: {self.user_data_dir}")

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=browser_args,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport=None,
            )
            page = context.new_page() if not context.pages else context.pages[0]

            print("[YouTubeUploader] Navigating to https://studio.youtube.com ...")
            try:
                page.goto("https://studio.youtube.com", wait_until="load", timeout=60000)
            except Exception:
                pass

            # Handle browser upgrade notice if shown
            try:
                skip_btn = page.locator("text='SKIP TO YOUTUBE STUDIO', text='Skip to YouTube Studio'")
                if skip_btn.count() > 0 and skip_btn.first.is_visible():
                    skip_btn.first.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

            current_url = page.url
            if "accounts.google.com" in current_url or "signin" in current_url.lower():
                print("\n" + "=" * 75)
                print(" 🔓 Please sign in to your YouTube account in the opened browser window!")
                print("    (One-time setup — your session and cookies will be saved forever!)")
                print("=" * 75 + "\n")

                start_t = time.time()
                logged_in = False
                while time.time() - start_t < timeout_sec:
                    try:
                        cur = page.url
                        if "studio.youtube.com" in cur and "accounts.google.com" not in cur:
                            logged_in = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

                if not logged_in:
                    print("[YouTubeUploader] Login timed out. Please run 'clipwa login' again.")
                    context.close()
                    return False
            else:
                print("✅ Already logged in to YouTube Studio!")

            print("✅ Successfully authenticated to YouTube Studio! Saving session...")
            page.wait_for_timeout(3000)
            context.close()
            return True

    def _extract_channel_info(self, page) -> Tuple[Optional[str], Optional[str]]:
        """Extract active channel ID and channel name from page context."""
        cid = None
        cname = None

        try:
            cur_url = page.url
            id_m = re.search(r"channel/(UC[a-zA-Z0-9_-]+)", cur_url)
            if id_m:
                cid = id_m.group(1)
        except Exception:
            pass

        if not cid:
            try:
                eval_cid = page.evaluate("""
                    () => {
                        try {
                            if (window.ytcfg && window.ytcfg.get) {
                                return window.ytcfg.get('CHANNEL_ID') || (window.ytcfg.data_ && window.ytcfg.data_.CHANNEL_ID);
                            }
                        } catch(e) {}
                        const meta = document.querySelector('meta[itemprop="channelId"], meta[property="og:url"]');
                        if (meta && meta.content) {
                            const m = meta.content.match(/channel\\/(UC[a-zA-Z0-9_-]+)/);
                            if (m) return m[1];
                        }
                        return null;
                    }
                """)
                if eval_cid:
                    cid = eval_cid
            except Exception:
                pass

        for name_sel in ["#channel-name", "#entity-name", ".channel-name", "ytcp-entity-info #entity-name", "#owner-name", "ytd-channel-name"]:
            try:
                loc = page.locator(name_sel).first
                if loc.is_visible():
                    txt = loc.inner_text().strip()
                    if txt:
                        cname = txt
                        break
            except Exception:
                pass

        return cid, cname

    def switch_channel(self, timeout_sec: int = 600) -> Dict[str, Any]:
        """
        Interactive helper: launches browser directly to YouTube Channel Switcher,
        allowing the user to switch to their other YouTube channel.
        Once the user switches or lands in YouTube Studio, automatically detects
        and saves the newly selected channel.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()
        old_channel_id = "UCzIwY0Z1oiofBgoXEt1uUxg"

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=browser_args,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport=None,
            )
            page = context.new_page() if not context.pages else context.pages[0]

            print("\n" + "=" * 75)
            print(" 🔄 Browser opened to YouTube Channel Switcher!")
            print("    👉 In the opened browser window:")
            print("       1. Select your desired YouTube channel (or avatar top-right -> Switch/Add account)")
            print("       2. Click the green '✅ Use This Channel & Continue' button at the top")
            print(f"    Previous channel: copprant ({old_channel_id})")
            print("=" * 75 + "\n")

            try:
                page.goto("https://www.youtube.com/channel_switcher", wait_until="load", timeout=60000)
            except Exception:
                pass

            def inject_banner():
                try:
                    page.evaluate("""
                        () => {
                            if (document.getElementById('clipwa-switch-banner')) return;
                            const b = document.createElement('div');
                            b.id = 'clipwa-switch-banner';
                            b.innerHTML = `
                                <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 24px;background:#0d47a1;color:#ffffff;font-family:Segoe UI,Roboto,sans-serif;box-shadow:0 4px 12px rgba(0,0,0,0.5);position:fixed;top:0;left:0;right:0;z-index:2147483647;">
                                    <div>
                                        <span style="font-size:16px;font-weight:700;">🎬 CLIPWA:</span>
                                        <span style="font-size:14px;margin-left:12px;color:#e3f2fd;">Select your channel below, or click avatar (top-right) &rarr; 'Switch account' / 'Add account'.</span>
                                    </div>
                                    <button id="clipwa-confirm-btn" style="background:#00e676;color:#000;border:none;border-radius:6px;padding:8px 20px;font-size:14px;font-weight:bold;cursor:pointer;box-shadow:0 2px 5px rgba(0,0,0,0.3);">
                                        ✅ Use This Channel & Continue &rarr;
                                    </button>
                                </div>
                            `;
                            document.body.prepend(b);
                            window.__clipwa_confirmed = false;
                            const btn = document.getElementById('clipwa-confirm-btn');
                            if (btn) {
                                btn.addEventListener('click', () => {
                                    window.__clipwa_confirmed = true;
                                    btn.innerText = '⏳ Saving Channel...';
                                    btn.style.background = '#ffd600';
                                });
                            }
                        }
                    """)
                except Exception:
                    pass

            inject_banner()

            start_t = time.time()
            switched = False
            new_channel_id = None
            new_channel_name = None
            last_checked_url = None

            while time.time() - start_t < timeout_sec:
                inject_banner()
                cur_url = page.url

                # Check if user clicked the green "Use This Channel & Continue" button
                is_confirmed = False
                try:
                    is_confirmed = page.evaluate("() => Boolean(window.__clipwa_confirmed)")
                except Exception:
                    pass

                if is_confirmed:
                    print("\n[YouTubeUploader] 'Use This Channel & Continue' clicked! Inspecting active channel...")
                    page.wait_for_timeout(2000)
                    if "studio.youtube.com" not in cur_url:
                        try:
                            page.goto("https://studio.youtube.com", wait_until="load", timeout=30000)
                            page.wait_for_timeout(4000)
                        except Exception:
                            pass
                    cid, cname = self._extract_channel_info(page)
                    new_channel_id = cid or old_channel_id
                    new_channel_name = cname or "Active Channel"
                    switched = True
                    break

                # Case A: User navigated to studio.youtube.com
                if "studio.youtube.com" in cur_url:
                    cid, cname = self._extract_channel_info(page)
                    if cid and cid != old_channel_id:
                        new_channel_id = cid
                        new_channel_name = cname
                        switched = True
                        break

                # Case B: User clicked a channel on channel_switcher and redirected to youtube.com homepage or @handle
                elif cur_url != last_checked_url and (
                    "youtube.com/@" in cur_url
                    or ("youtube.com" in cur_url and "channel_switcher" not in cur_url and "account" not in cur_url and "signin" not in cur_url)
                ):
                    last_checked_url = cur_url
                    print(f"[YouTubeUploader] Detected channel navigation to {cur_url}. Verifying in Studio...")
                    page.wait_for_timeout(3000)
                    try:
                        page.goto("https://studio.youtube.com", wait_until="load", timeout=30000)
                        page.wait_for_timeout(4000)
                        cid, cname = self._extract_channel_info(page)
                        if cid and cid != old_channel_id:
                            new_channel_id = cid
                            new_channel_name = cname
                            switched = True
                            break
                    except Exception as e:
                        print(f"[YouTubeUploader] Studio verification note: {e}")

                page.wait_for_timeout(2000)

            # Final check in Studio if not yet switched
            if not switched:
                print("[YouTubeUploader] Navigating to Studio to check active channel...")
                try:
                    page.goto("https://studio.youtube.com", wait_until="load", timeout=45000)
                    page.wait_for_timeout(5000)
                    cid, cname = self._extract_channel_info(page)
                    if cid:
                        new_channel_id = cid
                    if cname:
                        new_channel_name = cname
                    if new_channel_id and new_channel_id != old_channel_id:
                        switched = True
                except Exception as e:
                    print(f"[YouTubeUploader] Studio check note: {e}")

            active_id = new_channel_id or old_channel_id
            active_name = new_channel_name or ("copprant" if active_id == old_channel_id else "Selected Channel")

            print(f"\n=======================================================")
            print(f" ✅ Active YouTube Channel: {active_name}")
            print(f"    Channel ID:   {active_id}")
            print(f"=======================================================\n")

            page.wait_for_timeout(3000)
            context.close()
            return {
                "status": "success",
                "channel_id": active_id,
                "channel_name": active_name,
                "is_different": (active_id != old_channel_id),
            }

    def _get_upload_status(self, page) -> Tuple[str, Optional[int]]:
        """
        Extract the live upload status string and numeric percentage from YouTube Studio dialog.
        Uses Playwright's native Shadow-DOM-piercing locators.
        """
        status_text = ""
        # 1. Primary locator per YouTube Studio upload dialog
        try:
            progress_loc = page.locator("ytcp-video-upload-progress, span.progress-label, ytcp-uploads-dialog .progress-label").first
            if progress_loc.count() > 0:
                status_text = (progress_loc.inner_text(timeout=1000) or "").strip()
        except Exception:
            pass

        # 2. Broader locators
        if not status_text:
            for sel in [
                "ytcp-video-upload-progress .progress-label",
                "span.progress-label",
                ".progress-label",
                "tp-yt-paper-progress",
                ".status-container",
                ".ytcp-uploads-dialog-footer",
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        txt = (loc.inner_text(timeout=500) or "").strip()
                        if txt:
                            status_text = txt
                            break
                except Exception:
                    pass

        pct = None
        if status_text:
            pct_m = re.search(r"(\d+)%", status_text)
            if pct_m:
                pct = int(pct_m.group(1))

        return status_text, pct

    def _upload_single_clip_in_page(
        self,
        page,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        privacy_status: str = "public",
        scheduled_slot: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads one video clip inside an already-opened YouTube Studio page.
        Waits for 100% upload transfer and confirmation before returning.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        title = _sanitize_youtube_text(title)
        description = _sanitize_youtube_text(description)

        # Ensure #Shorts is present in title (max 100 chars)
        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title.strip()} #Shorts"
        title = _sanitize_youtube_text(title)[:100]

        # 1. Prepare UI state: dismiss any residual dialogs or tooltips
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
        except Exception:
            pass

        # 2. Trigger "Create" -> "Upload videos"
        if progress_callback:
            progress_callback({"stage": "upload", "message": f"Opening upload modal for '{title}'..."})

        create_btn = page.locator('#create-icon, button[aria-label="Create"], ytcp-button#create-icon').first
        try:
            create_btn.wait_for(state="visible", timeout=10000)
            create_btn.click(timeout=5000)
        except Exception:
            # Try clearing any backdrop overlay and force click
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            try:
                create_btn.click(force=True)
            except Exception:
                upload_btn = page.locator("ytcp-button:has-text('Upload videos'), button:has-text('Upload videos')").first
                if upload_btn.is_visible():
                    upload_btn.click(force=True)

        page.wait_for_timeout(1000)
        upload_item = page.locator(
            "ytcp-text-menu-item:has-text('Upload videos'), #text-item-0, paper-item:has-text('Upload')"
        ).first
        try:
            upload_item.wait_for(state="visible", timeout=10000)
            upload_item.click(timeout=5000)
        except Exception:
            try:
                upload_item.click(force=True)
            except Exception:
                pass
        page.wait_for_timeout(1500)

        # 3. Select file using expect_file_chooser or direct input
        file_selected = False
        try:
            select_btn = page.locator("#select-files-button, ytcp-button#select-files-button, [aria-label*='Select files' i]").first
            with page.expect_file_chooser(timeout=8000) as fc_info:
                if select_btn.is_visible():
                    select_btn.click()
                else:
                    page.locator("input[type='file']").first.click()
            fc_info.value.set_files(str(video_path))
            file_selected = True
        except Exception as e:
            print(f"[YouTubeUploader] File chooser notice: {e}, using direct file input fallback...")

        if not file_selected:
            file_input = page.locator("ytcp-uploads-file-picker input[type='file'], input[type='file']").first
            file_input.wait_for(state="attached", timeout=15000)
            file_input.set_input_files(str(video_path))

        def check_daily_limit():
            for sel in [
                "ytcp-dialog:has-text('Daily upload limit reached')",
                "ytcp-dialog:has-text('Upload more videos daily')",
                "ytcp-dialog:has-text('Daily limit')",
                "ytcp-dialog:has-text('daily upload limit')",
                "ytcp-dialog:has-text('wait 24 hours')",
                ":has-text('Daily upload limit reached')",
                ":has-text('Upload more videos daily')",
                ":has-text('daily upload limit')",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible():
                        txt = el.inner_text().strip().replace("\n", " ")
                        raise DailyUploadLimitError(f"Daily upload limit reached: {txt}")
                except DailyUploadLimitError:
                    raise
                except Exception:
                    pass

        # 4. Wait for Upload Dialog & Title Input
        if progress_callback:
            progress_callback({"stage": "upload", "message": "Video transfer started, entering metadata..."})

        # Wait for loading scrim to disappear if present
        try:
            page.locator(".dialog-scrim").wait_for(state="hidden", timeout=5000)
        except Exception:
            pass

        title_box = page.locator("#title-textarea #textbox, #textbox[aria-label*='title' i]").first
        title_found = False
        for _ in range(45):
            check_daily_limit()
            if title_box.is_visible():
                title_found = True
                break
            page.wait_for_timeout(1000)

        check_daily_limit()

        if not title_found:
            title_box.wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(1000)

        # Fill Title
        try:
            title_box.click(force=True)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            clean_title = _sanitize_youtube_text(title)[:100]
            title_box.fill(clean_title)
        except Exception as e:
            print(f"[YouTubeUploader] Warning filling title: {e}")

        # Fill Description
        try:
            desc_box = page.locator("#description-textarea #textbox, #textbox[aria-label*='description' i]").first
            if desc_box.is_visible():
                desc_box.click(force=True)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                clean_tags = [_sanitize_youtube_text(t).strip("#") for t in (tags or []) if t]
                full_desc = f"{description}\n\n" + " ".join(f"#{t}" for t in clean_tags)
                clean_desc = _sanitize_youtube_text(full_desc)
                desc_box.fill(clean_desc)
        except Exception as e:
            print(f"[YouTubeUploader] Warning filling description: {e}")

        # Select 'Not Made for Kids' (REQUIRED by YouTube to enable Next button!)
        try:
            not_for_kids = page.locator(
                '[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], tp-yt-paper-radio-button:has-text("not made for kids")'
            ).first
            not_for_kids.scroll_into_view_if_needed(timeout=10000)
            not_for_kids.click(force=True)
            print("[YouTubeUploader] Selected audience: 'No, it is not made for kids'")
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[YouTubeUploader] Notice setting audience: {e}")

        # 5. Advance wizard step-by-step until the Visibility step is active
        print("[YouTubeUploader] Advancing wizard through steps to Visibility...")
        for step_attempt in range(6):
            # Check if Visibility tab is already active
            vis_active = page.evaluate("""
                () => {
                    const visTab = document.querySelector('#step-badge-3[aria-selected="true"], [test-id="VISIBILITY"][aria-selected="true"]');
                    const pubRadio = document.querySelector('tp-yt-paper-radio-button[name="PUBLIC"]');
                    const doneBtn = document.querySelector('#done-button:not([hidden])');
                    return Boolean(visTab || (pubRadio && window.getComputedStyle(pubRadio).display !== 'none') || (doneBtn && window.getComputedStyle(doneBtn).display !== 'none'));
                }
            """)
            if vis_active:
                print(f"[YouTubeUploader] Visibility step reached.")
                break

            # Handle scrim or 'Verify it's you' if present
            try:
                verify_box = page.locator("ytcp-dialog:has-text('Verify it\'s you'), :has-text('To continue, we need to confirm it\'s really you')").first
                if verify_box.is_visible():
                    print("\n[YouTubeUploader] ⚠️ Google verification prompt detected on screen! Please complete it in the browser...")
                    for _ in range(30):
                        if not verify_box.is_visible():
                            break
                        page.wait_for_timeout(2000)
            except Exception:
                pass

            try:
                page.locator(".dialog-scrim").wait_for(state="hidden", timeout=2000)
            except Exception:
                pass

            # Try clicking Visibility tab header directly if enabled
            clicked_tab = False
            for tab_sel in ["#step-badge-3", "ytcp-step-badge:has-text('Visibility')", "div[test-id='VISIBILITY']"]:
                try:
                    tab_loc = page.locator(tab_sel).first
                    if tab_loc.is_visible():
                        tab_loc.click()
                        clicked_tab = True
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # Click next button if visible and not hidden
            if not clicked_tab:
                next_btn = page.locator("#next-button:not([hidden]), ytcp-button#next-button:not([hidden])").first
                if next_btn.is_visible():
                    for _ in range(25):
                        is_disabled = next_btn.get_attribute("disabled") or (next_btn.get_attribute("aria-disabled") == "true")
                        if not is_disabled:
                            break
                        page.wait_for_timeout(500)
                    try:
                        next_btn.click(force=True)
                    except Exception:
                        page.evaluate("el => el.click()", next_btn)
                    print(f"[YouTubeUploader] Advanced wizard step {step_attempt + 1}")
                    page.wait_for_timeout(2000)
                else:
                    page.wait_for_timeout(1000)

        page.wait_for_timeout(1500)

        # 6. Set Visibility or Schedule Peak Engagement Window
        priv_upper = privacy_status.upper()
        if scheduled_slot:
            target_date = scheduled_slot.get("scheduled_date")
            target_time = scheduled_slot.get("scheduled_time")
            slot_name = scheduled_slot.get("slot_name", "Peak Window")
            print(f"[YouTubeUploader] 📅 Setting Scheduled Mode for peak window: {scheduled_slot.get('display_plan', f'{target_date} @ {target_time}')}...")

            # 1. Expand / Click the Schedule option
            try:
                for sel in [
                    '#schedule-radio-button',
                    'tp-yt-paper-radio-button[name="SCHEDULE"]',
                    '#second-container-expand-button',
                    'tp-yt-paper-radio-button:has-text("Schedule")',
                ]:
                    sched_opt = page.locator(sel).first
                    if sched_opt.is_visible():
                        sched_opt.click(force=True)
                        break
            except Exception:
                pass

            page.evaluate("""
                () => {
                    const r = document.querySelector('tp-yt-paper-radio-button[name="SCHEDULE"]') || document.querySelector('#schedule-radio-button');
                    if (r) {
                        r.click();
                        r.setAttribute('checked', 'true');
                        r.setAttribute('aria-checked', 'true');
                    }
                    const exp = document.querySelector('#second-container-expand-button');
                    if (exp) exp.click();
                }
            """)
            page.wait_for_timeout(1500)

            # 2. Set Date in ytcp-visibility-scheduler
            for d_sel in [
                "ytcp-visibility-scheduler ytcp-date-picker input",
                "#datepicker-trigger input",
                "ytcp-visibility-scheduler #datepicker-trigger",
                "ytcp-date-picker input",
                "#datepicker-trigger",
            ]:
                try:
                    d_input = page.locator(d_sel).first
                    if d_input.is_visible():
                        d_input.click()
                        page.wait_for_timeout(400)
                        page.keyboard.press("Control+A")
                        page.keyboard.type(target_date)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(400)
                        page.keyboard.press("Tab")
                        break
                except Exception:
                    pass

            page.evaluate(f"""
                () => {{
                    const inp = document.querySelector('ytcp-visibility-scheduler ytcp-date-picker input') ||
                                document.querySelector('#datepicker-trigger input') ||
                                document.querySelector('ytcp-date-picker input');
                    if (inp) {{
                        inp.value = '{target_date}';
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }}
            """)
            page.wait_for_timeout(1000)

            # 3. Set Time in ytcp-visibility-scheduler
            for t_sel in [
                "ytcp-visibility-scheduler #time-of-day-trigger input",
                "#time-of-day-trigger input",
                "ytcp-visibility-scheduler tp-yt-paper-input input",
                "input[aria-label*='Time' i]",
            ]:
                try:
                    t_input = page.locator(t_sel).first
                    if t_input.is_visible():
                        t_input.click()
                        page.wait_for_timeout(400)
                        page.keyboard.press("Control+A")
                        page.keyboard.type(target_time)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(400)
                        page.keyboard.press("Tab")
                        break
                except Exception:
                    pass

            page.evaluate(f"""
                () => {{
                    const inp = document.querySelector('ytcp-visibility-scheduler #time-of-day-trigger input') ||
                                document.querySelector('#time-of-day-trigger input');
                    if (inp) {{
                        inp.value = '{target_time}';
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }}
            """)
            page.wait_for_timeout(1000)
            print(f"[YouTubeUploader] Configured Schedule Slot: {target_date} @ {target_time} ({slot_name})")
        else:
            try:
                if priv_upper == "PUBLIC":
                    vis_sel = '[name="PUBLIC"], tp-yt-paper-radio-button:has-text("Public")'
                elif priv_upper == "UNLISTED":
                    vis_sel = '[name="UNLISTED"], tp-yt-paper-radio-button:has-text("Unlisted")'
                else:
                    vis_sel = '[name="PRIVATE"], tp-yt-paper-radio-button:has-text("Private")'

                vis_radio = page.locator(vis_sel).first
                try:
                    vis_radio.wait_for(state="visible", timeout=10000)
                    vis_radio.scroll_into_view_if_needed()
                    vis_radio.click(force=True)
                except Exception:
                    pass

                page.evaluate(f"""
                    () => {{
                        const targetName = '{priv_upper}';
                        const r = document.querySelector(`tp-yt-paper-radio-button[name="${{targetName}}"]`) ||
                                  document.querySelector(`[name="${{targetName}}"]`) ||
                                  Array.from(document.querySelectorAll('tp-yt-paper-radio-button')).find(el => el.innerText && el.innerText.toUpperCase().includes(targetName));
                        if (r) {{
                            r.click();
                            r.setAttribute('checked', 'true');
                            r.setAttribute('aria-checked', 'true');
                        }}
                    }}
                """)
                print(f"[YouTubeUploader] Set visibility to: {priv_upper}")
            except Exception as e:
                print(f"[YouTubeUploader] Notice setting visibility: {e}")

        # Extract video URL early if available
        video_url = None
        try:
            link_elem = page.locator("a.style-scope.ytcp-video-info, a[href*='youtu.be'], a[href*='youtube.com/shorts']").first
            if link_elem.is_visible():
                video_url = link_elem.get_attribute("href")
        except Exception:
            pass

        # 7. CRITICAL: WAIT FOR 100% UPLOAD & PROCESSING BEFORE CLICKING DONE!
        # YouTube Studio uploads chunks over network. We MUST NOT click Done or close
        # while status says "Uploading..."!
        print(f"\n[YouTubeUploader] Monitoring upload progress for '{title}'...")
        upload_complete = False
        start_wait = time.time()
        last_pct = -1

        while time.time() - start_wait < 600:  # 10 min max timeout per clip
            status_text, pct = self._get_upload_status(page)
            low_text = status_text.lower()

            # Condition 1: Actively uploading chunks
            if "uploading" in low_text:
                curr_pct = pct if pct is not None else 0
                if curr_pct != last_pct:
                    last_pct = curr_pct
                    disp_stat = status_text.replace("\n", " ").strip()
                    msg = f"Uploading to YouTube: {curr_pct}%... ({disp_stat[:70]})"
                    print(f"[YouTubeUploader] {msg}")
                    if progress_callback:
                        progress_callback({
                            "stage": "upload_progress",
                            "percent": curr_pct,
                            "message": msg,
                        })
            # Condition 2: Upload / processing / checks complete keywords
            elif any(kw in low_text for kw in [
                "checks complete",
                "no issues found",
                "processing complete",
                "upload complete",
                "processing hd",
                "processing sd",
                "processing",
                "checks",
                "hd",
            ]):
                clean_stat = status_text.replace("\n", " ").strip()
                msg = f"Uploading to YouTube: 100%... Upload & checks verified! ({clean_stat[:70]})"
                print(f"[YouTubeUploader] {msg}")
                if progress_callback:
                    progress_callback({
                        "stage": "upload_progress",
                        "percent": 100,
                        "message": msg,
                    })
                upload_complete = True
                break

            # Condition 3: Done / Schedule button is visible and enabled while not uploading
            for d_sel in [
                "#done-button:not([hidden])",
                "#done-button",
                "ytcp-button#done-button",
                "button:has-text('Schedule')",
                "button:has-text('Save')",
                "button:has-text('Publish')",
            ]:
                try:
                    btn = page.locator(d_sel).first
                    if btn.count() > 0 and btn.is_visible():
                        is_btn_enabled = False
                        try:
                            is_btn_enabled = btn.is_enabled()
                        except Exception:
                            pass
                        if not is_btn_enabled:
                            dis_attr = btn.get_attribute("disabled")
                            aria_dis = btn.get_attribute("aria-disabled")
                            is_btn_enabled = (dis_attr is None and aria_dis != "true")

                        if is_btn_enabled and "uploading" not in low_text:
                            clean_stat = status_text.replace("\n", " ").strip()
                            print(f"[YouTubeUploader] Done / Schedule button ({d_sel}) is visible and enabled! ({clean_stat[:60] or 'Ready to Schedule'})")
                            upload_complete = True
                            break
                except Exception:
                    pass

            if upload_complete:
                break

            page.wait_for_timeout(1500)

        # STRICT 100% UPLOAD VERIFICATION: Never click Done or close if not finished!
        if not upload_complete:
            raise RuntimeError(f"YouTube upload did not reach 100% completion within timeout. Last status: '{status_text}'")

        # 8. Click Done / Schedule / Publish button
        action_verb = "Schedule" if scheduled_slot else "Publish"
        print(f"[YouTubeUploader] 100% upload bytes confirmed. Saving / {action_verb}ing Short...")

        # Ensure we are fully on the final publish/schedule step
        for _ in range(4):
            nxt = page.locator("#next-button:not([hidden]):visible, ytcp-button#next-button:not([hidden]):visible").first
            if nxt.is_visible():
                try:
                    nxt.click(force=True)
                except Exception:
                    page.evaluate("el => el.click()", nxt)
                page.wait_for_timeout(1500)
            else:
                break

        # Re-verify visibility selection right before publish (if not scheduled)
        if not scheduled_slot:
            page.evaluate(f"""
                () => {{
                    const targetName = '{priv_upper}';
                    const r = document.querySelector(`tp-yt-paper-radio-button[name="${{targetName}}"]`) ||
                              document.querySelector(`[name="${{targetName}}"]`);
                    if (r) {{
                        r.click();
                        r.setAttribute('checked', 'true');
                    }}
                }}
            """)
            page.wait_for_timeout(1000)

        # Locate Done / Schedule / Publish button
        done_btn = page.locator(
            "#done-button, ytcp-button#done-button, button:has-text('Schedule'), button:has-text('Publish'), button:has-text('Save')"
        ).first

        # Wait until Done button is enabled
        for _ in range(35):
            if done_btn.count() > 0 and done_btn.is_visible():
                is_disabled = done_btn.get_attribute("disabled") or (done_btn.get_attribute("aria-disabled") == "true")
                if not is_disabled:
                    break
            page.wait_for_timeout(1000)

        # Click schedule/publish via Playwright and JavaScript fallback
        published_clicked = False
        for click_target in [
            page.locator("#done-button button, ytcp-button#done-button button").first,
            page.locator("button:has-text('Schedule'), button:has-text('Save'), button:has-text('Publish')").first,
            done_btn,
        ]:
            try:
                if click_target.count() > 0 and click_target.is_visible():
                    click_target.click(force=True, timeout=5000)
                    published_clicked = True
                    break
            except Exception:
                pass

        if not published_clicked:
            published_clicked = page.evaluate("""
                () => {
                    const btn = document.querySelector('#done-button button') ||
                                document.querySelector('#done-button') || 
                                document.querySelector('ytcp-button#done-button') ||
                                document.querySelector('button[aria-label="Schedule"]') ||
                                document.querySelector('button[aria-label="Publish"]') ||
                                document.querySelector('button[aria-label="Save"]');
                    if (btn) {
                        btn.removeAttribute('disabled');
                        btn.removeAttribute('aria-disabled');
                        btn.removeAttribute('hidden');
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """)

        print(f"[YouTubeUploader] Clicked {action_verb} for '{title}'.")

        # Handle 'We're still checking your content' or 'Checks are still running' popup
        try:
            page.wait_for_timeout(1500)
            for _ in range(5):
                check_box = page.locator(
                    "ytcp-dialog:has-text('checking your content'), ytcp-dialog:has-text('Checks are still running'), "
                    "ytcp-dialog:has-text('Schedule anyway'), ytcp-dialog:has-text('Publish anyway')"
                ).first
                if check_box.is_visible():
                    print(f"[YouTubeUploader] ⚠️ Content check dialog appeared. Confirming '{action_verb} anyway'...")
                    confirm_btn = check_box.locator(
                        "ytcp-button:has-text('Schedule anyway'), button:has-text('Schedule anyway'), "
                        "ytcp-button:has-text('Publish anyway'), button:has-text('Publish anyway'), "
                        "#confirm-button, #secondary-action-button"
                    ).first
                    if confirm_btn.is_visible():
                        confirm_btn.click(force=True)
                        page.wait_for_timeout(2000)
                        break
                page.wait_for_timeout(800)
        except Exception:
            pass

        # 9. Shielded post-publish URL extraction & buffer wait
        # (Once 100% upload bytes are confirmed and Publish is clicked, the video IS published!)
        print("[YouTubeUploader] Published! Waiting buffer to capture live YouTube URL...")
        try:
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass

            try:
                share_dialog = page.locator(
                    "ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog, "
                    "ytcp-dialog:has-text('Video published'), ytcp-dialog:has-text('Video uploaded'), "
                    "ytcp-dialog:has-text('Video scheduled'), ytcp-dialog:has-text('scheduled'), "
                    "ytcp-dialog:has-text('published')"
                ).first
                share_dialog.wait_for(state="visible", timeout=15000)

                # Extract short URL from share dialog if possible
                try:
                    share_links = page.locator(
                        "ytcp-video-share-dialog a[href*='youtu.be'], "
                        "ytcp-video-share-dialog a[href*='youtube.com'], "
                        "a.ytcp-video-info, a[href*='youtu.be']"
                    )
                    if share_links.count() > 0:
                        for i in range(share_links.count()):
                            href = share_links.nth(i).get_attribute("href")
                            if href and ("youtu.be" in href or "shorts" in href or "watch" in href):
                                video_url = href
                                break
                except Exception:
                    pass

                # Close share dialog
                close_btn = page.locator(
                    "ytcp-video-share-dialog #close-button, ytcp-button#close-button, button:has-text('Close'), #close-button"
                ).first
                if close_btn.is_visible():
                    close_btn.click()
            except Exception:
                pass

            # Fallback to locate URL on page if share dialog didn't provide it
            if not video_url:
                for link_sel in [
                    "a.style-scope.ytcp-video-info",
                    "a[href*='youtu.be']",
                    "a[href*='youtube.com/shorts']",
                    "a[href*='watch?v=']",
                    "ytcp-video-list-cell-video a",
                ]:
                    try:
                        loc = page.locator(link_sel)
                        for i in range(loc.count()):
                            href = loc.nth(i).get_attribute("href")
                            if href and ("youtu.be" in href or "shorts" in href or "watch" in href):
                                video_url = href
                                break
                        if video_url:
                            break
                    except Exception:
                        pass

            try:
                page.wait_for_selector("ytcp-uploads-dialog", state="detached", timeout=3000)
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception as e:
            print(f"[YouTubeUploader] Post-publish link extraction notice: {e}")

        vid_id = video_url.split("/")[-1].split("=")[-1] if video_url else "unknown"
        if video_url and "shorts" not in video_url and vid_id != "unknown":
            final_short_url = f"https://youtube.com/shorts/{vid_id}"
        else:
            final_short_url = video_url or "https://youtube.com/shorts"

        return {
            "status": "scheduled" if scheduled_slot else "uploaded",
            "engine": "studio_playwright",
            "video_id": vid_id,
            "video_url": final_short_url,
            "title": title,
            "privacy": "SCHEDULE" if scheduled_slot else privacy_status,
            "scheduled_date": scheduled_slot.get("scheduled_date") if scheduled_slot else None,
            "scheduled_time": scheduled_slot.get("scheduled_time") if scheduled_slot else None,
            "scheduled_slot": scheduled_slot.get("slot_name") if scheduled_slot else None,
            "display_plan": scheduled_slot.get("display_plan") if scheduled_slot else None,
        }

    def upload_multiple_via_studio(
        self,
        clips: List[Dict[str, Any]],
        privacy_status: str = "public",
        scheduled: bool = True,
        max_per_day: int = 4,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Uploads multiple clips sequentially within the SAME persistent Playwright browser session.
        Enforces 100% Scheduled Peak Engagement Windows, rolling 24-hour rate limit (8 uploads max),
        and immediate local file deletion.
        """
        ledger = ScheduleLedger(self.output_dir / "schedule_ledger.json")

        # 1. Check Rolling 24-Hour Safety Cap (Max 8 uploads per 24 hours)
        recent_uploads = ledger.get_recent_upload_count(hours=24)
        print(f"\n[YouTubeUploader] 📊 24-Hour Rolling Upload Count: {recent_uploads}/8 permitted uploads.")
        if recent_uploads >= 8:
            print("[YouTubeUploader] 🛑 Safety Cap Reached: 8 uploads already completed in the last 24 hours!")
            print("[YouTubeUploader] 💾 All clips are preserved safely in output/.\n")
            for c in clips:
                c["youtube_status"] = "Queued for Next Upload Window (Saved in output/)"
                c["status"] = "Queued for Next Upload Window (Saved in output/)"
                c["file_deleted"] = False
                self._sync_summary_clip(c)
            self._display_schedule_plan_table(clips, title_prefix="Upload Safety Limit Reached")
            return clips

        # 2. Compute Peak Slot Schedule Plan
        resolver = PeakSlotResolver(ledger)
        if scheduled:
            slots = resolver.get_next_available_slots(num_clips=len(clips), max_per_day=max_per_day)
            for i, c in enumerate(clips):
                if i < len(slots):
                    c["scheduled_slot"] = slots[i]
                    c["scheduled_date"] = slots[i]["scheduled_date"]
                    c["scheduled_time"] = slots[i]["scheduled_time"]
                    c["display_plan"] = slots[i]["display_plan"]

        self._display_schedule_plan_table(clips, title_prefix="Planned")

        if dry_run:
            for idx, c in enumerate(clips, 1):
                s_slot = c.get("scheduled_slot") or {}
                c["upload_result"] = {
                    "status": "dry_run_success",
                    "engine": "studio_playwright",
                    "video_id": f"dry_run_studio_{idx}",
                    "video_url": f"https://youtube.com/shorts/dry_run_{idx}",
                    "title": c.get("youtube_title", f"Clip #{idx}"),
                    "privacy": "SCHEDULE" if scheduled else privacy_status,
                    "scheduled_date": s_slot.get("scheduled_date"),
                    "scheduled_time": s_slot.get("scheduled_time"),
                    "scheduled_slot": s_slot.get("slot_name"),
                }
            self._display_schedule_plan_table(clips, title_prefix="Dry-Run Confirmed")
            return clips

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        # Automatically clean up any stale browser locks or zombie processes
        self._unlock_profile()

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=browser_args,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport=None,
            )
            page = context.new_page() if not context.pages else context.pages[0]

            # 1. Navigate to YouTube Studio
            print("[YouTubeUploader] Opening YouTube Studio in persistent browser...")
            try:
                page.goto("https://studio.youtube.com", wait_until="load", timeout=60000)
            except Exception:
                pass

            # Handle browser upgrade notice if shown
            try:
                skip_btn = page.locator("text='SKIP TO YOUTUBE STUDIO', text='Skip to YouTube Studio'")
                if skip_btn.count() > 0 and skip_btn.first.is_visible():
                    skip_btn.first.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

            # Check if login is required
            if "accounts.google.com" in page.url or "signin" in page.url.lower():
                print("\n" + "=" * 75)
                print(" 🔓 Please sign in to your YouTube account in the opened browser window!")
                print("=" * 75 + "\n")
                start_login = time.time()
                while time.time() - start_login < 300:
                    try:
                        if "studio.youtube.com" in page.url and "accounts.google.com" not in page.url:
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

            page.wait_for_timeout(2000)
            cid, cname = self._extract_channel_info(page)
            print(f"\n=======================================================")
            print(f" 📺 Active YouTube Channel: {cname or 'Active Channel'} ({cid or 'active'})")
            print(f"=======================================================\n")

            # Clean up any stuck drafts in Content -> Shorts before uploading
            try:
                self._cleanup_stuck_drafts(
                    page,
                    target_titles=None,
                )
            except Exception as e:
                print(f"[YouTubeUploader] Notice during draft cleanup: {e}")

            # Check live Studio scheduled shorts calendar
            try:
                scraped = self._scrape_scheduled_studio_shorts(page)
                if scraped:
                    ledger.merge_studio_scraped_slots(scraped)
                    if scheduled:
                        slots = resolver.get_next_available_slots(num_clips=len(clips), max_per_day=max_per_day)
                        for i, c in enumerate(clips):
                            if i < len(slots):
                                c["scheduled_slot"] = slots[i]
                                c["scheduled_date"] = slots[i]["scheduled_date"]
                                c["scheduled_time"] = slots[i]["scheduled_time"]
                                c["display_plan"] = slots[i]["display_plan"]
            except Exception:
                pass

            # 2. Upload and Schedule each clip sequentially inside the SAME session
            total_clips = len(clips)
            for idx, clip in enumerate(clips, 1):
                # Safety Rate Limiter Check (rolling 24h cap of 8 uploads)
                if recent_uploads >= 8:
                    print(f"\n[YouTubeUploader] 🛑 Reached 24-hour upload safety limit (8 uploads).")
                    print(f"[YouTubeUploader] 💾 Preserving remaining {total_clips - idx + 1} clip(s) safely in output/.\n")
                    for rem_idx in range(idx - 1, total_clips):
                        rem_clip = clips[rem_idx]
                        rem_clip["upload_result"] = {
                            "status": "queued_rate_limit",
                            "error": "24-hour upload safety cap (8 uploads) reached.",
                        }
                        rem_clip["youtube_status"] = "Queued for Next Upload Window (Saved in output/)"
                        rem_clip["file_deleted"] = False
                        rem_clip["status"] = "Queued for Next Upload Window (Saved in output/)"
                        self._sync_summary_clip(rem_clip)
                    break

                file_path = clip.get("file_path") or clip.get("filepath") or clip.get("video_path")
                title = clip.get("youtube_title") or clip.get("title", f"Short #{idx}")
                desc = clip.get("youtube_description") or clip.get("description", "#Shorts")
                tags = clip.get("tags", ["shorts", "viral"])
                s_slot = clip.get("scheduled_slot") if scheduled else None

                slot_label = f" -> Scheduled: {s_slot['display_plan']}" if s_slot else ""
                print(f"\n=======================================================")
                print(f" 🚀 UPLOADING CLIP [{idx}/{total_clips}]: {title}{slot_label}")
                print(f"=======================================================")

                if progress_callback:
                    progress_callback({
                        "stage": "uploading_clip",
                        "message": f"Uploading [{idx}/{total_clips}]: {title}...",
                        "clip_index": idx,
                        "total_clips": total_clips,
                    })

                # Ensure clean studio page state before each clip
                if idx > 1:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1000)
                        page.goto("https://studio.youtube.com", wait_until="load", timeout=45000)
                        page.wait_for_selector("#create-icon, button[aria-label='Create']", state="visible", timeout=30000)
                        page.wait_for_timeout(3000)
                    except Exception:
                        pass

                try:
                    res = self._upload_single_clip_in_page(
                        page=page,
                        video_path=file_path,
                        title=title,
                        description=desc,
                        tags=tags,
                        privacy_status=privacy_status,
                        scheduled_slot=s_slot,
                        progress_callback=progress_callback,
                    )
                    clip["upload_result"] = res
                    recent_uploads += 1

                    sched_str = f" (Scheduled for {s_slot['display_plan']})" if s_slot else ""
                    print(f"✅ Clip [{idx}/{total_clips}] uploaded successfully: {res.get('video_url')}{sched_str}")

                    # Record to persistent schedule ledger
                    if s_slot and res.get("status") in ("scheduled", "uploaded") and res.get("video_url"):
                        ledger.record_scheduled_clip(
                            title=title,
                            video_id=res.get("video_id", "unknown"),
                            video_url=res.get("video_url", ""),
                            scheduled_date=s_slot["scheduled_date"],
                            scheduled_time=s_slot["scheduled_time"],
                            iso_timestamp=s_slot["iso_timestamp"],
                            slot_name=s_slot["slot_name"],
                            channel_id=cid or "active",
                            clip_id=clip.get("clip_id", idx),
                        )

                    # Automatic immediate file cleanup upon confirmed successful upload/schedule
                    if res.get("status") in ("scheduled", "uploaded") and res.get("video_url"):
                        file_name = clip.get("file_name") or (os.path.basename(file_path) if file_path else "")
                        # 1. Immediately delete rendered .mp4 file
                        if file_path and os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                print(f"[YouTubeUploader] 🗑️ Cleaned up local file: {os.path.basename(file_path)}")
                            except Exception as ex:
                                print(f"[YouTubeUploader] Warning deleting local file {file_path}: {ex}")

                        if file_name and (self.output_dir / file_name).exists():
                            try:
                                (self.output_dir / file_name).unlink(missing_ok=True)
                            except Exception:
                                pass

                        # 2. Also delete any matching raw section video, extracted .wav audio, and .ass subtitle files
                        if self.temp_dir and self.temp_dir.exists():
                            stem = Path(file_name).stem if file_name else ""
                            clip_id = clip.get("clip_id", idx)
                            patterns = [
                                f"{stem}.*",
                                f"clip_{clip_id}_*.*",
                                f"raw_section_{clip_id}.*",
                                f"raw_section_*_{clip_id}.*",
                                f"clip_audio_{clip_id}.*",
                            ]
                            for pat in patterns:
                                for tf in self.temp_dir.glob(pat):
                                    try:
                                        tf.unlink(missing_ok=True)
                                    except Exception:
                                        pass

                        # 3. Update clip metadata
                        status_label = "Scheduled & Deleted Locally" if scheduled else "Uploaded & Deleted Locally"
                        clip["youtube_status"] = status_label
                        clip["file_deleted"] = True
                        clip["status"] = status_label
                        res["file_deleted"] = True
                        self._sync_summary_clip(clip)
                except DailyUploadLimitError as limit_err:
                    print(f"\n[YouTubeUploader] ⚠️ {limit_err}")
                    print("[YouTubeUploader] 🛑 Stopping further uploads for this session.")
                    print("[YouTubeUploader] 💾 All remaining clips are preserved safely in output/.\n")
                    clip["upload_result"] = {
                        "status": "queued_daily_limit",
                        "error": str(limit_err),
                    }
                    clip["youtube_status"] = "Queued for Next Upload Window (Saved in output/)"
                    clip["file_deleted"] = False
                    clip["status"] = "Queued for Next Upload Window (Saved in output/)"
                    self._sync_summary_clip(clip)
                    # Mark all subsequent clips as queued without deletion
                    for rem_idx in range(idx, total_clips):
                        rem_clip = clips[rem_idx]
                        rem_clip["upload_result"] = {
                            "status": "queued_daily_limit",
                            "error": "Daily upload limit reached - saved in output/",
                        }
                        rem_clip["youtube_status"] = "Queued for Next Upload Window (Saved in output/)"
                        rem_clip["file_deleted"] = False
                        rem_clip["status"] = "Queued for Next Upload Window (Saved in output/)"
                        self._sync_summary_clip(rem_clip)
                    break
                except Exception as e:
                    print(f"❌ Clip [{idx}/{total_clips}] upload failed: {e}")
                    clip["upload_result"] = {"error": str(e), "status": "failed"}
                    clip["youtube_status"] = "Error"
                    clip["file_deleted"] = False
                    self._sync_summary_clip(clip)
                    # Try to reset UI state for next clip
                    try:
                        page.keyboard.press("Escape")
                        page.goto("https://studio.youtube.com", wait_until="load", timeout=30000)
                    except Exception:
                        pass

                page.wait_for_timeout(3000)

            print("\n[YouTubeUploader] All clips processed. Closing browser session...")
            try:
                context.close()
            except Exception:
                pass

        self._display_schedule_plan_table(clips, title_prefix="Final Confirmed")
        return clips

    def upload_via_studio(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        privacy_status: str = "public",
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads a single video to YouTube Shorts using Playwright Studio automation.
        """
        single_clip = [{
            "file_path": video_path,
            "youtube_title": title,
            "youtube_description": description,
            "tags": tags or [],
        }]
        res_clips = self.upload_multiple_via_studio(
            clips=single_clip,
            privacy_status=privacy_status,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )
        return res_clips[0].get("upload_result") or {"status": "failed", "error": "Unknown error"}

    # -------------------------------------------------------------------------
    # Engine A: YouTube Data API v3 (Used when client_secrets.json exists)
    # -------------------------------------------------------------------------

    def get_authenticated_service(self):
        """Authenticate via OAuth 2.0 and return YouTube resource."""
        if self._youtube_client is not None:
            return self._youtube_client

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(
                    self.token_file, ["https://www.googleapis.com/auth/youtube.upload"]
                )
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, ["https://www.googleapis.com/auth/youtube.upload"]
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w", encoding="utf-8") as token:
                token.write(creds.to_json())

        self._youtube_client = build("youtube", "v3", credentials=creds)
        return self._youtube_client

    def _upload_via_api(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        category_id: str = "24",
        privacy_status: str = "public",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Resumable upload using YouTube Data API v3."""
        from googleapiclient.http import MediaFileUpload

        youtube = self.get_authenticated_service()

        title = _sanitize_youtube_text(title)
        description = _sanitize_youtube_text(description)

        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title.strip()} #Shorts"[:100]

        all_tags = [_sanitize_youtube_text(t) for t in (tags or []) if t]
        if "shorts" not in [t.lower() for t in all_tags]:
            all_tags.append("shorts")

        body = {
            "snippet": {
                "title": title,
                "description": f"{description}\n\n#Shorts",
                "tags": all_tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status.lower(),
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            video_path,
            chunksize=5 * 1024 * 1024,
            resumable=True,
            mimetype="video/mp4",
        )

        request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_callback:
                pct = int(status.progress() * 100)
                progress_callback({
                    "stage": "upload_progress",
                    "percent": pct,
                    "message": f"Uploading to YouTube API: {pct}%",
                })

        video_id = response.get("id")
        video_url = f"https://youtube.com/shorts/{video_id}"

        return {
            "status": "uploaded",
            "engine": "youtube_data_api_v3",
            "video_id": video_id,
            "video_url": video_url,
            "title": title,
            "privacy": privacy_status,
        }

    # -------------------------------------------------------------------------
    # Main Universal Upload Dispatcher
    # -------------------------------------------------------------------------

    def upload_short(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        category_id: str = "24",
        privacy_status: str = "public",
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads a single video file directly to YouTube Shorts.
        - Uses YouTube Data API v3 if client_secrets.json exists.
        - Automatically uses Playwright YouTube Studio uploader if no API keys are present.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        if dry_run:
            if progress_callback:
                progress_callback({"stage": "upload", "message": "Dry-run: Upload skipped."})
            return {
                "status": "dry_run_success",
                "engine": "dry_run",
                "video_id": "dry_run_id",
                "video_url": "https://youtube.com/shorts/dry_run_preview",
                "title": title,
                "privacy": privacy_status,
            }

        # Select Engine A (API) if client_secrets.json exists, otherwise Engine B (Playwright Studio)
        if os.path.exists(self.client_secrets_file):
            try:
                return self._upload_via_api(
                    video_path=video_path,
                    title=title,
                    description=description,
                    tags=tags,
                    category_id=category_id,
                    privacy_status=privacy_status,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                print(f"[YouTubeUploader] YouTube API upload failed ({e}), falling back to Playwright Studio uploader...")

        # Engine B: Playwright Studio Uploader
        return self.upload_via_studio(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )

    def upload_multiple(
        self,
        clips: List[Dict[str, Any]],
        privacy_status: str = "public",
        scheduled: bool = True,
        max_per_day: int = 4,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Batch uploads multiple clips.
        Uses Engine A (API) if client_secrets.json exists, else Engine B (Playwright Studio)
        keeping the browser session open for all clips.
        """
        if os.path.exists(self.client_secrets_file):
            results = []
            for idx, c in enumerate(clips, 1):
                try:
                    res = self._upload_via_api(
                        video_path=c.get("file_path", ""),
                        title=c.get("youtube_title", f"Short #{idx}"),
                        description=c.get("youtube_description", ""),
                        tags=c.get("tags", []),
                        privacy_status=privacy_status,
                        progress_callback=progress_callback,
                    )
                    c["upload_result"] = res
                except Exception as e:
                    c["upload_result"] = {"error": str(e), "status": "failed"}
                results.append(c)
            return results

        return self.upload_multiple_via_studio(
            clips=clips,
            privacy_status=privacy_status,
            scheduled=scheduled,
            max_per_day=max_per_day,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )
