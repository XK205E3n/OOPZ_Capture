"""Opt-in, bounded avatar cache for URLs already returned by OOPZ membership.

Nothing is fetched on import/construction. The caller must gate ``get`` on the
image feature. No cookies, proxy environment, OOPZ credentials or new profile
API calls are used. Failures return ``None`` and never log source URLs.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import math
import os
import re
import socket
import ssl
import stat
import secrets
import threading
import time
import warnings
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


class UnsafeAvatar(ValueError):
    """An avatar did not satisfy the resource or network boundary."""


@dataclass(frozen=True, slots=True)
class AvatarLimits:
    timeout_seconds: float = 5.0
    max_redirects: int = 2
    max_bytes: int = 2 * 1024 * 1024
    max_pixels: int = 4_000_000
    max_side: int = 512
    max_cache_entries: int = 128
    max_cache_bytes: int = 32 * 1024 * 1024
    failure_retry_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
                or not math.isfinite(self.failure_retry_seconds) or self.failure_retry_seconds < 0):
            raise ValueError("invalid avatar timeout")
        for name in ("max_bytes", "max_pixels", "max_side", "max_cache_entries", "max_cache_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError("invalid avatar resource limit")
        if type(self.max_redirects) is not int or self.max_redirects < 0:
            raise ValueError("invalid avatar redirect limit")


@dataclass(frozen=True, slots=True)
class AvatarRequest:
    """A validated destination. A transport must connect only to ``address``."""
    host: str
    target: str
    address: str
    deadline: float  # absolute time.monotonic() deadline, shared by all hops
    max_bytes: int


@dataclass(frozen=True, slots=True)
class AvatarResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


# stdlib DNS has no timeout. At most four timed-out daemon resolvers may linger;
# callers time out and fail closed rather than accumulating unbounded threads.
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_MAX_ADDRESSES = 16
_CACHE_NAME = re.compile(r"avatar-[0-9a-f]{64}\.png\Z")
_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
_REDIRECTS = {301, 302, 303, 307, 308}
# Cloud metadata/wire-server addresses and IPv6 translation/tunnelling ranges
# need explicit exclusion even where Python versions label them global.
_DENIED_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "168.63.129.16/32", "192.0.0.0/24", "192.88.99.0/24",
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/23", "2002::/16",
    "3fff::/20", "::ffff:0:0/96",
))


def _public_address(value: str) -> str:
    if not isinstance(value, str) or "%" in value:
        raise UnsafeAvatar("invalid avatar address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise UnsafeAvatar("invalid avatar address") from None
    if (not address.is_global or address.is_multicast or address.is_reserved
            or any(address.version == network.version and address in network
                   for network in _DENIED_NETWORKS)):
        raise UnsafeAvatar("non-public avatar address")
    return str(address)


def _validated_url(value: str) -> tuple[str, str, str]:
    """Return canonical URL, ASCII host, and origin-form request target."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or "\\" in value):
        raise UnsafeAvatar("invalid avatar URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme.lower() != "https" or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or "%" in parts.netloc or parts.fragment):
            raise UnsafeAvatar("invalid avatar URL")
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or "." not in host
                    or host.endswith((".localhost", ".local", ".internal", ".lan", ".home",
                                      ".test", ".invalid", ".example", ".onion"))
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split("."))):
                raise UnsafeAvatar("invalid avatar host")
        else:
            host = _public_address(host)
        authority = f"[{host}]" if ":" in host else host
        path = parts.path or "/"
        # http.client expects an ASCII request target; URLs from the SDK should
        # already be percent-encoded. Do not reinterpret a malformed target.
        target = path + (f"?{parts.query}" if parts.query else "")
        target.encode("ascii")
        return urlunsplit(("https", authority, path, parts.query, "")), host, target
    except (ValueError, UnicodeError):
        raise UnsafeAvatar("invalid avatar URL") from None


def _system_resolve(host: str) -> Sequence[str]:
    return list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
    )))


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("avatar time budget exhausted")
    return remaining


def _resolve_public(host: str, resolver: Callable[[str], Sequence[str]], deadline: float) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [_public_address(str(literal))]
    if not _DNS_SLOTS.acquire(blocking=False):
        raise UnsafeAvatar("avatar resolver capacity exhausted")
    ready = threading.Event()
    result: list[object] = []

    def run() -> None:
        try:
            addresses = resolver(host)
            # The injected resolver has the same bounded, finite-list contract.
            if not isinstance(addresses, (list, tuple)) or not 0 < len(addresses) <= _DNS_MAX_ADDRESSES:
                raise UnsafeAvatar("invalid avatar DNS response")
            result.append([_public_address(address) for address in addresses])
        except Exception:
            result.append(None)
        finally:
            _DNS_SLOTS.release()
            ready.set()

    try:
        threading.Thread(target=run, name="avatar-dns", daemon=True).start()
    except Exception:
        _DNS_SLOTS.release()
        raise
    if not ready.wait(_remaining(deadline)):
        raise TimeoutError("avatar DNS timed out")
    _remaining(deadline)
    if not result or result[0] is None:
        raise UnsafeAvatar("avatar DNS rejected")
    return result[0]  # type: ignore[return-value]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, request: AvatarRequest) -> None:
        super().__init__(request.host, port=443, timeout=_remaining(request.deadline),
                         context=ssl.create_default_context())
        self.avatar_request = request
        # HTTPConnection clears .sock for Connection: close responses while
        # HTTPResponse's file still owns the live fd. Keep the deadline handle.
        self.deadline_socket: socket.socket | None = None

    def connect(self) -> None:
        request = self.avatar_request
        address = _public_address(request.address)
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        raw_socket = socket.socket(family, socket.SOCK_STREAM)
        self.sock = raw_socket
        self.deadline_socket = raw_socket
        try:
            raw_socket.settimeout(_remaining(request.deadline))
            destination = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            raw_socket.connect(destination)  # numeric IP, no second DNS lookup
            if _public_address(raw_socket.getpeername()[0]) != address:
                raise UnsafeAvatar("avatar peer differs from pinned address")
            raw_socket.settimeout(_remaining(request.deadline))
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=request.host)
            self.deadline_socket = self.sock
        except BaseException:
            raw_socket.close()
            self.sock = None
            raise


def _https_transport(request: AvatarRequest) -> AvatarResponse:
    """Direct TLS, no proxies, redirects, cookies, credentials or DNS relookup."""
    connection = _PinnedHTTPSConnection(request)

    def abort() -> None:
        # Enforce a wall-clock limit even against a peer dripping header/body
        # bytes often enough to avoid an ordinary per-recv socket timeout.
        sock = connection.deadline_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    timer = threading.Timer(_remaining(request.deadline), abort)
    timer.daemon = True
    timer.start()
    response = None
    try:
        connection.request("GET", request.target, headers={
            "Accept": "image/png,image/jpeg,image/webp,image/gif",
            "Accept-Encoding": "identity", "User-Agent": "OOPZ-Avatar-Cache/1",
            "Connection": "close",
        })
        response = connection.getresponse()
        _remaining(request.deadline)
        headers: dict[str, str] = {}
        for key, value in response.getheaders():
            name = key.lower()
            if name in headers and name in {"content-type", "content-length", "content-encoding", "transfer-encoding", "location"}:
                raise UnsafeAvatar("ambiguous avatar response header")
            headers[name] = value
        if response.status in _REDIRECTS:
            return AvatarResponse(response.status, headers)
        if response.status != 200:
            return AvatarResponse(response.status, headers)
        _validate_headers(headers, request.max_bytes)
        body = bytearray()
        while True:
            _remaining(request.deadline)
            chunk = response.read(min(65536, request.max_bytes + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > request.max_bytes:
                raise UnsafeAvatar("avatar exceeds byte budget")
        _remaining(request.deadline)
        return AvatarResponse(response.status, headers, bytes(body))
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def _validate_headers(headers: Mapping[str, str], max_bytes: int) -> None:
    if sum(len(key) + len(value) for key, value in headers.items()) > 16384:
        raise UnsafeAvatar("avatar response headers exceed budget")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() not in _ALLOWED_TYPES:
        raise UnsafeAvatar("avatar is not a supported raster image")
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise UnsafeAvatar("encoded avatar response rejected")
    if "transfer-encoding" in headers and (
        headers["transfer-encoding"].strip().lower() != "chunked" or "content-length" in headers
    ):
        raise UnsafeAvatar("ambiguous avatar transfer encoding")
    if "content-length" in headers:
        value = headers["content-length"]
        if not re.fullmatch(r"[0-9]{1,12}", value) or int(value) > max_bytes:
            raise UnsafeAvatar("invalid avatar content length")


def _normalize_image(body: bytes, limits: AvatarLimits, *, cache_hit: bool = False) -> bytes:
    from PIL import Image, ImageOps

    if not body or len(body) > limits.max_bytes:
        raise UnsafeAvatar("avatar exceeds byte budget")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(body)) as candidate:
            if candidate.format not in ({"PNG"} if cache_hit else _ALLOWED_FORMATS):
                raise UnsafeAvatar("unsupported avatar raster format")
            width, height = candidate.size
            if width < 1 or height < 1 or width * height > limits.max_pixels:
                raise UnsafeAvatar("avatar exceeds pixel budget")
            if cache_hit and max(width, height) > limits.max_side:
                raise UnsafeAvatar("invalid cached avatar dimensions")
            candidate.verify()
        with Image.open(io.BytesIO(body)) as candidate:
            candidate.seek(0)  # animations are reduced to a single raster frame
            candidate.load()
            oriented = ImageOps.exif_transpose(candidate)
            oriented.thumbnail((limits.max_side, limits.max_side), Image.Resampling.LANCZOS)
            # Fresh pixels exclude EXIF, ICC, text chunks and animation metadata.
            converted = oriented.convert("RGBA")
            clean = Image.new("RGBA", converted.size)
            clean.paste(converted)
            output = io.BytesIO()
            clean.save(output, format="PNG", optimize=False)
    png = output.getvalue()
    if len(png) > limits.max_bytes:
        raise UnsafeAvatar("normalized avatar exceeds byte budget")
    return png


def _close_directory_handles(handles: dict[tuple[str, ...], int]) -> None:
    for fd in tuple(handles.values()):
        try:
            os.close(fd)
        except OSError:
            pass
    handles.clear()


class AvatarCache:
    """Session-local images keyed by stable UID AND URL, with bounded storage.

    ``resolver`` and ``transport`` are trusted dependency-injection seams for
    entirely offline tests. Production callers should use the pinned defaults.
    Their inputs contain no OOPZ API credentials and must never be logged.
    """
    def __init__(self, cache_dir: Path, *, limits: AvatarLimits | None = None,
                 resolver: Callable[[str], Sequence[str]] | None = None,
                 transport: Callable[[AvatarRequest], AvatarResponse] | None = None) -> None:
        supplied = Path(cache_dir)
        self.cache_dir = Path(os.path.abspath(supplied))  # lexical only; never resolve symlinks
        self._safe_path = ".." not in supplied.parts and len(self.cache_dir.parts) > 1
        self.limits = limits or AvatarLimits()
        self.resolver = resolver or _system_resolve
        self.transport = transport or _https_transport
        self._failures: OrderedDict[str, float] = OrderedDict()
        self.last_failure = ""      # why the latest get() returned None (a fixed phrase or exception class, never a URL)
        self._lock = threading.RLock()
        self._directory_ids: dict[tuple[str, ...], tuple[int, int]] = {}
        self._missing_directories: set[tuple[str, ...]] = set()
        # Keep the original inodes alive across calls, preventing an ABA reuse
        # after deletion/recreation. Finalization never follows filesystem paths.
        self._directory_handles: dict[tuple[str, ...], int] = {}
        self._finalizer = weakref.finalize(self, _close_directory_handles, self._directory_handles)

    def close(self) -> None:
        """Release pinned directory handles; a closed cache fails safely."""
        with self._lock:
            self._finalizer()

    def get(self, oopz_uid: str, avatar_url: str) -> Path | None:
        """Get a verified PNG or ``None``; never fabricate a replacement face."""
        if not isinstance(oopz_uid, str) or not oopz_uid.strip() or len(oopz_uid) > 1024:
            return None
        key = ""
        try:
            canonical, _, _ = _validated_url(avatar_url)
            key = hashlib.sha256(("avatar-v1\0" + oopz_uid + "\0" + canonical).encode("utf-8")).hexdigest()
            with self._lock:
                if self._failures.get(key, 0) > time.monotonic():
                    return None
            # Missing Pillow should not cause even an attempted download.
            from PIL import Image  # noqa: F401
            path = self.cache_dir / f"avatar-{key}.png"
            cached = self._read_cached(path)
            if cached is not None:
                return path
            deadline = time.monotonic() + self.limits.timeout_seconds
            current = canonical
            seen: set[str] = set()
            for hop in range(self.limits.max_redirects + 1):
                current, host, target = _validated_url(current)
                if current in seen:
                    raise UnsafeAvatar("avatar redirect cycle")
                seen.add(current)
                addresses = _resolve_public(host, self.resolver, deadline)
                response = self.transport(AvatarRequest(host, target, addresses[0], deadline, self.limits.max_bytes))
                _remaining(deadline)
                headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status in _REDIRECTS:
                    if hop == self.limits.max_redirects or not headers.get("location"):
                        raise UnsafeAvatar("avatar redirect limit")
                    current = urljoin(current, headers["location"])
                    continue
                if response.status != 200:
                    raise UnsafeAvatar("avatar fetch failed")
                _validate_headers(headers, self.limits.max_bytes)
                if not isinstance(response.body, bytes):
                    raise UnsafeAvatar("invalid avatar payload")
                if "content-length" in headers and int(headers["content-length"]) != len(response.body):
                    raise UnsafeAvatar("avatar content length mismatch")
                png = _normalize_image(response.body, self.limits)
                return self._store(path, png)
            return None
        except Exception as error:
            # Fail closed, without URLs, headers or exception strings in logs; only the reason class is kept.
            self.last_failure = str(error) if isinstance(error, UnsafeAvatar) else type(error).__name__
            if key:
                with self._lock:
                    self._failures[key] = time.monotonic() + self.limits.failure_retry_seconds
                    self._failures.move_to_end(key)
                    while len(self._failures) > self.limits.max_cache_entries:
                        self._failures.popitem(last=False)
            return None

    def _walk_directory(self, *, create: bool) -> int:
        """Open every ancestor without following links, pinning inode identity.

        Secure descriptor-relative operations are required. Unsupported platforms
        omit optional avatars rather than falling back to racy pathname writes.
        The instance lock protects the pinned identities and missing-path state.
        """
        if (not self._finalizer.alive or not self._safe_path or not hasattr(os, "O_NOFOLLOW")
                or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NONBLOCK")
                or any(operation not in os.supports_dir_fd
                       for operation in (os.open, os.stat, os.mkdir, os.unlink, os.rename))
                or os.listdir not in os.supports_fd):
            raise UnsafeAvatar("secure avatar cache filesystem unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        parts = self.cache_dir.parts
        fd = os.open(parts[0], flags)
        try:
            for index in range(len(parts)):
                prefix = parts[:index + 1]
                if index:
                    created = False
                    try:
                        child = os.open(parts[index], flags, dir_fd=fd)
                    except FileNotFoundError:
                        if prefix in self._directory_ids:
                            raise UnsafeAvatar("avatar cache ancestor disappeared") from None
                        self._missing_directories.add(prefix)
                        if not create:
                            raise
                        # Do not accept an existing entry in a mkdir race.
                        os.mkdir(parts[index], mode=0o700, dir_fd=fd)
                        created = True
                        child = os.open(parts[index], flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
                    if prefix in self._missing_directories and not created:
                        raise UnsafeAvatar("avatar cache directory appeared unexpectedly")
                    self._missing_directories.discard(prefix)
                info = os.fstat(fd)
                identity = (info.st_dev, info.st_ino)
                if not stat.S_ISDIR(info.st_mode):
                    raise UnsafeAvatar("avatar cache ancestor is not a directory")
                if prefix in self._directory_ids and self._directory_ids[prefix] != identity:
                    raise UnsafeAvatar("avatar cache ancestor changed")
                if prefix not in self._directory_handles:
                    self._directory_handles[prefix] = os.dup(fd)
                self._directory_ids[prefix] = identity
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _assert_current_directory(self, fd: int) -> None:
        check = self._walk_directory(create=False)
        try:
            original, current = os.fstat(fd), os.fstat(check)
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise UnsafeAvatar("avatar cache directory changed")
        finally:
            os.close(check)

    @contextmanager
    def _directory(self, *, create: bool = False) -> Iterator[int]:
        fd = self._walk_directory(create=create)
        try:
            self._assert_current_directory(fd)
            yield fd
            self._assert_current_directory(fd)
        finally:
            os.close(fd)

    def _read_cached(self, path: Path) -> bytes | None:
        try:
            with self._lock, self._directory() as directory:
                # Nonblocking prevents a malicious FIFO from hanging before
                # fstat can reject it. Never open a symlink, even transiently.
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise UnsafeAvatar("avatar cache entry is not a regular file")
                    if info.st_size > self.limits.max_bytes:
                        return None
                    handle = os.fdopen(fd, "rb")
                    fd = -1  # the file object now owns the descriptor
                    with handle:
                        data = handle.read(self.limits.max_bytes + 1)
                finally:
                    if fd >= 0:
                        os.close(fd)
                try:
                    _normalize_image(data, self.limits, cache_hit=True)
                except Exception:
                    return None
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(current.st_mode)
                        or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                    raise UnsafeAvatar("avatar cache entry changed")
                return data
        except FileNotFoundError:
            return None
        # Invalid regular-file content can be refetched. Unsafe filesystem
        # entries/ancestors escape to get(), which fails closed without fetching.

    def _store(self, path: Path, png: bytes) -> Path:
        if len(png) > self.limits.max_cache_bytes:
            raise UnsafeAvatar("avatar exceeds cache budget")
        with self._lock, self._directory(create=True) as directory:
            try:
                existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(existing.st_mode):
                    raise UnsafeAvatar("avatar cache target is not a regular file")
            temporary: str | None = ".avatar-" + secrets.token_hex(16) + ".tmp"
            created = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                             mode=0o600, dir_fd=directory)
                created = True
                with os.fdopen(fd, "wb") as handle:
                    handle.write(png)
                    written = os.fstat(handle.fileno())
                self._assert_current_directory(directory)
                current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(current.st_mode)
                        or (written.st_dev, written.st_ino) != (current.st_dev, current.st_ino)):
                    raise UnsafeAvatar("avatar cache temporary changed")
                os.rename(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                temporary = None
                self._prune(directory, path.name)
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(current.st_mode)
                        or (written.st_dev, written.st_ino) != (current.st_dev, current.st_ino)):
                    raise UnsafeAvatar("avatar cache target changed")
            finally:
                if temporary is not None and created:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        return path

    def _prune(self, directory: int, keep: str) -> None:
        self._assert_current_directory(directory)
        entries = []
        for name in os.listdir(directory):
            if not _CACHE_NAME.fullmatch(name):
                continue
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                entries.append((info.st_mtime_ns, name, info.st_size))
        size = sum(item[2] for item in entries)
        count = len(entries)
        for _, name, length in sorted(entries, key=lambda item: (item[0], item[1])):
            if count <= self.limits.max_cache_entries and size <= self.limits.max_cache_bytes:
                break
            if name == keep:
                continue
            self._assert_current_directory(directory)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            count -= 1
            size -= length
