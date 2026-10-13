from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
import sys
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    pass


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446
_CREATE_VERSION = 1
_PATH_BENEATH = 1
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_READ = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
_FS_WRITE = (
    _FS_WRITE_FILE
    | _FS_REMOVE_DIR
    | _FS_REMOVE_FILE
    | _FS_MAKE_DIR
    | _FS_MAKE_REG
    | _FS_MAKE_SYM
    | _FS_REFER
    | _FS_TRUNCATE
)
_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_FILTER = 2
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_RET_K = 0x06
_SECCOMP_ALLOW = 0x7FFF0000
_SECCOMP_ERRNO = 0x00050000


def landlock_abi() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    value = libc.syscall(_CREATE_RULESET, 0, 0, _CREATE_VERSION)
    return int(value) if value >= 0 else 0


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    result = libc.syscall(number, *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(result)


def _allow_path(libc: ctypes.CDLL, ruleset_fd: int, path: Path, access: int) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        access &= _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = _PathRule(access, fd)
        _syscall(libc, _ADD_RULE, ruleset_fd, _PATH_BENEATH, ctypes.byref(rule), 0)
    finally:
        os.close(fd)


def _deny_process_escape(libc: ctypes.CDLL) -> None:
    if platform.machine() not in {"x86_64", "amd64"}:
        raise SandboxUnavailable("seccomp syscall table is currently defined for x86_64 only")
    blocked = [
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        51,
        52,
        53,
        54,
        55,
        101,
        288,
        299,
        307,
        310,
        311,
        438,
    ]
    filters = [_SockFilter(_BPF_LD_W_ABS, 0, 0, 0)]
    for number in blocked:
        filters.extend(
            [
                _SockFilter(_BPF_JMP_JEQ_K, 0, 1, number),
                _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_ERRNO | errno.EPERM),
            ]
        )
    filters.append(_SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_ALLOW))
    array = (_SockFilter * len(filters))(*filters)
    program = _SockFprog(len(filters), array)
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_FILTER, ctypes.byref(program)) != 0:
        code = ctypes.get_errno()
        raise SandboxUnavailable(f"seccomp unavailable: {os.strerror(code)}")


def restrict_to_workspace(workspace: Path) -> None:
    workspace = workspace.resolve(strict=True)
    abi = landlock_abi()
    if abi < 4:
        raise SandboxUnavailable(f"Landlock ABI 4 required, found {abi}")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise SandboxUnavailable(f"no_new_privs failed: {os.strerror(code)}")
    rules = _Ruleset(_FS_READ | _FS_WRITE, _NET_BIND_TCP | _NET_CONNECT_TCP)
    ruleset_fd = _syscall(libc, _CREATE_RULESET, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    try:
        for path in ("/bin", "/usr", "/lib", "/lib64"):
            _allow_path(libc, ruleset_fd, Path(path), _FS_READ)
        for path in ("/dev/null", "/dev/urandom"):
            _allow_path(libc, ruleset_fd, Path(path), _FS_READ | _FS_WRITE)
        _allow_path(libc, ruleset_fd, workspace, _FS_READ | _FS_WRITE)
        _syscall(libc, _RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)
    _deny_process_escape(libc)


def _safe_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PermissionError(f"path outside workspace: {value}")
    return path


def _file_operation(operation: str) -> int:
    payload = json.loads(sys.stdin.read())
    path = _safe_path(payload.get("path", "."))
    if operation == "read":
        raw = path.read_bytes()
        truncated = len(raw) > 512 * 1024
        text = raw[: 512 * 1024].decode("utf-8", errors="replace")
        print(text + ("\n[truncated]" if truncated else ""), end="")
    elif operation == "write":
        content = payload["content"]
        encoded = content.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("content exceeds 1 MB")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {len(encoded)} bytes to {path}")
    elif operation == "list":
        if not path.is_dir():
            raise NotADirectoryError(path)
        max_depth = int(payload.get("max_depth", 2))
        lines = [str(path) + "/"]
        count = 0

        def walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal count
            entries = sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name))
            for index, entry in enumerate(entries):
                if count >= 200:
                    lines.append(prefix + "... (truncated)")
                    return
                last = index == len(entries) - 1
                suffix = "/" if entry.is_dir() else ""
                lines.append(prefix + ("└── " if last else "├── ") + entry.name + suffix)
                count += 1
                if entry.is_dir() and depth < max_depth:
                    walk(entry, depth + 1, prefix + ("    " if last else "│   "))

        walk(path, 1, "")
        print("\n".join(lines), end="")
    return 0


def clean_environment(workspace: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("operation", choices=["exec", "read", "write", "list"])
    parser.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    os.chdir(args.workspace)
    restrict_to_workspace(args.workspace)
    if args.operation == "exec":
        os.execve("/bin/sh", ["sh", "-c", args.value or ""], clean_environment(args.workspace))
    return _file_operation(args.operation)


if __name__ == "__main__":
    raise SystemExit(main())
