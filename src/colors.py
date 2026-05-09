import sys


def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return code + s + "\033[0m"


def green(s: str) -> str:
    return _c("\033[92m", s)


def yellow(s: str) -> str:
    return _c("\033[93m", s)


def red(s: str) -> str:
    return _c("\033[91m", s)


def gray(s: str) -> str:
    return _c("\033[90m", s)


def bold(s: str) -> str:
    return _c("\033[1m", s)


def blue(s: str) -> str:
    return _c("\033[94m", s)
