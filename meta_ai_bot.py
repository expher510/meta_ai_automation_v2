import argparse
import base64
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests
from playwright.sync_api import sync_playwright


def safe_log(message: str) -> None:
    text = str(message)
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sanitized = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(sanitized)


def unique_non_empty(items: List[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for item in items:
        if not item:
            continue
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(key)
    return output


@dataclass
class BotResult:
    success: bool
    output_type: str
    prompt: Optional[str]
    job_id: Optional[str] = None
    video_urls: List[str] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)
    text_response: Optional[str] = None
    error: Optional[str] = None

    def to_payload(self, mode_requested: str) -> dict:
        return {
            "job_id": self.job_id,
            "success": self.success,
            "prompt": self.prompt,
            "mode_requested": mode_requested,
            "output_type": self.output_type,
            "video_urls": self.video_urls,
            "video_count": len(self.video_urls),
            "image_urls": self.image_urls,
            "image_count": len(self.image_urls),
            "text_response": self.text_response,
            "error": self.error,
        }


class CookieParser:
    @staticmethod
    def parse(file_path_or_content: str) -> List[dict]:
        try:
            with open(file_path_or_content, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
        except OSError:
            content = file_path_or_content

        content = content.lstrip("\ufeff")

        json_cookies = CookieParser._try_parse_json_cookies(content)
        if json_cookies:
            return json_cookies

        decoded_candidate = CookieParser._try_decode_base64(content)
        if decoded_candidate:
            json_cookies = CookieParser._try_parse_json_cookies(decoded_candidate)
            if json_cookies:
                return json_cookies
            content = decoded_candidate

        return CookieParser._parse_netscape_cookies(content)

    @staticmethod
    def _try_decode_base64(content: str) -> Optional[str]:
        compact = "".join(content.split())
        if not compact or len(compact) % 4 != 0:
            return None
        try:
            decoded_bytes = base64.b64decode(compact, validate=True)
            return decoded_bytes.decode("utf-8-sig")
        except Exception:
            return None

    @staticmethod
    def _try_parse_json_cookies(content: str) -> Optional[List[dict]]:
        try:
            import json

            parsed = json.loads(content)
        except Exception:
            return None

        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
            return parsed["cookies"]
        return None

    @staticmethod
    def _parse_netscape_cookies(content: str) -> List[dict]:
        cookies: List[dict] = []
        for line in content.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split("\t")
            if len(parts) < 7:
                continue

            cookie = {
                "domain": parts[0],
                "path": parts[2],
                "secure": parts[3].lower() == "true",
                "name": parts[5],
                "value": parts[6],
            }

            try:
                expires = float(parts[4])
                if expires > 0:
                    cookie["expires"] = expires
            except ValueError:
                pass

            cookies.append(cookie)
        return cookies


class ResponseExtractor:
    def __init__(self, page):
        self.page = page

    def collect_video_urls(self) -> List[str]:
        urls: List[str] = []
        for video in self.page.locator("video").all():
            src = video.get_attribute("src")
            if src and src.startswith("http"):
                urls.append(src)
        return unique_non_empty(urls)

    def collect_image_urls(self) -> List[str]:
        image_urls: List[str] = []
        try:
            candidates = self.page.evaluate(
                """
                () => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    return imgs.map(img => ({
                        src: img.currentSrc || img.src || '',
                        w: img.naturalWidth || img.width || 0,
                        h: img.naturalHeight || img.height || 0
                    }));
                }
                """
            )
        except Exception:
            candidates = []

        for item in candidates:
            src = (item.get("src") or "").strip()
            width = int(item.get("w") or 0)
            height = int(item.get("h") or 0)
            if not src.startswith("http"):
                continue
            if "static.xx.fbcdn.net" in src:
                continue
            if width < 256 or height < 256:
                continue
            image_urls.append(src)

        return unique_non_empty(image_urls)

    def baseline_text_candidates(self) -> List[str]:
        return self._extract_text_candidates()

    def collect_text_response(self, baseline: List[str], prompt: str) -> Optional[str]:
        prompt_norm = self._normalize(prompt)

        markdown_answer = self._extract_markdown_answer()
        markdown_answer = self._sanitize_text(markdown_answer, prompt_norm)
        if markdown_answer:
            return markdown_answer

        baseline_set = set(unique_non_empty(baseline or []))
        for candidate in self._extract_text_candidates()[::-1]:
            if candidate in baseline_set:
                continue
            cleaned = self._sanitize_text(candidate, prompt_norm)
            if cleaned:
                return cleaned
        return None

    def _extract_markdown_answer(self) -> Optional[str]:
        try:
            text = self.page.evaluate(
                """
                () => {
                    const containers = Array.from(document.querySelectorAll('.ur-markdown, .markdown-content'));
                    if (!containers.length) return '';
                    const last = containers[containers.length - 1];
                    const pTags = Array.from(last.querySelectorAll('p'));
                    if (pTags.length) {
                        return pTags.map(p => (p.innerText || '').trim()).filter(Boolean).join('\\n').trim();
                    }
                    return (last.innerText || '').trim();
                }
                """
            )
        except Exception:
            return None
        if not text:
            return None
        return text.strip()

    def _extract_text_candidates(self) -> List[str]:
        try:
            texts = self.page.evaluate(
                """
                () => {
                    const selectors = [
                        '.markdown-content',
                        '.ur-markdown',
                        '[data-testid*="message"]',
                        '[data-testid*="response"]',
                        'main p',
                        'main div'
                    ];
                    const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
                    return nodes.map(n => (n.innerText || '').trim()).filter(Boolean);
                }
                """
            )
        except Exception:
            return []

        normalized: List[str] = []
        for text in texts:
            line = self._normalize(text)
            if len(line) < 10 or len(line) > 4000:
                continue
            normalized.append(line)
        return unique_non_empty(normalized)

    def _sanitize_text(self, text: Optional[str], prompt_norm: str) -> Optional[str]:
        if not text:
            return None

        normalized = self._normalize(text)
        if not normalized:
            return None

        blocked_patterns = [
            "Connecting apps like calendar and email",
            "Ask Meta AI...",
        ]
        for pattern in blocked_patterns:
            if pattern.lower() in normalized.lower():
                return None

        if prompt_norm:
            if normalized.lower() == prompt_norm.lower():
                return None
            if normalized.lower().startswith(prompt_norm.lower()):
                remainder = normalized[len(prompt_norm):].strip(" :-\n\t")
                if remainder.lower().startswith("today"):
                    remainder = remainder[5:].strip(" :-\n\t")
                if not remainder or len(remainder) < 8:
                    return None
                normalized = remainder

        if len(normalized) < 3:
            return None

        return normalized

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join((text or "").split()).strip()


class BaseModeHandler:
    mode_name = "base"

    def __init__(self, timeout_seconds: int, poll_seconds: int = 3):
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    def wait_for_result(self, extractor: ResponseExtractor, prompt: str, baseline: List[str], job_id: Optional[str]) -> BotResult:
        raise NotImplementedError


class TextModeHandler(BaseModeHandler):
    mode_name = "text"

    def wait_for_result(self, extractor: ResponseExtractor, prompt: str, baseline: List[str], job_id: Optional[str]) -> BotResult:
        elapsed = 0
        while elapsed < self.timeout_seconds:
            text_response = extractor.collect_text_response(baseline, prompt)
            if text_response:
                safe_log(f"Text response preview: {text_response[:160]}...")
                return BotResult(
                    success=True,
                    output_type="text",
                    prompt=prompt,
                    job_id=job_id,
                    text_response=text_response,
                )
            time.sleep(self.poll_seconds)
            elapsed += self.poll_seconds

        return BotResult(
            success=False,
            output_type="none",
            prompt=prompt,
            job_id=job_id,
            error="No text response found",
        )


class ImageModeHandler(BaseModeHandler):
    mode_name = "image"

    def wait_for_result(self, extractor: ResponseExtractor, prompt: str, baseline: List[str], job_id: Optional[str]) -> BotResult:
        elapsed = 0
        while elapsed < self.timeout_seconds:
            image_urls = extractor.collect_image_urls()
            if image_urls:
                for index, src in enumerate(image_urls, start=1):
                    safe_log(f"Image {index}: {src[:80]}...")
                safe_log(f"Total images found: {len(image_urls)}")
                return BotResult(
                    success=True,
                    output_type="image",
                    prompt=prompt,
                    job_id=job_id,
                    image_urls=image_urls,
                )
            time.sleep(self.poll_seconds)
            elapsed += self.poll_seconds

        return BotResult(
            success=False,
            output_type="none",
            prompt=prompt,
            job_id=job_id,
            error="No image URLs found",
        )


class VideoModeHandler(BaseModeHandler):
    mode_name = "video"

    def wait_for_result(self, extractor: ResponseExtractor, prompt: str, baseline: List[str], job_id: Optional[str]) -> BotResult:
        elapsed = 0
        while elapsed < self.timeout_seconds:
            video_urls = extractor.collect_video_urls()
            if video_urls:
                time.sleep(10)
                video_urls = extractor.collect_video_urls()
                for index, src in enumerate(video_urls, start=1):
                    safe_log(f"Video {index}: {src[:80]}...")
                safe_log(f"Total videos found: {len(video_urls)}")
                return BotResult(
                    success=True,
                    output_type="video",
                    prompt=prompt,
                    job_id=job_id,
                    video_urls=video_urls,
                )
            time.sleep(self.poll_seconds)
            elapsed += self.poll_seconds

        return BotResult(
            success=False,
            output_type="none",
            prompt=prompt,
            job_id=job_id,
            error="No video URLs found",
        )


class AutoModeHandler(BaseModeHandler):
    mode_name = "auto"

    def wait_for_result(self, extractor: ResponseExtractor, prompt: str, baseline: List[str], job_id: Optional[str]) -> BotResult:
        elapsed = 0
        while elapsed < self.timeout_seconds:
            video_urls = extractor.collect_video_urls()
            if video_urls:
                time.sleep(10)
                video_urls = extractor.collect_video_urls()
                for index, src in enumerate(video_urls, start=1):
                    safe_log(f"Video {index}: {src[:80]}...")
                return BotResult(
                    success=True,
                    output_type="video",
                    prompt=prompt,
                    job_id=job_id,
                    video_urls=video_urls,
                )

            image_urls = extractor.collect_image_urls()
            if image_urls:
                for index, src in enumerate(image_urls, start=1):
                    safe_log(f"Image {index}: {src[:80]}...")
                return BotResult(
                    success=True,
                    output_type="image",
                    prompt=prompt,
                    job_id=job_id,
                    image_urls=image_urls,
                )

            text_response = extractor.collect_text_response(baseline, prompt)
            if text_response:
                safe_log(f"Text response preview: {text_response[:160]}...")
                return BotResult(
                    success=True,
                    output_type="text",
                    prompt=prompt,
                    job_id=job_id,
                    text_response=text_response,
                )

            time.sleep(self.poll_seconds)
            elapsed += self.poll_seconds

        return BotResult(
            success=False,
            output_type="none",
            prompt=prompt,
            job_id=job_id,
            error="No video, image, or text response found",
        )


class WebhookClient:
    def __init__(self, webhook_url: Optional[str]):
        self.webhook_url = webhook_url

    def send(self, result: BotResult, mode_requested: str) -> None:
        if not self.webhook_url:
            safe_log("No webhook URL provided. Skipping webhook.")
            return

        payload = result.to_payload(mode_requested)
        safe_log(f"Sending webhook to {self.webhook_url}...")
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=30)
            response.raise_for_status()
            safe_log(f"Successfully sent result to webhook. HTTP Status: {response.status_code}")
        except Exception as error:
            safe_log(f"Failed to send webhook: {error}")


class MetaAIBot:
    def __init__(self, mode: str):
        self.mode = mode

    def _build_handler(self) -> BaseModeHandler:
        if self.mode == "text":
            return TextModeHandler(timeout_seconds=90)
        if self.mode == "image":
            return ImageModeHandler(timeout_seconds=180)
        if self.mode == "video":
            return VideoModeHandler(timeout_seconds=240)
        return AutoModeHandler(timeout_seconds=240)

    def run(self, prompt: Optional[str], webhook_url: Optional[str], cookies_input: str, job_id: Optional[str], test_cookies: bool) -> None:
        webhook_client = WebhookClient(webhook_url)

        with sync_playwright() as playwright:
            safe_log("Launching browser...")
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            safe_log("Parsing cookies...")
            cookies = CookieParser.parse(cookies_input)
            if cookies:
                context.add_cookies(cookies)
                safe_log(f"Loaded {len(cookies)} cookies into the browser context.")
            else:
                safe_log("WARNING: No cookies parsed. You might be asked to log in, which will fail automation.")

            page = context.new_page()

            try:
                safe_log("Navigating to https://meta.ai/ ...")
                page.goto("https://meta.ai/", timeout=60000)
                page.wait_for_load_state("networkidle")

                safe_log("Looking for the chat input box...")
                chat_input = page.get_by_role("textbox").first
                chat_input.wait_for(state="visible", timeout=15000)

                if test_cookies:
                    result = BotResult(
                        success=True,
                        output_type="cookie_test",
                        prompt="cookie_test",
                        job_id=job_id,
                        text_response="Cookie validation successful.",
                    )
                    webhook_client.send(result, mode_requested=self.mode)
                    return

                safe_log(f"Typing prompt: {prompt}")
                extractor = ResponseExtractor(page)
                baseline = extractor.baseline_text_candidates()

                chat_input.click()
                page.keyboard.type(prompt)
                page.keyboard.press("Enter")

                handler = self._build_handler()
                safe_log(f"Prompt submitted. Waiting for {handler.mode_name} result...")
                result = handler.wait_for_result(extractor, prompt, baseline, job_id)

                if not result.success:
                    try:
                        page.screenshot(path="error_screenshot.png")
                        safe_log("Saved error screenshot to error_screenshot.png")
                    except Exception:
                        pass

                webhook_client.send(result, mode_requested=self.mode)

            except Exception as error:
                safe_log(f"Error during automation: {error}")
                try:
                    page.screenshot(path="error_screenshot.png")
                    safe_log("Saved error screenshot to error_screenshot.png")
                except Exception:
                    pass

                result = BotResult(
                    success=False,
                    output_type="error",
                    prompt=prompt,
                    job_id=job_id,
                    error=str(error),
                )
                webhook_client.send(result, mode_requested=self.mode)
            finally:
                safe_log("Closing browser...")
                browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Meta AI automation bot")
    parser.add_argument("--prompt", required=False, default=None, help="Prompt to send to Meta AI")
    parser.add_argument("--webhook", required=False, default=None, help="Webhook URL to send result")
    parser.add_argument("--cookies", required=True, help="Cookies file path OR raw cookie string")
    parser.add_argument("--job-id", required=False, default=None, help="Job ID returned in webhook payload")
    parser.add_argument("--test-cookies", action="store_true", help="Validate cookies only")
    parser.add_argument(
        "--mode",
        choices=["auto", "text", "image", "video"],
        default="auto",
        help="Expected response mode; isolates logic per output type",
    )

    args = parser.parse_args()
    if not args.test_cookies and not args.prompt:
        parser.error("--prompt is required unless --test-cookies is used")

    bot = MetaAIBot(mode=args.mode)
    bot.run(
        prompt=args.prompt,
        webhook_url=args.webhook,
        cookies_input=args.cookies,
        job_id=args.job_id,
        test_cookies=args.test_cookies,
    )


if __name__ == "__main__":
    main()
