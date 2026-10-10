"""Watch-only address derivation (BIP32 public keys -> native segwit bc1q addresses). No dependencies.
Accepts an xpub / zpub, or a descriptor such as wpkh([fp/84h/0h/0h]xpub.../0/*)."""
import hashlib
import hmac
import re

_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _add(a, b):
    if a is None: return b
    if b is None: return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0: return None
    if a == b:
        m = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P) % _P
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P) % _P
    x = (m * m - a[0] - b[0]) % _P
    return x, (m * (a[0] - x) - a[1]) % _P


def _mul(k, pt=_G):
    r = None
    while k:
        if k & 1: r = _add(r, pt)
        pt, k = _add(pt, pt), k >> 1
    return r


def _b58check(s):
    n = 0
    for ch in s: n = n * 58 + _B58.index(ch)
    raw = n.to_bytes(82, "big").lstrip(b"\0") if False else n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(s) - len(s.lstrip("1"))) + raw
    body, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        raise ValueError("מפתח לא תקין (checksum)")
    return body


def _ripemd160(data):
    try:
        return hashlib.new("ripemd160", data).digest()
    except ValueError:
        return _rmd160(data)


def _rmd160(msg):                                    # fallback when OpenSSL has no ripemd160
    rol = lambda x, n: ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF
    r1 = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,7,4,13,1,10,6,15,3,12,0,9,5,2,14,11,8,3,10,14,4,9,15,8,1,2,7,0,6,13,11,5,12,1,9,11,10,0,8,12,4,13,3,7,15,14,5,6,2,4,0,5,9,7,12,2,10,14,1,3,8,11,6,15,13]
    r2 = [5,14,7,0,9,2,11,4,13,6,15,8,1,10,3,12,6,11,3,7,0,13,5,10,14,15,8,12,4,9,1,2,15,5,1,3,7,14,6,9,11,8,12,2,10,0,4,13,8,6,4,1,3,11,15,0,5,12,2,13,9,7,10,14,12,15,10,4,1,5,8,7,6,2,13,14,0,3,9,11]
    s1 = [11,14,15,12,5,8,7,9,11,13,14,15,6,7,9,8,7,6,8,13,11,9,7,15,7,12,15,9,11,7,13,12,11,13,6,7,14,9,13,15,14,8,13,6,5,12,7,5,11,12,14,15,14,15,9,8,9,14,5,6,8,6,5,12,9,15,5,11,6,8,13,12,5,12,13,14,11,8,5,6]
    s2 = [8,9,9,11,13,15,15,5,7,7,8,11,14,14,12,6,9,13,15,7,12,8,9,11,7,7,12,7,6,15,13,11,9,7,15,11,8,6,6,14,12,13,5,14,13,13,7,5,15,5,8,11,14,14,6,14,6,9,12,9,12,5,15,8,8,5,12,9,12,5,14,6,8,13,6,5,15,13,11,11]
    K1 = [0, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    K2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0]
    f = [lambda x,y,z: x^y^z, lambda x,y,z: (x&y)|(~x&z), lambda x,y,z: (x|~y)^z, lambda x,y,z: (x&z)|(y&~z), lambda x,y,z: x^(y|~z)]
    m = bytearray(msg) + b"\x80"
    m += b"\0" * ((56 - len(m)) % 64) + (len(msg) * 8).to_bytes(8, "little")
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    for o in range(0, len(m), 64):
        X = [int.from_bytes(m[o+4*i:o+4*i+4], "little") for i in range(16)]
        a, b, c, d, e = h; a2, b2, c2, d2, e2 = h
        for j in range(80):
            r = j // 16
            t = (rol((a + f[r](b, c, d) + X[r1[j]] + K1[r]) & 0xFFFFFFFF, s1[j]) + e) & 0xFFFFFFFF
            a, e, d, c, b = e, d, rol(c, 10), b, t
            t = (rol((a2 + f[4-r](b2, c2, d2) + X[r2[j]] + K2[r]) & 0xFFFFFFFF, s2[j]) + e2) & 0xFFFFFFFF
            a2, e2, d2, c2, b2 = e2, d2, rol(c2, 10), b2, t
        t = (h[1] + c + d2) & 0xFFFFFFFF
        h = [t, (h[2]+d+e2)&0xFFFFFFFF, (h[3]+e+a2)&0xFFFFFFFF, (h[4]+a+b2)&0xFFFFFFFF, (h[0]+b+c2)&0xFFFFFFFF]
    return b"".join(x.to_bytes(4, "little") for x in h)


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def _bech32_addr(prog, hrp="bc"):
    bits, acc, data = 0, 0, [0]
    for byte in prog:
        acc, bits = (acc << 8) | byte, bits + 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits: data.append((acc << (5 - bits)) & 31)
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(exp + data + [0] * 6) ^ 1
    return hrp + "1" + "".join(_B32[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def parse_keys(text):
    """All keys in the text (one per wallet), each as (pubkey point, chain code)."""
    found = re.findall(r"[xyz]pub[1-9A-HJ-NP-Za-km-z]{100,112}", text)
    if not found: raise ValueError("לא נמצא xpub/zpub")
    return [parse_key(f) for f in dict.fromkeys(found)]


def parse_key(text):
    """Returns (pubkey point, chain code) from an xpub/zpub or a descriptor that contains one."""
    m = re.search(r"[xyz]pub[1-9A-HJ-NP-Za-km-z]{100,112}", text)
    if not m: raise ValueError("לא נמצא xpub/zpub")
    body = _b58check(m.group(0))
    chain, key = body[13:45], body[45:78]
    x = int.from_bytes(key[1:], "big")
    y = pow((x ** 3 + 7) % _P, (_P + 1) // 4, _P)
    if (y & 1) != (key[0] & 1): y = _P - y
    return (x, y), chain


def _ckd(pt, chain, i):
    ser = (b"\x02" if pt[1] % 2 == 0 else b"\x03") + pt[0].to_bytes(32, "big")
    I = hmac.new(chain, ser + i.to_bytes(4, "big"), hashlib.sha512).digest()
    il = int.from_bytes(I[:32], "big")
    return _add(_mul(il), pt), I[32:]


def address(key, branch, index):
    """key from parse_key; branch 0 = receiving, 1 = change."""
    pt, chain = key
    pt, chain = _ckd(pt, chain, branch)
    pt, _ = _ckd(pt, chain, index)
    comp = (b"\x02" if pt[1] % 2 == 0 else b"\x03") + pt[0].to_bytes(32, "big")
    return _bech32_addr(_ripemd160(hashlib.sha256(comp).digest()))
