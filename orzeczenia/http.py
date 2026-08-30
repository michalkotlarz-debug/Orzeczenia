"""Klient HTTP do serwisów źródłowych.

Zasady:
* jedno zapytanie na host naraz, z minimalnym odstępem (portale mają limitery),
* krótka pamięć podręczna w RAM - żeby nie pytać dwa razy o to samo,
* rozpoznawanie stron blokady/CAPTCHA, które przychodzą z kodem 200.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import urllib.robotparser
from collections import OrderedDict
from urllib.parse import urlencode, urlparse

import httpx

log = logging.getLogger("orzecznik.http")


class SourceUnavailable(RuntimeError):
    """Serwis źródłowy nie odpowiedział albo oddał coś, co nie jest treścią."""


class RateLimited(SourceUnavailable):
    """Serwis pokazał CAPTCHA lub odrzucił zapytanie z powodu tempa."""


# orzeczenia.ms.gov.pl nie zwraca 429 - oddaje zwykłe 200 ze stroną CAPTCHA.
BLOCK_MARKERS = (
    "zbyt dużą liczbę zapytań",
    "/captcharenderer/",
    'id="captchaImage"',
    "captchaform",
    "Access Denied",
    "Request Rejected",
)


def _ascii(text: str) -> str:
    """Nagłówek HTTP musi dać się zapisać w ASCII."""
    return " ".join(text.split()).encode("ascii", "replace").decode("ascii")


def looks_blocked(html: str) -> bool:
    head = html[:8000]
    return any(m in head for m in BLOCK_MARKERS)


class TTLCache:
    """Mały cache w pamięci procesu. Znika przy restarcie - to nie jest baza danych."""

    def __init__(self, max_entries: int = 500):
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if expires < time.time():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class RateLimiter:
    def __init__(self, delay: float, jitter_pct: float = 0.25):
        self.delay = max(0.0, delay)
        self.jitter_pct = jitter_pct
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            nxt = self._next.get(host, 0.0)
            sleep_for = max(0.0, nxt - now)
            jitter = self.delay * self.jitter_pct
            self._next[host] = max(now, nxt) + max(
                0.05, self.delay + random.uniform(-jitter, jitter))
        if sleep_for:
            time.sleep(sleep_for)

    def penalise(self, host: str, seconds: float) -> None:
        with self._lock:
            self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + seconds)


class PoliteClient:
    MAX_DELAY = 20.0

    def __init__(self, http_cfg, cache_cfg):
        self.cfg = http_cfg
        self.cache_cfg = cache_cfg
        self.limiter = RateLimiter(http_cfg.delay_seconds, http_cfg.jitter_pct)
        self.cache = TTLCache(cache_cfg.max_entries)
        self.client = httpx.Client(
            headers={
                # Nagłówki HTTP są ASCII - polski znak w User-Agent wywala httpx
                # przy tworzeniu klienta, czyli przy starcie całej aplikacji.
                "User-Agent": _ascii(http_cfg.user_agent),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl-PL,pl;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                # orzeczenia.uzp.gov.pl oddaje listę wyników TYLKO żądaniom
                # wyglądającym na nawigację; bez tego wraca sam formularz.
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=httpx.Timeout(http_cfg.timeout_seconds),
            follow_redirects=True,
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------
    def _robots_ok(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(f"{origin}/robots.txt", timeout=8)
                body = r.text if r.status_code == 200 else ""
                if body.lstrip().lower().startswith(("<!doctype", "<html")):
                    body = ""      # serwis oddał HTML zamiast robots.txt
                rp.parse(body.splitlines())
                self._robots[origin] = rp
            except Exception:
                self._robots[origin] = None
        rp = self._robots[origin]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.cfg.user_agent, url)
        except Exception:
            return True

    def slow_down(self, reason: str = "") -> None:
        old = self.limiter.delay
        self.limiter.delay = min(old * 2, self.MAX_DELAY)
        if self.limiter.delay != old:
            log.warning("zwalniam: %.1fs -> %.1fs%s", old, self.limiter.delay,
                        f" ({reason})" if reason else "")

    # ------------------------------------------------------------------
    def get(self, url: str, *, ttl: int | None = None, ignore_robots: bool = False,
            cache_key: str | None = None, cookies=None) -> str:
        return self._cached("GET", url, None, ttl, ignore_robots, cache_key, cookies)

    def post(self, url: str, data: dict[str, str], *, ttl: int | None = None,
             ignore_robots: bool = False, cache_key: str | None = None, cookies=None) -> str:
        return self._cached("POST", url, data, ttl, ignore_robots, cache_key, cookies)

    def session(self, tag: str = "") -> "Session":
        """Kilka żądań dzielących ciasteczka - CBOSA trzyma wynik wyszukiwania
        w sesji po stronie serwera, więc kolejne strony da się pobrać tylko
        tym samym 'klientem'."""
        return Session(self, tag)

    # ------------------------------------------------------------------
    def _cached(self, method, url, data, ttl, ignore_robots, cache_key, cookies) -> str:
        ttl = self.cache_cfg.listing_ttl_seconds if ttl is None else ttl
        key = cache_key or (url if method == "GET" else
                            url + "|" + urlencode(sorted((data or {}).items())))
        if (hit := self.cache.get(key)) is not None:
            return hit
        text = self._fetch(method, url, data, ignore_robots, cookies)
        self.cache.put(key, text, ttl)
        return text

    def _fetch(self, method: str, url: str, data: dict[str, str] | None,
               ignore_robots: bool, cookies) -> str:
        if not ignore_robots and not self._robots_ok(url):
            raise SourceUnavailable(
                f"robots.txt tego serwisu zabrania pobierania tego adresu: {url}")

        host = urlparse(url).netloc
        last: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            self.limiter.wait(host)
            try:
                if method == "POST":
                    r = self.client.post(url, data=data or {}, cookies=cookies,
                                         headers={"Content-Type":
                                                  "application/x-www-form-urlencoded"})
                else:
                    r = self.client.get(url, cookies=cookies)
            except Exception as exc:
                last = exc
                log.warning("błąd połączenia (%s/%s) %s: %s",
                            attempt, self.cfg.max_retries, url, exc)
                time.sleep(self.cfg.backoff_base_seconds * attempt)
                continue

            if cookies is not None:
                cookies.update(r.cookies)

            if r.status_code == 200:
                text = r.text
                if looks_blocked(text):
                    self.slow_down("strona blokady/CAPTCHA")
                    self.limiter.penalise(host, self.cfg.cooldown_seconds)
                    raise RateLimited(
                        "serwis chwilowo ogranicza zapytania (pokazał stronę blokady). "
                        "Spróbuj ponownie za kilka minut.")
                return text

            if r.status_code in (404, 410):
                raise SourceUnavailable(f"nie znaleziono dokumentu ({r.status_code})")

            if r.status_code in (403, 429, 500, 502, 503, 504):
                wait = self.cfg.backoff_base_seconds * attempt
                self.limiter.penalise(host, wait)
                last = SourceUnavailable(f"HTTP {r.status_code}")
                log.warning("HTTP %s (%s/%s) %s", r.status_code, attempt,
                            self.cfg.max_retries, url)
                time.sleep(wait)
                continue

            raise SourceUnavailable(f"HTTP {r.status_code}")

        raise SourceUnavailable(f"serwis nie odpowiedział: {last}")


class Session:
    """Ciąg żądań dzielących ciasteczka i prefiks klucza cache'u.

    `tag` powinien jednoznacznie opisywać zapytanie - dzięki temu druga strona
    wyników jednego wyszukiwania nie wyląduje w cache'u pod tym samym kluczem,
    co druga strona zupełnie innego wyszukiwania."""

    def __init__(self, parent: PoliteClient, tag: str = ""):
        self.parent = parent
        self.tag = tag
        self.cookies = httpx.Cookies()

    def _key(self, url: str, data: dict[str, str] | None) -> str:
        extra = urlencode(sorted((data or {}).items()))
        return f"{self.tag}|{url}|{extra}"

    def get(self, url: str, *, ttl: int | None = None, ignore_robots: bool = False,
            cache: bool = True) -> str:
        if not cache:
            return self.parent._fetch("GET", url, None, ignore_robots, self.cookies)
        return self.parent.get(url, ttl=ttl, ignore_robots=ignore_robots,
                               cache_key=self._key(url, None), cookies=self.cookies)

    def post(self, url: str, data: dict[str, str], *, ttl: int | None = None,
             ignore_robots: bool = False, cache: bool = True) -> str:
        if not cache:
            return self.parent._fetch("POST", url, data, ignore_robots, self.cookies)
        return self.parent.post(url, data, ttl=ttl, ignore_robots=ignore_robots,
                                cache_key=self._key(url, data), cookies=self.cookies)
