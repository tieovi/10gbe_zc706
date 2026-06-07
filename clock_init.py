#!/usr/bin/env python3
"""
SI5324 Clock Generator Configuration for 156.25MHz 10G Ethernet Reference Clock

Usage:
    # Terminal 1:
    ./10g_zc706.py --build --load
    litex_server --jtag --jtag-config openocd_xc7z_smt2-nc.cfg

    # Terminal 2:
    python3 clock_init.py
"""

import time
import sys
import os
from litex import RemoteClient

I2C_BUS_SWITCH = 0x74
I2C_SI5324 = 0x68
I2C_BUS_SWITCH_DIR = (1 << 4)

# SI5324 registers for 156.25MHz
SI5324_REGS = {
    0x00: 0x54, 0x01: 0xE4, 0x02: 0x12, 0x03: 0x15, 0x04: 0x92,
    0x0A: 0x08, 0x0B: 0x40, 0x19: 0xA0, 0x1F: 0x00, 0x20: 0x00,
    0x21: 0x03, 0x28: 0xC2, 0x29: 0x49, 0x2A: 0xEF, 0x2B: 0x00,
    0x2C: 0x77, 0x2D: 0x0B, 0x2E: 0x00, 0x2F: 0x77, 0x30: 0x0B,
    0x88: 0x40,
}

# LiteX I2C bit-bang register bit positions (litex/soc/cores/bitbang.py)
# i2c_w: bit0=SCL, bit1=OE, bit2=SDA_out
# i2c_r: bit0=SDA_in
_SCL = 1 << 0
_OE  = 1 << 1
_SDA = 1 << 2

# Slow enough to be reliable over JTAG wishbone (~5 kHz)
_BIT_DELAY = 0.0001


# ---------------------------------------------------------------------------
# I2C bit-banging primitives
# ---------------------------------------------------------------------------

def _drive(bus, w, scl, sda):
    """Drive SCL and SDA; OE always set so the core actually outputs."""
    bus.write(w, _OE | (scl * _SCL) | (sda * _SDA))
    time.sleep(_BIT_DELAY)


def _start(bus, w):
    _drive(bus, w, 1, 1)   # idle
    _drive(bus, w, 1, 0)   # SDA low while SCL high → START condition
    _drive(bus, w, 0, 0)   # SCL low


def _stop(bus, w):
    _drive(bus, w, 0, 0)
    _drive(bus, w, 1, 0)   # SCL high, SDA still low
    _drive(bus, w, 1, 1)   # SDA high while SCL high → STOP condition


def _write_byte(bus, w, r, byte):
    """Clock out 8 bits MSB-first, then read ACK. Returns True on ACK."""
    for i in range(7, -1, -1):
        sda = (byte >> i) & 1
        _drive(bus, w, 0, sda)   # SCL low, set data
        _drive(bus, w, 1, sda)   # SCL high (slave samples here)
        _drive(bus, w, 0, sda)   # SCL low
    # ACK cycle: release SDA (drive high, slave pulls low for ACK)
    _drive(bus, w, 0, 1)
    _drive(bus, w, 1, 1)         # SCL high
    ack = (bus.read(r) & 1) == 0  # SDA_in=0 means ACK
    _drive(bus, w, 0, 1)         # SCL low
    return ack


# ---------------------------------------------------------------------------
# Higher-level I2C transactions
# ---------------------------------------------------------------------------

def i2c_write(bus, i2c_base, slave_addr, reg_addr, value):
    """Write one byte to a register: START, addr+W, reg, value, STOP."""
    w = i2c_base + 0x00
    r = i2c_base + 0x04
    _start(bus, w)
    ok  = _write_byte(bus, w, r, slave_addr << 1)  # W bit = 0
    ok &= _write_byte(bus, w, r, reg_addr)
    ok &= _write_byte(bus, w, r, value)
    _stop(bus, w)
    if not ok:
        print(f"  WARNING: NACK from 0x{slave_addr:02x} reg 0x{reg_addr:02x}")
    return ok


def i2c_write_bus_switch(bus, i2c_base, slave_addr, channel):
    """PCA9548 has no register address — write single channel-select byte."""
    w = i2c_base + 0x00
    r = i2c_base + 0x04
    _start(bus, w)
    ok  = _write_byte(bus, w, r, slave_addr << 1)
    ok &= _write_byte(bus, w, r, channel)
    _stop(bus, w)
    if not ok:
        print(f"  WARNING: NACK from bus switch 0x{slave_addr:02x}")
    return ok


# ---------------------------------------------------------------------------
# CSR helpers
# ---------------------------------------------------------------------------

def get_csr_addr_from_csv(csr_file, csr_name):
    """Extract CSR register address from csr.csv."""
    if not os.path.exists(csr_file):
        return None
    try:
        with open(csr_file, 'r') as f:
            for line in f:
                parts = line.strip().split(',')
                if len(parts) >= 3 and parts[0] == "csr_register" and parts[1] == csr_name:
                    return int(parts[2], 16)
    except Exception:
        pass
    return None


def get_i2c_base_from_csr(csr_file="csr.csv"):
    """Extract I2C CSR base address from csr.csv."""
    if not os.path.exists(csr_file):
        return 0x3800
    try:
        with open(csr_file, 'r') as f:
            for line in f:
                if line.startswith("csr_base,i2c,"):
                    return int(line.split(',')[2], 16)
    except Exception:
        pass
    return 0x3800


# ---------------------------------------------------------------------------
# SI5324 configuration
# ---------------------------------------------------------------------------

def configure_si5324(bus, i2c_base, csr_file="csr.csv"):
    print("\nConfiguring I2C Bus Switch (PCA9548 @ 0x74, channel 4)...")
    ok = i2c_write_bus_switch(bus, i2c_base, I2C_BUS_SWITCH, I2C_BUS_SWITCH_DIR)
    if ok:
        print("  Bus switch configured")
    time.sleep(0.05)

    print("\nConfiguring SI5324 for 156.25MHz...")
    nack_count = 0
    for reg_addr, reg_value in SI5324_REGS.items():
        ok = i2c_write(bus, i2c_base, I2C_SI5324, reg_addr, reg_value)
        status = "OK" if ok else "NACK"
        print(f"  reg 0x{reg_addr:02x} = 0x{reg_value:02x}  [{status}]")
        if not ok:
            nack_count += 1

    if nack_count:
        print(f"\n  WARNING: {nack_count} NACK(s) — check bus switch channel and SI5324 address")
    else:
        print("\n  SI5324 write complete (all ACKed)")

    # SI5324 datasheet: allow up to 2s for DSPLL to lock after config
    print("\nWaiting 2s for SI5324 PLL to lock...")
    time.sleep(2.0)

    xgmii_core_status_addr = get_csr_addr_from_csv(csr_file, "xgmii_core_status")
    if xgmii_core_status_addr is not None:
        try:
            core_status = bus.read(xgmii_core_status_addr)
            pcs_block_lock = (core_status >> 0) & 0x1

            print(f"\nStatus (0x{xgmii_core_status_addr:08x}): 0x{core_status:08x}")
            print(f"PCS Block Lock: {'LOCKED' if pcs_block_lock else 'NOT LOCKED'}")

            if not pcs_block_lock:
                print("\n  Check: SI5324 power, refclk cable, SFP module inserted")
        except Exception as e:
            print(f"\n  Could not read status: {e}")
    else:
        print("\n  Could not find xgmii_core_status in csr.csv — check manually")


def main():
    print("=" * 70)
    print("SI5324 Clock Generator Configuration (156.25MHz for 10G Ethernet)")
    print("=" * 70)

    i2c_base = get_i2c_base_from_csr("csr.csv")
    print(f"I2C CSR base: 0x{i2c_base:08x}  (i2c_w=+0x00, i2c_r=+0x04)")

    try:
        bus = RemoteClient()
        bus.open()
        print(f"Connected to litex_server at localhost:1234")
    except Exception as e:
        print(f"Failed to connect: {e}")
        print("\nStart litex_server first:")
        print("  litex_server --jtag --jtag-config openocd_xc7z_smt2-nc.cfg")
        sys.exit(1)

    try:
        configure_si5324(bus, i2c_base, "csr.csv")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
