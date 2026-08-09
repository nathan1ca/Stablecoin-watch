"""
keccak-256 (이더리움 방식) 순수 파이썬 구현.

이벤트 시그니처 문자열에서 topic0을 직접 계산하기 위한 것이다.
pycryptodome 같은 외부 패키지를 끌어오지 않으려고 넣었다.

검증: keccak256("Transfer(address,address,uint256)") 는
0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef 여야 한다.
python etl/keccak.py 로 자가검증할 수 있다.
"""

MASK = (1 << 64) - 1

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

RATE = 136  # keccak-256 의 rate (바이트)


def _rol(v: int, n: int) -> int:
    n %= 64
    return ((v << n) | (v >> (64 - n))) & MASK


def _f(A):
    for rnd in range(24):
        # theta
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                A[x][y] ^= D[x]
        # rho + pi
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                A[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & MASK & B[(x + 2) % 5][y])
        # iota
        A[0][0] ^= _RC[rnd]
    return A


def keccak256(data: bytes) -> bytes:
    # 패딩: keccak 원본 방식(0x01 … 0x80). SHA3-256(0x06)과 다르다.
    p = bytearray(data)
    p.append(0x01)
    while len(p) % RATE != 0:
        p.append(0x00)
    p[-1] |= 0x80

    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), RATE):
        block = p[off:off + RATE]
        for i in range(RATE // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            A[i % 5][i // 5] ^= lane
        _f(A)

    out = bytearray()
    for i in range(4):  # 32바이트만 필요
        out += A[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


def topic0(signature: str) -> str:
    """'Transfer(address,address,uint256)' → '0xddf252ad…'"""
    return "0x" + keccak256(signature.encode()).hex()


if __name__ == "__main__":
    checks = {
        "Transfer(address,address,uint256)":
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        "Approval(address,address,uint256)":
            "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
    }
    ok = True
    for sig, want in checks.items():
        got = topic0(sig)
        mark = "OK " if got == want else "실패"
        ok &= got == want
        print(f"{mark} {sig}\n     {got}")
    raise SystemExit(0 if ok else 1)
