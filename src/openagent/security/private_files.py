"""Files only their owner can read, on each platform's own terms.

OpenAgent writes short-lived policy and credential-adjacent files outside the workspace — the
Gemini CLI system-settings override is the first, and it is a security boundary: whatever can
rewrite that file chooses which tools a "read-only" run may call.

The mistake this module exists to prevent is assuming POSIX permission bits mean something
everywhere. ``os.open(path, ..., 0o600)`` on Windows does not create a private file; the mode
argument only drives the read-only attribute, ``stat.S_IMODE`` reports ``0o666``, and the file
inherits whatever DACL its parent directory hands down. A check written as
``assert S_IMODE(...) == 0o600`` therefore either fails (as it did on CI) or, if "relaxed" to
accept ``0o666``, asserts nothing at all while reading as though it asserts everything.

So the two platforms get two implementations of one contract:

POSIX
    ``0700`` directories and ``0600`` files, created ``O_EXCL | O_NOFOLLOW`` so an attacker
    cannot win the create, ``fchmod``-ed after the fact so a permissive ``umask`` cannot widen
    them, and verified against the effective uid.

Windows
    A real discretionary ACL — current user and ``SYSTEM``, full control, nothing else —
    supplied *at creation time* in a ``SECURITY_ATTRIBUTES`` block and marked
    ``SE_DACL_PROTECTED`` so the parent's inheritable ACEs are not merged in. There is no
    window during which the object exists with the inherited DACL.

Both paths end in a verification that is allowed to fail the run. An ACL we could not confirm is
treated exactly like an ACL we know to be wrong: :func:`create_private_file` raises rather than
hand back a path the caller will describe to the user as private.

Production code here calls the Win32 API directly through :mod:`ctypes`. It never shells out to
``icacls`` or PowerShell — a security primitive that depends on a subprocess inherits that
subprocess's PATH resolution, and the runtime dependency is not one this project takes. (Tests
may use ``icacls`` as an independent second opinion; that is a different risk.)
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DIRECTORY_MODE",
    "FILE_MODE",
    "PrivateFileError",
    "SecurityVerification",
    "create_private_directory",
    "create_private_file",
    "private_directory",
    "verify_private_directory",
    "verify_private_file",
]

WINDOWS = os.name == "nt"

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600

#: Backend names reported by :class:`SecurityVerification`, so a caller (and Doctor) can tell
#: which contract was actually checked instead of inferring it from the platform.
POSIX_BACKEND = "posix-mode"
WINDOWS_BACKEND = "windows-dacl"


class PrivateFileError(RuntimeError):
    """A private file or directory could not be created, or could not be proven private."""


@dataclass(frozen=True)
class SecurityVerification:
    """The evidence behind a privacy claim, not just a boolean.

    ``findings`` is empty exactly when ``ok`` is true. Callers that report to the user (Doctor)
    print the findings; callers that enforce (:func:`create_private_file`) raise on them.
    """

    path: Path
    backend: str
    ok: bool
    findings: tuple[str, ...] = ()
    #: POSIX only — the permission bits actually observed.
    mode: int | None = None
    #: The owning principal: a uid on POSIX, a SID string on Windows.
    owner: str | None = None
    #: Windows only — whether the DACL is protected from inherited ACEs.
    inheritance_disabled: bool | None = None
    #: Windows only — the SID strings of every ACE on the object.
    trustees: tuple[str, ...] = field(default=())

    def raise_if_insecure(self) -> None:
        if not self.ok:
            raise PrivateFileError(
                f"{self.path} is not private ({self.backend}): " + "; ".join(self.findings)
            )


# --------------------------------------------------------------------------------- POSIX


def _posix_create_directory(path: Path) -> None:
    # mkdir's mode is masked by the umask, and a umask can only clear bits — but it can clear
    # owner bits too (umask 0700 is legal), so the mode is re-applied rather than assumed.
    os.mkdir(path, DIRECTORY_MODE)
    os.chmod(path, DIRECTORY_MODE)


def _posix_create_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    # O_NOFOLLOW: O_EXCL already refuses an existing symlink on Linux and macOS, but stating the
    # requirement in the flags keeps it true if the create is ever split from the open.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, FILE_MODE)
    try:
        os.fchmod(descriptor, FILE_MODE)
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _posix_verify(path: Path, *, expected_mode: int, directory: bool) -> SecurityVerification:
    findings: list[str] = []
    try:
        info = os.lstat(path)
    except OSError as error:
        return SecurityVerification(
            path=path, backend=POSIX_BACKEND, ok=False, findings=(f"cannot stat: {error}",)
        )

    if stat.S_ISLNK(info.st_mode):
        findings.append("is a symlink")
    elif directory and not stat.S_ISDIR(info.st_mode):
        findings.append("is not a directory")
    elif not directory and not stat.S_ISREG(info.st_mode):
        findings.append("is not a regular file")

    mode = stat.S_IMODE(info.st_mode)
    if mode != expected_mode:
        findings.append(f"mode is {mode:04o}, not {expected_mode:04o}")
    if mode & stat.S_IRWXG:
        findings.append("group has access")
    if mode & stat.S_IRWXO:
        findings.append("other has access")

    euid = os.geteuid()
    if info.st_uid != euid:
        findings.append(f"owned by uid {info.st_uid}, not {euid}")

    return SecurityVerification(
        path=path,
        backend=POSIX_BACKEND,
        ok=not findings,
        findings=tuple(findings),
        mode=mode,
        owner=str(info.st_uid),
    )


# --------------------------------------------------------------------------------- Windows
#
# Guarded on ``sys.platform`` rather than ``os.name`` so type checkers narrow the block away on
# the platforms where ``ctypes.WinDLL`` does not exist.

if sys.platform == "win32":  # pragma: no cover - exercised by the Windows CI leg
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    TOKEN_QUERY = 0x0008
    TokenUser = 1
    WinLocalSystemSid = 22
    WinWorldSid = 0  # Everyone
    WinAuthenticatedUserSid = 11
    WinBuiltinUsersSid = 27

    SECURITY_DESCRIPTOR_REVISION = 1
    ACL_REVISION = 2
    ACCESS_ALLOWED_ACE_TYPE = 0
    FILE_ALL_ACCESS = 0x001F01FF

    OBJECT_INHERIT_ACE = 0x01
    CONTAINER_INHERIT_ACE = 0x02

    SE_DACL_PRESENT = 0x0004
    SE_DACL_PROTECTED = 0x1000

    SE_FILE_OBJECT = 1
    OWNER_SECURITY_INFORMATION = 0x00000001
    DACL_SECURITY_INFORMATION = 0x00000004

    AclSizeInformation = 2

    GENERIC_WRITE = 0x40000000
    CREATE_NEW = 1
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    #: Every ACE we refuse to see on an object we called private. Named rather than derived so a
    #: reader can check the list against the threat model without decoding well-known SID ids.
    _FORBIDDEN_WELL_KNOWN = {
        WinWorldSid: "Everyone",
        WinAuthenticatedUserSid: "Authenticated Users",
        WinBuiltinUsersSid: "Users",
    }

    class _SECURITY_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("Revision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("Control", ctypes.c_ushort),
            ("Owner", ctypes.c_void_p),
            ("Group", ctypes.c_void_p),
            ("Sacl", ctypes.c_void_p),
            ("Dacl", ctypes.c_void_p),
        ]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_ushort),
        ]

    #: Offset of ``SidStart`` within ``ACCESS_ALLOWED_ACE``: a 4-byte header plus a 4-byte mask.
    _ACE_SID_OFFSET = 8

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.CreateWellKnownSid.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.CreateWellKnownSid.restype = wintypes.BOOL
    _advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    _advapi32.GetLengthSid.restype = wintypes.DWORD
    _advapi32.CopySid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    _advapi32.CopySid.restype = wintypes.BOOL
    _advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _advapi32.EqualSid.restype = wintypes.BOOL
    _advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
    _advapi32.IsValidSid.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
    _advapi32.SetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        wintypes.WORD,
        wintypes.WORD,
    ]
    _advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    _advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    _advapi32.InitializeAcl.restype = wintypes.BOOL
    _advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    _advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    _advapi32.GetAclInformation.restype = wintypes.BOOL
    _advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetAce.restype = wintypes.BOOL
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    def _win_error(call: str) -> PrivateFileError:
        code = ctypes.get_last_error()
        return PrivateFileError(f"{call} failed: [{code}] {ctypes.FormatError(code)}")

    def _aligned_buffer(size: int) -> ctypes.Array[ctypes.c_uint32]:
        """A DWORD-aligned scratch buffer.

        SIDs, ACLs and security descriptors all require 4-byte alignment, which
        ``create_string_buffer`` does not promise. An array of ``c_uint32`` does.
        """

        return (ctypes.c_uint32 * ((size + 3) // 4))()

    def _current_user_sid() -> ctypes.Array[ctypes.c_uint32]:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _win_error("OpenProcessToken")
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
            buffer = _aligned_buffer(max(needed.value, 8))
            if not _advapi32.GetTokenInformation(
                token,
                TokenUser,
                ctypes.byref(buffer),
                ctypes.sizeof(buffer),
                ctypes.byref(needed),
            ):
                raise _win_error("GetTokenInformation(TokenUser)")
            # TOKEN_USER is a SID_AND_ATTRIBUTES whose first member is the PSID.
            sid_pointer = ctypes.cast(
                ctypes.byref(buffer), ctypes.POINTER(ctypes.c_void_p)
            ).contents
            return _copy_sid(sid_pointer)
        finally:
            _kernel32.CloseHandle(token)

    def _copy_sid(source: ctypes.c_void_p | int) -> ctypes.Array[ctypes.c_uint32]:
        """Own the SID's storage, so it outlives the token buffer it was read from."""

        length = _advapi32.GetLengthSid(source)
        if not length:
            raise _win_error("GetLengthSid")
        copy = _aligned_buffer(length)
        if not _advapi32.CopySid(ctypes.sizeof(copy), ctypes.byref(copy), source):
            raise _win_error("CopySid")
        return copy

    def _well_known_sid(kind: int) -> ctypes.Array[ctypes.c_uint32]:
        size = wintypes.DWORD(0)
        _advapi32.CreateWellKnownSid(kind, None, None, ctypes.byref(size))
        buffer = _aligned_buffer(max(size.value, 8))
        size = wintypes.DWORD(ctypes.sizeof(buffer))
        if not _advapi32.CreateWellKnownSid(kind, None, ctypes.byref(buffer), ctypes.byref(size)):
            raise _win_error(f"CreateWellKnownSid({kind})")
        return buffer

    def _sid_string(sid: ctypes.c_void_p | int) -> str:
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return "<unprintable SID>"
        try:
            return text.value or "<empty SID>"
        finally:
            _kernel32.LocalFree(text)

    class _PrivateSecurityDescriptor:
        """A self-relative-free absolute SD granting only the owner and SYSTEM.

        The ACL and SID buffers are held as attributes because an absolute security descriptor
        stores *pointers* to them: if Python collected the ACL, the descriptor would point at
        freed memory. The lifetime of this object is the lifetime of the descriptor.
        """

        def __init__(self, *, inheritable: bool) -> None:
            self.user_sid = _current_user_sid()
            self.system_sid = _well_known_sid(WinLocalSystemSid)

            # Two ACEs, each a header plus a mask plus the SID, with slack for alignment.
            acl_size = (
                ctypes.sizeof(_ACE_HEADER) * 2
                + 8 * 2
                + ctypes.sizeof(self.user_sid)
                + ctypes.sizeof(self.system_sid)
                + 64
            )
            self.acl = _aligned_buffer(acl_size)
            if not _advapi32.InitializeAcl(
                ctypes.byref(self.acl), ctypes.sizeof(self.acl), ACL_REVISION
            ):
                raise _win_error("InitializeAcl")

            flags = (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) if inheritable else 0
            for sid in (self.user_sid, self.system_sid):
                if not _advapi32.AddAccessAllowedAceEx(
                    ctypes.byref(self.acl),
                    ACL_REVISION,
                    flags,
                    FILE_ALL_ACCESS,
                    ctypes.byref(sid),
                ):
                    raise _win_error("AddAccessAllowedAceEx")

            self.descriptor = _SECURITY_DESCRIPTOR()
            if not _advapi32.InitializeSecurityDescriptor(
                ctypes.byref(self.descriptor), SECURITY_DESCRIPTOR_REVISION
            ):
                raise _win_error("InitializeSecurityDescriptor")
            if not _advapi32.SetSecurityDescriptorDacl(
                ctypes.byref(self.descriptor), True, ctypes.byref(self.acl), False
            ):
                raise _win_error("SetSecurityDescriptorDacl")
            # Without SE_DACL_PROTECTED the system merges the parent's inheritable ACEs into the
            # DACL we just supplied, which is precisely the "Users can read it" outcome the
            # explicit ACL was written to prevent.
            if not _advapi32.SetSecurityDescriptorControl(
                ctypes.byref(self.descriptor), SE_DACL_PROTECTED, SE_DACL_PROTECTED
            ):
                raise _win_error("SetSecurityDescriptorControl")

            self.attributes = _SECURITY_ATTRIBUTES(
                nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
                lpSecurityDescriptor=ctypes.cast(
                    ctypes.byref(self.descriptor), ctypes.c_void_p
                ).value,
                bInheritHandle=False,
            )

    def _windows_create_directory(path: Path) -> None:
        security = _PrivateSecurityDescriptor(inheritable=True)
        if not _kernel32.CreateDirectoryW(str(path), ctypes.byref(security.attributes)):
            raise _win_error(f"CreateDirectoryW({path})")

    def _windows_create_file(path: Path, content: bytes) -> None:
        security = _PrivateSecurityDescriptor(inheritable=False)
        handle = _kernel32.CreateFileW(
            str(path),
            GENERIC_WRITE,
            0,  # no sharing: nothing else opens this file while we are writing it
            ctypes.byref(security.attributes),
            CREATE_NEW,  # fails if it already exists, the O_EXCL of Win32
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or not handle:
            raise _win_error(f"CreateFileW({path})")
        # open_osfhandle transfers ownership of the handle to the CRT descriptor; closing the
        # descriptor closes the handle, and closing both would be a double free.
        descriptor = msvcrt.open_osfhandle(handle, 0)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _windows_verify(path: Path, *, directory: bool) -> SecurityVerification:
        findings: list[str] = []
        if not path.exists():
            return SecurityVerification(
                path=path, backend=WINDOWS_BACKEND, ok=False, findings=("does not exist",)
            )
        if directory and not path.is_dir():
            findings.append("is not a directory")
        if not directory and not path.is_file():
            findings.append("is not a regular file")

        owner_pointer = ctypes.c_void_p()
        dacl_pointer = ctypes.c_void_p()
        descriptor_pointer = ctypes.c_void_p()
        status = _advapi32.GetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(owner_pointer),
            None,
            ctypes.byref(dacl_pointer),
            None,
            ctypes.byref(descriptor_pointer),
        )
        if status != 0:
            return SecurityVerification(
                path=path,
                backend=WINDOWS_BACKEND,
                ok=False,
                findings=(f"GetNamedSecurityInfoW failed with {status}",),
            )

        try:
            expected_user = _current_user_sid()
            owner = _sid_string(owner_pointer)
            if not _advapi32.EqualSid(owner_pointer, expected_user):
                findings.append(f"owner is {owner}, not the current user")

            control = wintypes.WORD()
            revision = wintypes.DWORD()
            inheritance_disabled: bool | None = None
            if _advapi32.GetSecurityDescriptorControl(
                descriptor_pointer, ctypes.byref(control), ctypes.byref(revision)
            ):
                if not control.value & SE_DACL_PRESENT:
                    findings.append("no DACL present (the object grants everyone access)")
                inheritance_disabled = bool(control.value & SE_DACL_PROTECTED)
                if not inheritance_disabled:
                    findings.append("DACL is not protected: inherited ACEs still apply")
            else:
                findings.append("could not read the security descriptor control flags")

            ace_sids = _read_dacl_aces(dacl_pointer, findings)
            trustees = tuple(_sid_string(sid) for sid in ace_sids)

            # SIDs are compared with EqualSid on live pointers, never by rendering both sides to
            # strings. An earlier revision built the comparison set with
            # ``_sid_string(cast(byref(_well_known_sid(...)), c_void_p))``; the buffer that
            # expression allocates is unreferenced by the time the cast is passed on, so the
            # comparison ran against garbage and SYSTEM's own ACE was reported as an intruder.
            # That failure was loud. The same construction on the *forbidden* side would have
            # been silent, and would have passed a world-readable file.
            system_sid = _well_known_sid(WinLocalSystemSid)
            allowed = (expected_user, system_sid)
            forbidden = [
                (name, _well_known_sid(kind)) for kind, name in _FORBIDDEN_WELL_KNOWN.items()
            ]

            for sid, rendered in zip(ace_sids, trustees, strict=True):
                named = next(
                    (name for name, known in forbidden if _advapi32.EqualSid(sid, known)), None
                )
                if named is not None:
                    findings.append(f"{named} has an ACE")
                elif not any(_advapi32.EqualSid(sid, known) for known in allowed):
                    findings.append(f"unexpected trustee {rendered} has an ACE")

            return SecurityVerification(
                path=path,
                backend=WINDOWS_BACKEND,
                ok=not findings,
                findings=tuple(findings),
                owner=owner,
                inheritance_disabled=inheritance_disabled,
                trustees=trustees,
            )
        finally:
            if descriptor_pointer:
                _kernel32.LocalFree(descriptor_pointer)

    def _read_dacl_aces(dacl: ctypes.c_void_p, findings: list[str]) -> tuple[ctypes.c_void_p, ...]:
        """Every ACE's SID, as a pointer into the caller's still-live security descriptor.

        Pointers rather than strings, because the caller compares them with ``EqualSid``. They
        are only valid until the descriptor is freed, which is why this is a private helper and
        not part of the module's contract.
        """

        if not dacl:
            findings.append("DACL is NULL (the object grants everyone access)")
            return ()
        size = _ACL_SIZE_INFORMATION()
        if not _advapi32.GetAclInformation(
            dacl, ctypes.byref(size), ctypes.sizeof(size), AclSizeInformation
        ):
            findings.append("could not enumerate the DACL")
            return ()

        sids: list[ctypes.c_void_p] = []
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                findings.append(f"could not read ACE {index}")
                continue
            header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            if header.AceType != ACCESS_ALLOWED_ACE_TYPE:
                # A deny ACE is not a hole, but it is not something this module writes, so it is
                # reported rather than silently accepted.
                findings.append(f"ACE {index} has unexpected type {header.AceType}")
                continue
            sid = ctypes.c_void_p((ace.value or 0) + _ACE_SID_OFFSET)
            if not _advapi32.IsValidSid(sid):
                findings.append(f"ACE {index} has an invalid SID")
                continue
            sids.append(sid)
        return tuple(sids)


# --------------------------------------------------------------------------------- contract


def create_private_directory(path: Path) -> None:
    """Create ``path`` as a directory only its owner (and, on Windows, SYSTEM) can enter.

    Raises :class:`PrivateFileError` if the directory cannot be created, or can be created but
    not proven private.
    """

    if sys.platform == "win32":
        _windows_create_directory(path)
    else:
        _posix_create_directory(path)
    verify_private_directory(path).raise_if_insecure()


def create_private_file(path: Path, content: bytes) -> None:
    """Write ``content`` to a new private ``path``, or raise.

    The file never exists in a readable state: on POSIX it is created ``O_EXCL`` at ``0600``, and
    on Windows it is created with its final DACL already attached. The parent directory is
    expected to be private too — :func:`private_directory` is the usual way to get one.
    """

    if sys.platform == "win32":
        _windows_create_file(path, content)
    else:
        _posix_create_file(path, content)
    verify_private_file(path).raise_if_insecure()


def verify_private_directory(path: Path) -> SecurityVerification:
    """Report whether ``path`` is a directory only the current user can reach."""

    if sys.platform == "win32":
        return _windows_verify(path, directory=True)
    return _posix_verify(path, expected_mode=DIRECTORY_MODE, directory=True)


def verify_private_file(path: Path) -> SecurityVerification:
    """Report whether ``path`` is a file only the current user can read."""

    if sys.platform == "win32":
        return _windows_verify(path, directory=False)
    return _posix_verify(path, expected_mode=FILE_MODE, directory=False)


@contextmanager
def private_directory(prefix: str) -> Iterator[Path]:
    """A private temporary directory, removed on exit.

    ``tempfile.mkdtemp`` is not used: on Windows it creates the directory with the inherited
    DACL, and tightening it afterwards leaves a window. The name is random enough that an
    attacker cannot pre-create it, and creation fails rather than reuses if they somehow do.
    """

    root = Path(tempfile.gettempdir())
    for _ in range(8):
        candidate = root / f"{prefix}{secrets.token_hex(12)}"
        try:
            create_private_directory(candidate)
        except FileExistsError:
            continue
        except PrivateFileError:
            raise
        try:
            yield candidate
        finally:
            _remove_tree(candidate)
        return
    raise PrivateFileError(f"could not create a private directory under {root}")


def _remove_tree(path: Path) -> None:
    """Remove the tree without following symlinks out of it."""

    import shutil

    shutil.rmtree(path, ignore_errors=True)
