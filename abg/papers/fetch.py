"""Download a paper safely: http(s) only, public addresses only, size and time limits, no log-ins or paywalls.

``source_kind`` / ``source_ref`` come from the website form:

* ``pdf``    the file you uploaded; downloaded from the site itself (signed), not from here
* ``arxiv``  an arXiv ID like 2106.01234 or hep-th/9901001 -> https://arxiv.org/pdf/<id>
* ``doi``    10.xxxx/... -> https://doi.org/<doi>; only an open-access PDF the landing page links to is used
* ``url``    a direct PDF link, or a page that names its PDF (``citation_pdf_url``, as SSRN and most journals do)
* ``title``  searched on arXiv; used only when the top hit's title matches closely
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

import httpx

ARXIV_ID = re.compile(
    r"^(?:arxiv:)?((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)$", re.IGNORECASE
)
DOI = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
PDF_META = re.compile(
    r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)
PDF_META_REV = re.compile(
    r"""<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""", re.IGNORECASE
)
UA = "c3-mnq-bot/1 (research papers; contact: the site owner)"
MAX_REDIRECTS = 5


class FetchError(Exception):
    """A problem worth showing on the website as-is: says what failed and what to do."""


@dataclass
class Fetched:
    pdf: bytes
    url: str
    title: str | None = None


def _resolve(host: str) -> list[str]:
    return list({ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


def check_url(url: str, resolver=_resolve) -> str:
    """Return the URL if it's http(s) to a public address, else raise FetchError."""
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("Only http and https links work. Check the link, or upload the PDF.")
    if u.username or u.password:
        raise FetchError(
            "Links with a user name or password in them aren't fetched. Upload the PDF instead."
        )
    host = u.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise FetchError("That link points at a private address, so the bot won't fetch it.")
    try:
        addrs = [host] if _is_ip(host) else resolver(host)
    except (OSError, UnicodeError):
        raise FetchError(f"Couldn't find {host}. Check the link.") from None
    if not addrs:
        raise FetchError(f"Couldn't find {host}. Check the link.")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise FetchError("That link points at a private address, so the bot won't fetch it.")
    return url.strip()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def get(
    client: httpx.AsyncClient, url: str, max_bytes: int, timeout_s: float = 30.0, resolver=_resolve
) -> tuple[bytes, str, str]:
    """GET with every redirect re-checked; returns (body, final url, content-type).  Raises FetchError."""
    deadline = time.monotonic() + timeout_s
    slow = FetchError(
        f"The download took longer than {timeout_s:.0f} seconds. Try again later, or upload the PDF."
    )

    async def one(u: str):
        async with client.stream(
            "GET",
            u,
            headers={"user-agent": UA, "accept": "application/pdf,text/html;q=0.8,*/*;q=0.5"},
            follow_redirects=False,
        ) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                return None, urljoin(u, r.headers["location"]), ""
            if r.status_code in (401, 402, 403):
                raise FetchError(
                    "This link needs a log-in or a subscription. Download the PDF and upload it."
                )
            if r.status_code == 404:
                raise FetchError("Nothing is at that link (404). Check it, or upload the PDF.")
            if r.status_code >= 400:
                raise FetchError(
                    f"The site said {r.status_code}. Try again later, or upload the PDF."
                )
            too_big = FetchError(
                f"The file is over {max_bytes // 1_048_576} MB, the limit for papers."
            )
            if int(r.headers.get("content-length") or 0) > max_bytes:
                raise too_big
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise too_big
            return bytes(buf), u, r.headers.get("content-type", "")

    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, resolver)
        left = deadline - time.monotonic()
        if left <= 0:
            raise slow
        try:
            body, url, ctype = await asyncio.wait_for(one(url), left)
        except TimeoutError:
            raise slow from None
        except httpx.HTTPError as e:
            raise FetchError(
                f"Couldn't download it ({type(e).__name__}). Try again later, or upload the PDF."
            ) from None
        if body is not None:
            return body, url, ctype
    raise FetchError("Too many redirects. Upload the PDF instead.")


def is_pdf(b: bytes) -> bool:
    return b[:1024].lstrip().startswith(b"%PDF-")


def pdf_link_in(html: str, base: str) -> str | None:
    m = PDF_META.search(html) or PDF_META_REV.search(html)
    return urljoin(base, m.group(1).replace("&amp;", "&")) if m else None


async def _pdf_or_page(client, url: str, max_bytes: int, resolver, what: str) -> Fetched:
    body, final, ctype = await get(client, url, max_bytes, resolver=resolver)
    if is_pdf(body):
        return Fetched(body, final)
    if "html" in ctype or body[:200].lower().lstrip().startswith((b"<!doctype", b"<html")):
        link = pdf_link_in(body[:400_000].decode("utf-8", "replace"), final)
        if link:
            body2, final2, _ = await get(client, link, max_bytes, resolver=resolver)
            if is_pdf(body2):
                return Fetched(body2, final2)
        raise FetchError(
            f"{what} doesn't give an open PDF (it may need a log-in). Download the PDF and upload it."
        )
    raise FetchError("That link isn't a PDF. Upload the PDF instead.")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def arxiv_search(client, title: str, resolver=_resolve) -> tuple[str, str] | None:
    """(arXiv id, title) of the top hit when its title matches ``title`` closely, else None."""
    q = "https://export.arxiv.org/api/query?max_results=3&search_query=" + quote(
        f'ti:"{title.strip()[:200]}"'
    )
    body, _, _ = await get(client, q, 2_000_000, resolver=resolver)
    xml = body.decode("utf-8", "replace")
    want = set(_norm(title).split())
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL):
        t = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        i = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", entry)
        if not (t and i):
            continue
        got = set(_norm(t.group(1)).split())
        if want and len(want & got) / max(len(want | got), 1) >= 0.85:
            return i.group(1).strip(), " ".join(t.group(1).split())
    return None


async def fetch_source(
    client: httpx.AsyncClient, kind: str, ref: str, max_bytes: int, resolver=_resolve
) -> Fetched:
    """Download the PDF for a link-type paper.  Raises FetchError with a message for the website."""
    ref = (ref or "").strip()
    if kind == "arxiv":
        m = ARXIV_ID.match(ref)
        if not m:
            raise FetchError("That doesn't look like an arXiv ID (for example 2106.01234).")
        return await _pdf_or_page(
            client, f"https://arxiv.org/pdf/{m.group(1)}", max_bytes, resolver, "arXiv"
        )
    if kind == "doi":
        m = DOI.match(ref)
        if not m:
            raise FetchError("That doesn't look like a DOI (it starts with 10.).")
        return await _pdf_or_page(
            client, "https://doi.org/" + m.group(1), max_bytes, resolver, "The DOI's page"
        )
    if kind == "url":
        return await _pdf_or_page(client, ref, max_bytes, resolver, "That page")
    if kind == "title":
        hit = await arxiv_search(client, ref, resolver)
        if not hit:
            raise FetchError(
                "Couldn't find an open copy of that title on arXiv. Upload the PDF or paste a link."
            )
        f = await _pdf_or_page(
            client, f"https://arxiv.org/pdf/{hit[0]}", max_bytes, resolver, "arXiv"
        )
        f.title = hit[1]
        return f
    raise FetchError("Unknown kind of source.")
