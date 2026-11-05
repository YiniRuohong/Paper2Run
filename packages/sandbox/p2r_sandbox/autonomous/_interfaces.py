"""Target-side IPv4 interface enumeration using libc, with no extra packages.

Executed with python3 -I -c over the trusted local/SSH transport.
"""
import ctypes
import json
import socket
import sys


def ipv4_addresses(records, platform):
    """Decode sockaddr bytes (Darwin has sa_len before sa_family)."""
    addresses = set()
    for raw in records:
        if len(raw) < 8:
            continue
        family = raw[1] if platform == 'darwin' else int.from_bytes(raw[:2], sys.byteorder)
        if family == socket.AF_INET:
            addresses.add(socket.inet_ntop(socket.AF_INET, raw[4:8]))
    return sorted(addresses)


def interface_addresses():
    if sys.platform not in ('linux', 'darwin'):
        raise RuntimeError('IPv4 interface enumeration requires Linux or macOS')
    class Ifaddrs(ctypes.Structure):
        pass
    Ifaddrs._fields_ = [('next', ctypes.POINTER(Ifaddrs)), ('name', ctypes.c_char_p),
                       ('flags', ctypes.c_uint), ('addr', ctypes.c_void_p),
                       ('netmask', ctypes.c_void_p), ('union', ctypes.c_void_p),
                       ('data', ctypes.c_void_p)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(Ifaddrs))]
    libc.getifaddrs.restype = ctypes.c_int
    libc.freeifaddrs.argtypes = [ctypes.POINTER(Ifaddrs)]
    head = ctypes.POINTER(Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), 'getifaddrs failed')
    records = []
    try:
        current = head
        while current:
            entry = current.contents
            if entry.addr:
                # Every supported sockaddr is at least 8 bytes; decode only IPv4.
                records.append(ctypes.string_at(entry.addr, 8))
            current = entry.next
    finally:
        libc.freeifaddrs(head)
    addresses = ipv4_addresses(records, sys.platform)
    if not addresses:
        raise RuntimeError('getifaddrs returned no IPv4 interfaces; refusing execution')
    return addresses


if __name__ == '__main__':
    print(json.dumps(interface_addresses()))
