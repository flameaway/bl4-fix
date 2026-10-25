"""Dump the running game's decrypted image (headers + all sections) from outside the process."""
import ctypes, json, os, struct, sys
from ctypes import wintypes as w

pid = int(sys.argv[1])
out = sys.argv[2]
os.makedirs(out, exist_ok=True)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
k32.OpenProcess.restype = w.HANDLE
k32.ReadProcessMemory.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
psapi.EnumProcessModulesEx.argtypes = (w.HANDLE, ctypes.POINTER(ctypes.c_void_p), w.DWORD, ctypes.POINTER(w.DWORD), w.DWORD)

h = k32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
assert h, ctypes.get_last_error()
mods = (ctypes.c_void_p * 1024)()
need = w.DWORD()
assert psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(need), 3)
base = mods[0]


def read(addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
    return buf.raw[:got.value]


hdr = read(base, 0x1000)
pe = struct.unpack_from("<I", hdr, 0x3C)[0]
nsec = struct.unpack_from("<H", hdr, pe + 6)[0]
optsz = struct.unpack_from("<H", hdr, pe + 20)[0]
image_size = struct.unpack_from("<I", hdr, pe + 24 + 56)[0]
secs = []
for i in range(nsec):
    e = pe + 24 + optsz + i * 40
    name = hdr[e:e + 8].rstrip(b"\0").decode(errors="replace")
    vsize, va = struct.unpack_from("<II", hdr, e + 8)
    chars = struct.unpack_from("<I", hdr, e + 36)[0]
    secs.append({"name": name, "va": va, "vsize": vsize, "chars": chars})

# write a flat image: file offset == RVA
with open(os.path.join(out, "image.bin"), "wb") as f:
    f.write(hdr.ljust(0x1000, b"\0"))
    for s in secs:
        f.seek(s["va"])
        pos = 0
        while pos < s["vsize"]:
            n = min(16 << 20, s["vsize"] - pos)
            data = read(base + s["va"] + pos, n)
            # unreadable pages (guard / not committed): read page by page
            if len(data) < n:
                data = b"".join(read(base + s["va"] + pos + p, 0x1000).ljust(0x1000, b"\0")
                                for p in range(0, n, 0x1000))[:n]
            f.write(data)
            pos += n
json.dump({"base": base, "image_size": image_size, "sections": secs}, open(os.path.join(out, "meta.json"), "w"), indent=1)
print("base", hex(base), "image", hex(image_size), [(s["name"], hex(s["va"]), hex(s["vsize"])) for s in secs])
