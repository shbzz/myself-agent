"""Read-only, allowlisted HTTPS tools with DNS pinning and bounded browser rendering."""

import ipaddress
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx2 as httpx
from langchain_core.tools import ToolException

from myself_agent.general_settings import web_hostname

MAX_BODY = 1024 * 1024
MAX_TEXT = 12000


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ToolException("网页工具已达到本次执行的时间限制")
    return value


@dataclass
class WebPage:
    url: str
    content_type: str
    body: bytes


class PageText(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.title, self.text, self.links = url, [], [], []
        self.hidden, self.in_title = 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "template", "svg"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "a" and len(self.links) < 30:
            href = dict(attrs).get("href", "")
            try:
                target = urljoin(self.url, href)
                parsed = urlsplit(target)
            except ValueError:
                return
            if parsed.scheme == "https" and not parsed.username and not parsed.password:
                self.links.append({"url": target[:2048]})

    def handle_endtag(self, tag):
        if tag in ("script", "style", "template", "svg"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, value):
        if self.in_title:
            self.title.append(value)
        elif not self.hidden:
            self.text.append(value)

    def result(self):
        text = re.sub(r"\s+", " ", " ".join(self.text))
        return {
            "source_url": self.url,
            "title": " ".join(self.title)[:256],
            "untrusted_content": text[:MAX_TEXT],
            "truncated": len(text) > MAX_TEXT,
            "links": self.links,
        }


class WebAccess:
    def __init__(self, policy, proxy):
        self.policy, self.proxy = policy, proxy
        self.requests = 0
        self.addresses = {}
        self.lock = threading.Lock()

    def validate(self, url):
        try:
            parsed = urlsplit(url)
            if (
                len(url) > 4096
                or any(ord(c) < 32 for c in url)
                or parsed.scheme != "https"
                or parsed.port not in (None, 443)
                or parsed.username
                or parsed.password
                or not parsed.hostname
            ):
                raise ValueError
            host = web_hostname(parsed.hostname)
        except ValueError:
            raise ToolException("网页工具仅支持标准 HTTPS 公网域名地址") from None
        if not self.policy.get("web_enabled") or host not in self.policy.get(
            "allowed_web_hosts", []
        ):
            raise ToolException("此域名未获授权，请在工具设置中启用并加入精确域名白名单")
        return host

    def resolve(self, host, deadline):
        if host not in self.addresses:
            script = (
                "import socket,json,sys; print(json.dumps(sorted(set(x[4][0] "
                "for x in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)))))"
            )
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script, host],
                    capture_output=True,
                    text=True,
                    timeout=min(3, remaining(deadline)),
                    check=True,
                )
                addresses = [ipaddress.ip_address(value) for value in json.loads(result.stdout)]
            except (OSError, ValueError, subprocess.SubprocessError):
                raise ToolException("域名解析失败或超时") from None
            if not addresses or any(
                not address.is_global or address.is_multicast for address in addresses
            ):
                raise ToolException("禁止网页工具访问内网、回环、链路本地或保留地址")
            self.addresses[host] = str(sorted(addresses, key=lambda a: a.version)[0])
        return self.addresses[host]

    def exchange(self, url, host, address, deadline):
        target = httpx.URL(url).copy_with(host=address)
        try:
            with httpx.Client(
                proxy=self.proxy,
                trust_env=False,
                follow_redirects=False,
                timeout=min(8, remaining(deadline)),
            ) as client:
                with client.stream(
                    "GET",
                    target,
                    headers={
                        "Host": host,
                        "Accept-Encoding": "identity",
                        "User-Agent": "MyselfAgent-ReadOnly/1.0",
                    },
                    extensions={"sni_hostname": host},
                ) as response:
                    headers = dict(response.headers)
                    if response.status_code in (301, 302, 303, 307, 308):
                        return response.status_code, headers, b""
                    if headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise ToolException("网页返回了不支持的压缩内容")
                    data = bytearray()
                    for chunk in response.iter_raw(chunk_size=16384):
                        remaining(deadline)
                        data.extend(chunk)
                        if len(data) > MAX_BODY:
                            raise ToolException("单次网页或资源超过 1 MiB，已停止读取")
                    return response.status_code, headers, bytes(data)
        except httpx.HTTPError:
            raise ToolException("网页连接失败，请检查网络代理或网站可用性") from None

    def fetch(self, url, deadline):
        for _ in range(4):
            host = self.validate(url)
            with self.lock:
                if self.requests >= self.policy.get("max_web_requests", 20):
                    raise ToolException("本轮网页请求额度已耗尽（浏览器资源也计入）")
                self.requests += 1
            address = self.resolve(host, deadline)
            status, headers, body = self.exchange(url, host, address, deadline)
            if status in (301, 302, 303, 307, 308):
                url = urljoin(url, headers.get("location", ""))
                continue
            if status < 200 or status >= 300:
                raise ToolException(f"网页返回 HTTP {status}")
            return WebPage(url, headers.get("content-type", "application/octet-stream"), body)
        raise ToolException("网页重定向过多，已停止访问")

    def read(self, url, deadline):
        page = self.fetch(url, deadline)
        kind = page.content_type.split(";", 1)[0].lower()
        content = page.body.decode("utf-8", errors="replace")
        if kind in ("text/html", "application/xhtml+xml"):
            parser = PageText(page.url)
            parser.feed(content)
            return parser.result()
        if kind.startswith("text/") or kind in ("application/json", "application/xml"):
            return {
                "source_url": page.url,
                "untrusted_content": content[:MAX_TEXT],
                "truncated": len(content) > MAX_TEXT,
                "links": [],
            }
        raise ToolException("该内容不是支持的网页或文本，请使用其他文件工具")


def browse_page(access, url, store, run_id, deadline):
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from playwright.sync_api import sync_playwright

    if not access.policy.get("browser_enabled"):
        raise ToolException("只读浏览器尚未启用")
    source = access.fetch(url, deadline)
    if source.content_type.split(";", 1)[0] not in ("text/html", "application/xhtml+xml"):
        raise ToolException("只读浏览器仅渲染 HTML 页面")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            chromium_sandbox=True,
            timeout=max(1, int(remaining(deadline) * 1000)),
            proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--host-resolver-rules=MAP * ~NOTFOUND",
            ],
        )
        try:
            context = browser.new_context(
                java_script_enabled=False,
                service_workers="block",
                accept_downloads=False,
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            first_document = True

            def route(request_route):
                nonlocal first_document
                request = request_route.request
                try:
                    if request.method != "GET" or request.resource_type not in (
                        "document",
                        "stylesheet",
                        "image",
                        "font",
                    ):
                        request_route.abort()
                        return
                    if request.resource_type == "document":
                        if not first_document or request.frame != page.main_frame:
                            request_route.abort()
                            return
                        first_document = False
                        result = source
                    else:
                        result = access.fetch(request.url, deadline)
                    request_route.fulfill(
                        status=200, content_type=result.content_type, body=result.body
                    )
                except ToolException:
                    request_route.abort()

            context.route("**/*", route)
            page.goto(
                source.url,
                wait_until="domcontentloaded",
                timeout=max(1, int(remaining(deadline) * 1000)),
            )
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=min(2500, max(1, int(remaining(deadline) * 1000)))
                )
            except BrowserTimeout:
                pass
            full_text = page.locator("body").inner_text(
                timeout=max(1, int(remaining(deadline) * 1000))
            )
            text = full_text[:MAX_TEXT]
            title = page.title()[:256]
            image = page.screenshot(
                type="png", full_page=False, timeout=max(1, int(remaining(deadline) * 1000))
            )
            session_id = store.run(run_id)["session_id"]
            item = store.create_attachment(session_id, "browser-page.png", image)
            store.event(
                run_id,
                "browser.captured",
                {"attachment_id": item["id"], "source_url": source.url, "title": title},
            )
            return {
                "source_url": source.url,
                "title": title,
                "untrusted_content": text,
                "truncated": len(full_text) > MAX_TEXT,
                "attachment_id": item["id"],
                "mode": "read-only; JavaScript disabled",
            }
        finally:
            browser.close()
