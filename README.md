# 10GbE on ZC706 with LiteEth

Open-source 10G Ethernet on the Xilinx ZC706 using [LiteEth](https://github.com/enjoy-digital/liteeth) (BSD-licensed MAC) + Xilinx PG068 BASER (free license PCS/PMA). No eval IP — fully deployable.

See the companion article: **[Replacing the Xilinx Eval MAC with Open-Source LiteEth](https://tieovi.github.io/posts/zc706-10g-liteeth)**

---

## Hardware

- **Xilinx ZC706** evaluation board
- **SFP+ cage** (J32) with an SFP+ transceiver module
- LC fiber or DAC cable to a 10GbE NIC on the host PC
- **Si5324** on-board clock generator (provides 156.25 MHz refclk to PG068)

No external clock generator needed — the ZC706's on-board Si5324 is configured over I²C at runtime.

---

## Stack

```
Application (UDP port 3000)
    │
LiteEthUDPIPCore   (UDP / IP / ARP / ICMP)   ← BSD license
LiteEthMACCore     (MAC)                      ← BSD license
LiteEthPHYXGMII    (Reconciliation Sublayer)  ← BSD license
    │ XGMII (64-bit data + 8-bit ctl @ 156.25 MHz)
PG068 ten_gig_eth_pcs_pma_0  (PCS/PMA)        ← Xilinx free license
    │ SFP+ differential pair
Network
```

All Ethernet datapath logic runs in the `clkmgt` clock domain (156.25 MHz from PG068 `coreclk_out`). The VexRISCV CPU runs in `sys` (125 MHz) and only touches Ethernet through CSRs.

---

## Quick Start

### 1. Build and load bitstream

```bash
./10g_zc706.py --build --load
```

### 2. Start the JTAG server

```bash
litex_server --jtag --jtag-config prog/openocd_xc7z_smt2-nc.cfg
```

### 3. Configure the Si5324 clock (156.25 MHz)

**Option A — Python script (automated):**
```bash
python3 clock_init.py
```

**Option B — BIOS console (manual):**
```bash
litex_term crossover   # open BIOS prompt
```
Then type the I²C sequence (bus switch first, then Si5324 registers):
```
litex> i2c_write 0x74 0x10 1 0x10
litex> i2c_write 0x68 0x00 1 0x54
litex> i2c_write 0x68 0x01 1 0xe4
... (see clock_init.py for the full SI5324_REGS dict)
litex> i2c_write 0x68 0x88 1 0x40
```

### 4. Verify link is up

In the BIOS console:
```
litex> xgmii_core_status
xgmii_core_status = 0x00000001
```
`bit[0] = 1` means PCS block lock — the SERDES is locked to the 156.25 MHz clock and the link is up.

Or with Python:
```bash
python3 clock_init.py   # prints "LOCKED" or "NOT LOCKED" at the end
```

---

## LED Indicators

| LED | Clock domain | Meaning |
|-----|-------------|---------|
| LED[0] | `sys` 125 MHz | SoC running (always blinks after bitstream load) |
| LED[2] | `clkmgt` 156.25 MHz | Si5324 locked + PG068 clock running |

If LED[2] is dark after Si5324 config, the clock is not locked — re-run `clock_init.py`.

---

## Receiving UDP Packets on the Host

The board continuously sends 64-byte UDP packets from `10.1.0.3:3000` to `<host>:7778`.

```bash
nc -u -l 7778 | xxd
```

You should see `0xC0FFEEC1FFEE` magic bytes followed by a decrementing counter (8 words per packet).

---

## Files

| File | Purpose |
|------|---------|
| `10g_zc706.py` | LiteX SoC definition — `XilinxXGMII`, `TenGbeTestSoC`, `DevSoC` |
| `clock_init.py` | Si5324 configuration via JTAGBone (bit-banging I²C over Wishbone) |
| `ip/ten_gig_eth_pcs_pma_0.xci` | Vivado IP configuration for ZC706 (Kintex-7 GTX) |
| `ip/ten_gig_eth_pcs_pma_0.xci.kintex` | Alternative XCI for Kintex KC705 target |
| `prog/openocd_xc7z_smt2-nc.cfg` | OpenOCD config for Digilent SMT2 JTAG probe |

---

## Xilinx IP

`ip/ten_gig_eth_pcs_pma_0.xci` — Vivado 2024.1, Kintex-7 GTX, shared logic inside core.

PG068 (BASER mode) is a **free license** — no eval restriction.

---

## Related

- **Article 1** — [10G Ethernet on ZC706: One Board, One PC](https://tieovi.github.io/posts/zc706-10g-one-board) (PG157 eval IP baseline)
- **Article 2** — [Replacing the Xilinx Eval MAC with Open-Source LiteEth](https://tieovi.github.io/posts/zc706-10g-liteeth) (this repo)
