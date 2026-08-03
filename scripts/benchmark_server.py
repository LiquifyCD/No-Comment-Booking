from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.request
from pathlib import Path


def timed(opener: urllib.request.OpenerDirector, request: urllib.request.Request) -> float:
    started = time.perf_counter()
    with opener.open(request, timeout=15) as response:
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        response.read()
    return (time.perf_counter() - started) * 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:8080")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()

    opener = urllib.request.build_opener()
    health = urllib.request.Request(f"{args.origin}/api/health")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        samples = list(pool.map(lambda _index: timed(opener, health), range(args.requests)))
    samples.sort()
    print(
        f"health requests={len(samples)} concurrency={args.concurrency} "
        f"median_ms={statistics.median(samples):.1f} p95_ms={samples[int(len(samples) * .95) - 1]:.1f}"
    )

    if args.credentials:
        credentials = {}
        for line in args.credentials.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            credentials[key.strip().casefold()] = value.strip()
        authenticated = urllib.request.build_opener()
        body = json.dumps(
            {"email": credentials["email"], "password": credentials["password"]}
        ).encode()
        login = urllib.request.Request(
            f"{args.origin}/api/auth/login",
            data=body,
            headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
        )
        with authenticated.open(login, timeout=15) as response:
            cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
        if not cookie:
            raise RuntimeError("Login did not issue a session cookie")
        bootstrap = urllib.request.Request(
            f"{args.origin}/api/bootstrap",
            headers={"Host": "127.0.0.1", "Cookie": cookie},
        )
        auth_samples = [timed(authenticated, bootstrap) for _ in range(20)]
        print(
            f"authenticated_bootstrap requests=20 "
            f"median_ms={statistics.median(auth_samples):.1f} "
            f"max_ms={max(auth_samples):.1f}"
        )


if __name__ == "__main__":
    main()
