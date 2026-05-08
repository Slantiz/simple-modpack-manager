class _C:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    ENDC = "\033[0m"


def green(s: str) -> str:
    return _C.GREEN + s + _C.ENDC


def yellow(s: str) -> str:
    return _C.YELLOW + s + _C.ENDC


def red(s: str) -> str:
    return _C.RED + s + _C.ENDC


def gray(s: str) -> str:
    return _C.GRAY + s + _C.ENDC


def bold(s: str) -> str:
    return _C.BOLD + s + _C.ENDC
