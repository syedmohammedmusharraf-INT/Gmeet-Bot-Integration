import asyncio
import os
import subprocess
import sys

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

os.environ["DISPLAY"] = ":99"
os.environ["PULSE_SERVER"] = "unix:/var/run/pulse/native"

_PULSE_SOURCE   = os.getenv("PULSE_MIC_SOURCE") or "VirtualMicSource"
_PULSE_SPK_SINK = os.getenv("PULSE_SPK_SINK")   or "VirtualSpeaker"

os.environ["PULSE_SOURCE"] = _PULSE_SOURCE
os.environ["PULSE_SINK"]   = _PULSE_SPK_SINK


def _set_pulse_defaults():
    try:
        subprocess.run(["pactl", "set-default-source", _PULSE_SOURCE],
                       capture_output=True, timeout=5)
        subprocess.run(["pactl", "set-default-sink", _PULSE_SPK_SINK],
                       capture_output=True, timeout=5)
    except Exception as e:
        print(f"[AUDIO] Could not set PulseAudio defaults: {e}", flush=True)


async def _click_use_microphone(page) -> bool:
    try:
        result = await page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt.includes('microphone') &&
                        !txt.includes('camera') &&
                        !txt.includes('without')) {
                        btn.click();
                        return btn.textContent.trim();
                    }
                }
                return null;
            }
        """)
        if result:
            print(f"[MIC]  Clicked: '{result}'", flush=True)
            return True
    except Exception as e:
        print(f"[MIC]  Error: {e}", flush=True)
    return False


async def _click_join_button(page) -> bool:
    try:
        result = await page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt.includes('without')) continue;
                    if (txt.includes('leave call') || txt.includes('call_end')) continue;
                    if (txt === 'ask to join') { btn.click(); return 'Ask to join'; }
                    if (txt === 'join now')    { btn.click(); return 'Join now'; }
                }
                return null;
            }
        """)
        if result:
            print(f"[JOIN] Clicked: '{result}'", flush=True)
            return True
    except Exception as e:
        print(f"[JOIN] Error: {e}", flush=True)
    return False


async def _click_switch_here(page) -> bool:
    try:
        result = await page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt.includes('switch here') || txt.includes('switch the call here')) {
                        btn.click();
                        return btn.textContent.trim();
                    }
                }
                return null;
            }
        """)
        if result:
            print(f"[JOIN] Clicked: '{result}'", flush=True)
            return True
    except Exception as e:
        print(f"[JOIN] Switch click error: {e}", flush=True)
    return False


async def _dismiss_popups(page):
    try:
        await page.evaluate("""
            () => {
                for (const sel of [
                    '[aria-label="Close dialog"]',
                    '[aria-label="Close"]',
                    'button[aria-label="Dismiss"]'
                ]) {
                    const btn = document.querySelector(sel);
                    if (btn) btn.click();
                }
            }
        """)
    except Exception:
        pass


async def _get_mic_state(page) -> str:
    try:
        return await page.evaluate("""
            () => {
                if (document.querySelector('[aria-label*="Turn off microphone"]')) return 'MIC_ON';
                if (document.querySelector('[aria-label*="Turn on microphone"]'))  return 'MIC_OFF';
                if (document.querySelector('[aria-label*="Microphone problem"]'))  return 'MIC_PROBLEM';
                return 'UNKNOWN';
            }
        """)
    except Exception:
        return "UNKNOWN"


async def _click_leave_button(page) -> bool:
    try:
        result = await page.evaluate("""
            () => {
                const leaveSelectors = [
                    '[aria-label="Leave call"]',
                    '[aria-label="Leave meeting"]',
                    '[data-tooltip="Leave call"]',
                ];
                for (const sel of leaveSelectors) {
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); return sel; }
                }
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (txt.includes('leave') || label.includes('leave')) {
                        btn.click();
                        return btn.getAttribute('aria-label') || btn.textContent.trim();
                    }
                }
                return null;
            }
        """)
        if result:
            print(f"[JOIN] Clicked Leave: '{result}'", flush=True)
            await page.wait_for_timeout(1500)
            return True
        else:
            print("[JOIN] Leave button not found — closing Chrome directly.", flush=True)
    except Exception as e:
        print(f"[JOIN] Leave click error: {e}", flush=True)
    return False


async def _google_login_required(page) -> bool:
    try:
        url = (page.url or "").lower()
        if "accounts.google.com" in url or "servicelogin" in url or "challenge" in url:
            return True
        if await page.locator('input[type="email"]').count() > 0:
            return True
        if await page.locator('input[type="password"]').count() > 0:
            return True
        body_text = ""
        try:
            body_text = (await page.locator("body").inner_text(timeout=3000)).lower()
        except Exception:
            pass
        hints = ["sign in", "verify", "challenge", "2-step", "two-step", "use your phone", "tap yes"]
        return any(h in body_text for h in hints)
    except Exception:
        return False


async def _wait_for_manual_login(page, timeout_seconds: int) -> None:
    if timeout_seconds <= 0:
        return
    print("\n[LOGIN] Use the VNC browser to sign in to Google manually.", flush=True)
    print(f"[LOGIN] Waiting up to {timeout_seconds}s...", flush=True)
    elapsed = 0
    while elapsed < timeout_seconds:
        if not await _google_login_required(page):
            print("[LOGIN] Login no longer required. Continuing.", flush=True)
            return
        await asyncio.sleep(5)
        elapsed += 5
        print(f"[LOGIN]   {max(timeout_seconds - elapsed, 0)}s remaining...", flush=True)
    print("[LOGIN] Login wait timeout reached. Continuing anyway.", flush=True)


async def run_meet(
    joined_event: asyncio.Event = None,
    page_holder: list = None,
):
    meeting_link      = os.getenv("MEETING_LINK", "").strip()
    stay_duration     = int(os.getenv("STAY_DURATION_SECONDS", "7200"))
    chrome_profile    = os.getenv("CHROME_USER_DATA_DIR", "/tmp/chrome-profile")
    manual_login_wait = int(os.getenv("MANUAL_LOGIN_WAIT_SECONDS", "0"))

    if not meeting_link:
        print("MEETING_LINK not set in .env", file=sys.stderr)
        sys.exit(1)

    if not meeting_link.startswith("http"):
        meeting_link = "https://" + meeting_link

    _set_pulse_defaults()

    print(f"Joining  : {meeting_link}", flush=True)
    print(f"Duration : {stay_duration}s ({stay_duration // 60} min)", flush=True)
    print(f"Mic src  : {_PULSE_SOURCE} → Chrome → Meet", flush=True)
    print(f"Spk sink : {_PULSE_SPK_SINK}", flush=True)
    print(f"Profile  : {chrome_profile}", flush=True)

    async with async_playwright() as p:
        context = None
        page = None
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=chrome_profile,
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 720},
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--ignore-certificate-errors",
                    "--alsa-input-device=pulse",
                    "--alsa-output-device=pulse",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--window-size=1280,720",
                ],
            )

            await context.grant_permissions(
                ["microphone"], origin="https://meet.google.com"
            )
            print("[PERM] Microphone pre-granted for meet.google.com", flush=True)

            page = await context.new_page()

            print("\n[JOIN] Step 1: Opening Meet", flush=True)
            try:
                await page.goto(meeting_link, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4000)
                print(f"[JOIN] Meet loaded: {page.url}", flush=True)
            except PlaywrightTimeout:
                print("[JOIN] Meet load timeout — continuing", flush=True)
            except Exception as e:
                print(f"[JOIN] Failed to open Meet: {e}", file=sys.stderr)
                sys.exit(1)

            if await _google_login_required(page):
                print("\n[LOGIN] Google sign-in detected.", flush=True)
                await _wait_for_manual_login(page, manual_login_wait)
                try:
                    await page.goto(meeting_link, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    print(f"[JOIN] Failed to reopen Meet after login: {e}", file=sys.stderr)
                    sys.exit(1)

            print("\n[JOIN] Step 2: Checking for switch prompt", flush=True)
            for attempt in range(4):
                await page.wait_for_timeout(1500)
                if await _click_switch_here(page):
                    await page.wait_for_timeout(3000)
                    break
                print(f"[JOIN] Switch prompt not visible (attempt {attempt+1}/4)", flush=True)

            print("\n[JOIN] Step 3: Pre-join mic popup", flush=True)
            for attempt in range(6):
                await page.wait_for_timeout(1500)
                if await _click_use_microphone(page):
                    await page.wait_for_timeout(2000)
                    break
                print(f"[JOIN] Mic popup not visible (attempt {attempt+1}/6)", flush=True)

            print("\n[JOIN] Step 4: Joining meeting", flush=True)
            joined = False
            for attempt in range(10):
                await page.wait_for_timeout(2000)
                if await _click_join_button(page):
                    joined = True
                    await page.wait_for_timeout(3000)
                    break
                print(f"[JOIN] Join button not found (attempt {attempt+1}/10)", flush=True)

            if not joined:
                print("[JOIN] Could not click join — may already be inside", flush=True)

            print("\n[JOIN] Step 5: Post-join mic popup", flush=True)
            for attempt in range(5):
                await page.wait_for_timeout(1500)
                if await _click_use_microphone(page):
                    await page.wait_for_timeout(2000)
                    break

            await page.wait_for_timeout(1000)
            await _dismiss_popups(page)

            print("\n[JOIN] Step 6: Verifying mic state", flush=True)
            await page.wait_for_timeout(2000)
            state = await _get_mic_state(page)
            print(f"[JOIN] Mic state: {state}", flush=True)

            if state == "MIC_OFF":
                try:
                    await page.keyboard.press("Control+d")
                    await page.wait_for_timeout(1000)
                    print("[JOIN] Toggled mic ON with Ctrl+D", flush=True)
                except Exception as e:
                    print(f"[JOIN] Ctrl+D failed: {e}", flush=True)

            final = await _get_mic_state(page)
            if final == "MIC_ON":
                print("[JOIN] MIC IS ON — bot's voice will be heard!", flush=True)
            else:
                print(f"[JOIN] Mic state: {final} — check noVNC", flush=True)

            print("\n[JOIN] Step 7: Audio routing check", flush=True)
            try:
                result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.splitlines():
                    if "Default Source" in line or "Default Sink" in line:
                        print(f"[AUDIO] {line.strip()}", flush=True)
            except Exception as e:
                print(f"[AUDIO] Could not check PulseAudio: {e}", flush=True)

            print("\nBot is in the meeting.", flush=True)
            if joined_event:
                joined_event.set()

            if page_holder is not None:
                page_holder.append(page)
                print("[JOIN] Page reference shared with vision worker.", flush=True)

            print(f"Staying for {stay_duration // 60} min.", flush=True)
            print("Bot will only leave when /stop is called from UI.\n", flush=True)

            elapsed = 0
            while elapsed < stay_duration:
                try:
                    await asyncio.sleep(5)
                    elapsed += 5
                    if page.is_closed():
                        print("[JOIN] Page closed unexpectedly.", flush=True)
                        break
                except asyncio.CancelledError:
                    print("[JOIN] Stop signal received — leaving meeting.", flush=True)
                    raise

            print(f"[JOIN] Stay duration reached ({stay_duration}s). Leaving.", flush=True)

        except asyncio.CancelledError:
            print("[JOIN] Bot stopped by user.", flush=True)
            raise
        except Exception as e:
            print(f"\nFatal error: {e}", file=sys.stderr)
            raise
        finally:
            if page and not page.is_closed():
                print("[JOIN] Clicking Leave Call...", flush=True)
                try:
                    await _click_leave_button(page)
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                    print("Browser context closed.", flush=True)
                except Exception as e:
                    print(f"Could not close browser context: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(run_meet())
