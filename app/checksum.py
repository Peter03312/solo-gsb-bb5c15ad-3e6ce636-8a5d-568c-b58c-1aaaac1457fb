"""Checksum algorithms for industrial message validation."""

from typing import Callable, Dict


def sum8(data: bytes) -> int:
    """8-bit additive checksum."""
    return sum(data) & 0xFF


def xor8(data: bytes) -> int:
    """8-bit XOR checksum."""
    result = 0
    for b in data:
        result ^= b
    return result


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def crc16_modbus(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/MODBUS: reflected poly 0xA001, init 0xFFFF."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# algorithm name -> (function, checksum width in bits)
ALGORITHMS: Dict[str, tuple[Callable[[bytes], int], int]] = {
    "sum8": (sum8, 8),
    "xor8": (xor8, 8),
    "crc16-ccitt": (crc16_ccitt, 16),
    "crc16-modbus": (crc16_modbus, 16),
}


def compute(algorithm: str, data: bytes) -> int:
    fn, _ = ALGORITHMS[algorithm]
    return fn(data)
