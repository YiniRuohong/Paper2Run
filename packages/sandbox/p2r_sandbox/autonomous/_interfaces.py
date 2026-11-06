"""Target-side IPv4 interface enumeration with no extra packages.

Linux/macOS use libc getifaddrs; Windows uses iphlpapi GetAdaptersAddresses.
Executed with ``-I -c`` over the trusted transport: the host's own interpreter
for a local target, ``python3`` on an SSH target (POSIX by requirement).
The Windows branch is unit-tested with a fake buffer, not on a real machine.
"""
import ctypes
import json
import socket
import sys

AF_INET_WINDOWS = 2
ERROR_SUCCESS, ERROR_BUFFER_OVERFLOW, ERROR_NO_DATA = 0, 111, 232
# Unicast only: skip anycast, multicast and DNS server lists.
GAA_FLAGS = 0x2 | 0x4 | 0x8


class SocketAddress(ctypes.Structure):
    _fields_ = [('sockaddr', ctypes.c_void_p), ('length', ctypes.c_int32)]


class UnicastAddress(ctypes.Structure):
    pass


# Leading fields of IP_ADAPTER_UNICAST_ADDRESS_LH / IP_ADAPTER_ADDRESSES_LH.
# The leading union is {ULONGLONG Alignment; {ULONG Length; ULONG Flags/IfIndex}}:
# two 32-bit fields then the Next pointer at offset 8 on 32- and 64-bit Windows.
UnicastAddress._fields_ = [('length', ctypes.c_uint32), ('flags', ctypes.c_uint32),
                           ('next', ctypes.POINTER(UnicastAddress)), ('address', SocketAddress)]


class AdapterAddresses(ctypes.Structure):
    pass


AdapterAddresses._fields_ = [('length', ctypes.c_uint32), ('if_index', ctypes.c_uint32),
                             ('next', ctypes.POINTER(AdapterAddresses)), ('adapter_name', ctypes.c_char_p),
                             ('first_unicast', ctypes.POINTER(UnicastAddress))]


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


def adapter_records(head):
    """First 8 bytes of every unicast sockaddr in a GetAdaptersAddresses list."""
    records = []
    adapter = head
    while adapter:
        unicast = adapter.contents.first_unicast
        while unicast:
            address = unicast.contents.address
            if address.sockaddr and address.length >= 8:
                records.append(ctypes.string_at(address.sockaddr, 8))
            unicast = unicast.contents.next
        adapter = adapter.contents.next
    return records


def windows_records(api=None):
    if api is None:
        api = ctypes.WinDLL('iphlpapi').GetAdaptersAddresses
        api.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_uint32)]
        api.restype = ctypes.c_uint32
    size = ctypes.c_uint32(16 * 1024)
    for _ in range(8):  # adapters can appear between the size query and the call
        buffer = ctypes.create_string_buffer(size.value)
        result = api(AF_INET_WINDOWS, GAA_FLAGS, None, buffer, ctypes.byref(size))
        if result == ERROR_SUCCESS:
            return adapter_records(ctypes.cast(buffer, ctypes.POINTER(AdapterAddresses)))
        if result == ERROR_NO_DATA:
            return []
        if result != ERROR_BUFFER_OVERFLOW:
            raise OSError(result, 'GetAdaptersAddresses failed')
    raise OSError(ERROR_BUFFER_OVERFLOW, 'GetAdaptersAddresses buffer size did not settle')


def getifaddrs_records():
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
    return records


def interface_addresses(platform=None):
    platform = platform or sys.platform
    if platform == 'win32':
        records = windows_records()
    elif platform in ('linux', 'darwin'):
        records = getifaddrs_records()
    else:
        raise RuntimeError('IPv4 interface enumeration requires Linux, macOS or Windows')
    addresses = ipv4_addresses(records, platform)
    if not addresses:
        raise RuntimeError('host returned no IPv4 interfaces; refusing execution')
    return addresses


if __name__ == '__main__':
    print(json.dumps(interface_addresses()))
