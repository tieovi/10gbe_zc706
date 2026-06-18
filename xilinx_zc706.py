#!/usr/bin/env python3

import os

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2024 Gwenhael Goavec-Merou <gwenhael@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

# Build/use
# Build/Load bitstream:
# ./xilinx_zc7006.py --with-etherbone --uart-name=crossover --csr-csv=csr.csv --build --load
#
# Test Ethernet:
# ping 192.168.1.50
#
# Test Console:
# litex_server --udp
# litex_term crossover
#
#
# Build/Load bitstream:
# ./xilinx_zc706.py --with-jtagbone --uart-name=crossover --csr-csv=csr.csv --builde --load
#
# litex_server --jtag --jtag-config openocd_xc7z_smt2-nc.cfg
#
# In a second terminal:
# litex_cli --regs # to dump all registers
# Or
# litex_term crossover # to have access to LiteX bios
#
# --------------------------------------------------------------------------------------------------

from migen import *

from litex.gen import *
from migen.genlib.cdc import PulseSynchronizer, MultiReg

from litex_boards.platforms import xilinx_zc706

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.builder import *
from litex.soc.cores.led import LedChaser
from litex.soc.cores.bitbang import I2CMaster
from litex.soc.cores.freqmeter import FreqMeter

from litedram.modules import MT8JTF12864
from litedram.phy import s7ddrphy

from liteeth.phy.k7_1000basex import K7_1000BASEX
from liteeth.phy.xgmii import LiteEthPHYXGMII
from liteeth.core import LiteEthUDPIPCore
from liteeth.common import convert_ip

from litepcie.phy.s7pciephy import S7PCIEPHY
from litepcie.software import generate_litepcie_software

from litex.soc.interconnect.csr import *
# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.rst       = Signal()
        self.cd_sys    = ClockDomain()
        self.cd_sys4x  = ClockDomain()
        self.cd_idelay = ClockDomain()
        self.cd_eth    = ClockDomain()

        # # #

        # Clk/Rst.
        clk200 = platform.request("clk200")

        # PLL.
        self.pll = pll = S7PLL(speedgrade=-1)
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(clk200, 200e6)
        pll.create_clkout(self.cd_sys,    sys_clk_freq)
        pll.create_clkout(self.cd_sys4x,  4*sys_clk_freq)
        pll.create_clkout(self.cd_idelay, 200e6)
        pll.create_clkout(self.cd_eth,    200e6)
        # Ignore sys_clk -> PLL reset path created by SoC's rst without
        # declaring the related PLL clocks asynchronous.
        platform.add_platform_command("set_false_path -quiet -to [get_pins -quiet FDCE/D]")
        platform.add_platform_command("set_false_path -quiet -to [get_pins -quiet PLLE2_ADV/RST]")
        platform.add_platform_command(
            "set_false_path -quiet "
            "-to [get_pins -quiet -filter {{REF_PIN_NAME == D}} "
            "-of_objects [get_cells -quiet -hierarchical -filter {{mr_ff == TRUE}}]]"
        )

        # IDelayCtrl.
        self.idelayctrl = S7IDELAYCTRL(self.cd_idelay)

# XilinxXGMII -----------------------------------------------------------------------------------------

class XilinxXGMII(Module, AutoCSR):
    """10GbE PHY via Xilinx PCS/PMA (pg068-ten-gig-eth-pcs-pma.pdf)"""

    def __init__(self, cd_sys, platform, dw=64):
        self._reset = CSRStorage(reset=0)
        self._pcs_status = CSRStatus(
            fields=[
                CSRField("pcs_fault", size=1, offset=1),
                CSRField("pcs_fault_rx", size=1, offset=2),
                CSRField("pcs_fault_tx", size=1, offset=3),
            ]
        )
        self._core_status = CSRStatus(
            fields=[
                CSRField("pcs_block_lock", size=1, offset=0),
                CSRField("fec_signal_ok", size=1, offset=1),
                CSRField("pmd_signal_detect", size=1, offset=2),
                CSRField("an_complete", size=1, offset=3),
                CSRField("an_enable", size=1, offset=4),
                CSRField("an_link_up", size=1, offset=5),
            ]
        )
        self._pcs_config = CSRStorage(
            reset=0,
            fields=[
                CSRField("pcs_clear", size=1, offset=0, pulse=True),
            ],
        )

        self.clock_domains.cd_clkmgt = ClockDomain()

        self.tx_data = Signal(dw)
        self.tx_ctl = Signal(dw // 8)
        self.rx_data = Signal(dw)
        self.rx_ctl = Signal(dw // 8)
        self.pma_status = Signal(448)
        _pma_status = Signal(448)
        self.core_status = Signal(8)
        core_status_sync = Signal(6)

        txusrclk = Signal()
        txusrclk2 = Signal()
        drp_req = Signal()
        drp_daddr_o = Signal(16)
        drp_den_o = Signal()
        drp_di_o = Signal(16)
        drp_dwe_o = Signal()
        drp_drpdo_i = Signal(16)
        drp_drdy_i = Signal()

        self.sfp_tx_disable_n_pad = platform.request("sfp_tx_disable_n", loose=True)

        refclk_pads = platform.request("refclk")
        rx_pads = platform.request("sfp_rx")
        tx_pads = platform.request("sfp_tx")
        self.tx_disable = Signal()
        self.qplllock = Signal()
        self.gtrxreset = Signal()
        self.gttxreset = Signal()
        self.txusrrdy = Signal()
        self.coreclk = Signal()

        self.pcs_clear = pcs_clear = Signal()
        config_vector = Signal(536, reset=0)
        self.submodules.ps = PulseSynchronizer("sys", "clkmgt")
        self.pma_multi = pma_multi = Signal(3)
        self.specials += MultiReg(
            Cat(self.pma_status[250:252], self.pma_status[231]), pma_multi
        )
        self.specials += MultiReg(self.core_status, core_status_sync)
        self.comb += [
            self.ps.i.eq(self._pcs_config.fields.pcs_clear),
            pcs_clear.eq(self.ps.o),
            self._pcs_status.fields.pcs_fault_rx.eq(pma_multi[0]),
            self._pcs_status.fields.pcs_fault_tx.eq(pma_multi[1]),
            self._pcs_status.fields.pcs_fault.eq(pma_multi[2]),
            self._core_status.fields.pcs_block_lock.eq(core_status_sync[0]),
            self._core_status.fields.fec_signal_ok.eq(core_status_sync[1]),
            self._core_status.fields.pmd_signal_detect.eq(core_status_sync[2]),
            self._core_status.fields.an_complete.eq(core_status_sync[3]),
            self._core_status.fields.an_enable.eq(core_status_sync[4]),
            self._core_status.fields.an_link_up.eq(core_status_sync[5]),
        ]

        self.comb += [ClockSignal("clkmgt").eq(self.coreclk)]
        self.sync.clkmgt += [
            config_vector[517].eq(pcs_clear),
            self.pma_status.eq(_pma_status),
        ]

        self.specials += Instance(
            "ten_gig_eth_pcs_pma_0",
            i_refclk_p=refclk_pads.p,
            i_refclk_n=refclk_pads.n,
            i_reset=self._reset.storage,
            o_coreclk_out=self.coreclk,
            i_rxp=rx_pads.p,
            i_rxn=rx_pads.n,
            o_txp=tx_pads.p,
            o_txn=tx_pads.n,
            i_dclk=cd_sys.clk,
            i_sim_speedup_control=0,
            o_txusrclk_out=txusrclk,
            o_txusrclk2_out=txusrclk2,
            o_qplllock_out=self.qplllock,
            o_txuserrdy_out=self.txusrrdy,
            o_gttxreset_out=self.gttxreset,
            o_gtrxreset_out=self.gtrxreset,
            o_xgmii_rxd=self.rx_data,
            o_xgmii_rxc=self.rx_ctl,
            i_xgmii_txd=self.tx_data,
            i_xgmii_txc=self.tx_ctl,
            i_configuration_vector=config_vector,
            o_status_vector=_pma_status,
            o_core_status=self.core_status,
            i_signal_detect=1,
            i_tx_fault=0,
            o_tx_disable=self.tx_disable,
            i_pma_pmd_type=0b101,
            o_drp_req=drp_req,
            i_drp_gnt=drp_req,
            o_drp_daddr_o=drp_daddr_o,
            o_drp_den_o=drp_den_o,
            o_drp_di_o=drp_di_o,
            o_drp_dwe_o=drp_dwe_o,
            i_drp_drpdo_i=drp_drpdo_i,
            i_drp_drdy_i=drp_drdy_i,
            i_drp_daddr_i=drp_daddr_o,
            i_drp_den_i=drp_den_o,
            i_drp_di_i=drp_di_o,
            i_drp_dwe_i=drp_dwe_o,
            o_drp_drpdo_o=drp_drpdo_i,
            o_drp_drdy_o=drp_drdy_i,
        )

        if self.sfp_tx_disable_n_pad is not None:
            self.comb += self.sfp_tx_disable_n_pad.eq(~self.tx_disable)

        class Pads:
            rx = ClockSignal("clkmgt")
            rx_ctl = self.rx_ctl
            rx_data = self.rx_data
            tx = ClockSignal("clkmgt")
            tx_ctl = self.tx_ctl
            tx_data = self.tx_data

        self.pads = Pads()

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=125e6,
        with_ethernet   = False,
        with_etherbone  = False,
        with_10g        = False,
        eth_ip          = "10.1.0.3",
        remote_ip       = "10.1.0.4",
        eth_dynamic_ip  = False,
        with_led_chaser = True,
        with_pcie       = False,
        **kwargs):
        platform = xilinx_zc706.Platform()

        # When nor jtagbone, nor etherbone are set forces jtagbone.
        kwargs["uart_name"]     = "crossover"
        if not (kwargs["with_jtagbone"] or with_etherbone):
            kwargs["with_jtagbone"] = True

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq)

        # SoCCore ----------------------------------------------------------------------------------
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on ZC706", **kwargs)

        # DDR3 SDRAM -------------------------------------------------------------------------------
        if not self.integrated_main_ram_size:
            self.ddrphy = s7ddrphy.K7DDRPHY(platform.request("ddram"),
                memtype      = "DDR3",
                nphases      = 4,
                sys_clk_freq = sys_clk_freq)
            self.add_sdram("sdram",
                phy           = self.ddrphy,
                module        = MT8JTF12864(sys_clk_freq, "1:4"),
                l2_cache_size = kwargs.get("l2_size", 8192)
            )

        # Ethernet / Etherbone / 10G -------------------------------------------------------------------
        if with_10g:
            self.submodules.xgmii = XilinxXGMII(self.crg.cd_sys, platform)
            self.add_csr("xgmii")
            #platform.toolchain.pre_placement_commands.add(
            #    "set_clock_groups "
            #    "-group [get_clocks -include_generated_clocks -of [get_nets {sys_clk}]] "
            #    "-group [get_clocks -include_generated_clocks refclk_p] "
            #    "-asynchronous",
            #    sys_clk=self.crg.cd_sys.clk,
            #)
            self.submodules.xgmiiphy = ClockDomainsRenamer("clkmgt")(
                LiteEthPHYXGMII(self.xgmii.pads, self.xgmii.pads)
            )
            self.add_csr("xgmiiphy")

            # Add to CPU.
            # The PHY's eth_tx/eth_rx clock domains are both driven by clkmgt
            # (156.25 MHz coreclk) via the XilinxXGMII Pads. data_width=64 keeps
            # with_sys_datapath off, so add_ethernet maps the MAC's eth_tx/eth_rx
            # datapath onto those clkmgt domains while the wishbone SRAM slots
            # stay in sys (125 MHz) for VexRiscv. The MAC core does the
            # clkmgt<->sys CDC internally, so the CPU lwIP stack can ping-pong.
            #
            # with_timing_constraints=False: coreclk/clkmgt is already a clock
            # defined by the PCS/PMA IP. Letting add_ethernet add its own period
            # constraint on that net creates a conflicting/duplicate clock, so we
            # disable it and group the two domains as asynchronous below.
            self.add_ethernet(
                phy                     = self.xgmiiphy,
                data_width              = 64,
                with_timing_constraints = False,
                dynamic_ip              = False,
                local_ip                = eth_ip,
                remote_ip               = remote_ip,
                mac_address             = 0xaa1233445566,
            )
            platform.add_ip("ip/ten_gig_eth_pcs_pma_0.xci")
            # The MAC core crosses sys (125 MHz) <-> the PHY datapath inside
            # async (gray-coded) CDC FIFOs, so the two domains must be declared
            # asynchronous. The PCS/PMA IP creates the refclk_p clock from its
            # scoped XDC after the top XDC is parsed, so emit this constraint as
            # a pre-placement Tcl command instead of a top-level XDC false path.
            platform.toolchain.pre_placement_commands.append("\n".join([
                "set sys_clks [get_clocks -quiet crg_clkout0]",
                "set eth_clks [get_clocks -quiet {{refclk_p *RXOUTCLK *TXOUTCLK}}]",
                "if {{[llength $sys_clks] == 0}} {{error {{No crg_clkout0 clock found for async group}}}}",
                "if {{[llength [get_clocks -quiet refclk_p]] == 0}} {{error {{No refclk_p clock found for async group}}}}",
                "set_clock_groups -asynchronous -group $sys_clks -group $eth_clks",
            ]))
        elif with_ethernet or with_etherbone:
            self.ethphy = K7_1000BASEX(
                refclk_or_clk_pads = self.crg.cd_eth.clk,
                data_pads          = self.platform.request("sfp", 0),
                sys_clk_freq       = self.clk_freq,
                with_csr           = False
            )
            #self.comb += self.platform.request("sfp_tx_disable_n", 0).eq(1) Controlled by PG068
            platform.add_platform_command("set_property SEVERITY {{Warning}} [get_drc_checks REQP-52]")
            if with_etherbone:
                self.add_etherbone(phy=self.ethphy, ip_address=eth_ip, with_ethmac=with_ethernet)
            #elif with_ethernet:
            #    self.add_ethernet(phy=self.ethphy, dynamic_ip=eth_dynamic_ip, local_ip=eth_ip, remote_ip=remote_ip)

        # PCIe -------------------------------------------------------------------------------------
        if with_pcie:
            self.pcie_phy = S7PCIEPHY(platform, platform.request("pcie_x4"),
                data_width = 128,
                bar0_size  = 0x20000)
            self.add_pcie(phy=self.pcie_phy, ndmas=1)

        # Leds -------------------------------------------------------------------------------------
        if with_led_chaser:
            self.leds = LedChaser(
                pads         = platform.request_all("user_led"),
                sys_clk_freq = sys_clk_freq
            )

        # I2C --------------------------------------------------------------------------------------
        self.i2c = I2CMaster(platform.request("i2c"))

# DevSoC -------------------------------------------------------------------------------------------

class DevSoC(BaseSoC):
    """BaseSoC with optional LiteScopeAnalyzer for 10G debugging."""

    def __init__(self, with_analyzer=False, **kwargs):
        super().__init__(**kwargs)

        # Add analyzer for 10G mode with optional logic analyzer
        if kwargs.get("with_10g", False) and with_analyzer:
            from litescope import LiteScopeAnalyzer

            core_status = Signal(8)
            tx_disable  = Signal()
            qplllock    = Signal()
            gtrxreset   = Signal()
            gttxreset   = Signal()
            txusrrdy    = Signal()
            pma_multi   = Signal(3)

            self.specials += [
                MultiReg(self.xgmii.core_status, core_status, odomain="clkmgt"),
                MultiReg(self.xgmii.tx_disable,  tx_disable,  odomain="clkmgt"),
                MultiReg(self.xgmii.qplllock,    qplllock,    odomain="clkmgt"),
                MultiReg(self.xgmii.gtrxreset,   gtrxreset,   odomain="clkmgt"),
                MultiReg(self.xgmii.gttxreset,   gttxreset,   odomain="clkmgt"),
                MultiReg(self.xgmii.txusrrdy,    txusrrdy,    odomain="clkmgt"),
            ]
            self.sync.clkmgt += pma_multi.eq(Cat(self.xgmii.pma_status[250:252], self.xgmii.pma_status[231]))

            # All probes sample in the clkmgt (156.25 MHz) domain — that is where
            # the PHY datapath lives, so this shows whether frames physically
            # arrive from the wire (e.g. an ARP request) and whether the MAC
            # drives a reply back out (ARP reply / ping echo).
            analyzer_signals = [
                # RX: frames coming in from the wire into the MAC.
                self.xgmiiphy.rx.source.valid,
                self.xgmiiphy.rx.source.ready,
                self.xgmiiphy.rx.source.last,
                self.xgmiiphy.rx.source.last_be,
                self.xgmiiphy.rx.source.data,
                # TX: frames the MAC sends back out to the wire.
                self.xgmiiphy.tx.sink.valid,
                self.xgmiiphy.tx.sink.ready,
                self.xgmiiphy.tx.sink.last,
                self.xgmiiphy.tx.sink.last_be,
                self.xgmiiphy.tx.sink.data,
                # PHY / PCS-PMA link status.
                core_status,   # bit0 = PCS block lock
                tx_disable,
                qplllock,
                gtrxreset,
                gttxreset,
                txusrrdy,
                self.xgmii.pcs_clear,
                pma_multi,
            ]
            self.submodules.analyzer = LiteScopeAnalyzer(
                analyzer_signals, 1024, clock_domain="clkmgt", register=True, csr_csv="analyzer.csv"
            )
            self.add_csr("analyzer")

# Build --------------------------------------------------------------------------------------------
def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=xilinx_zc706.Platform, description="LiteX SoC on ZC706.")
    parser.add_target_argument("--sys-clk-freq",   default=125e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--programmer",     default="vivado",          help="Programmer select from Vivado/openFPGALoader.")
    parser.add_target_argument("--with-ethernet",  action="store_true",       help="Enable 1GbE support.")
    parser.add_target_argument("--with-etherbone", action="store_true",       help="Enable Etherbone support.")
    parser.add_target_argument("--with-10g",       action="store_true",       help="Enable 10GbE support.")
    parser.add_target_argument("--with-analyzer",  action="store_true",       help="Enable logic analyzer (requires 10G mode).")
    parser.add_target_argument("--eth-ip",         default="10.1.0.3",        help="Ethernet/Etherbone IP address.")
    parser.add_target_argument("--remote-ip",      default="10.1.0.4",        help="Remote IP address of TFTP server.")
    parser.add_target_argument("--eth-dynamic-ip", action="store_true",       help="Enable dynamic Ethernet IP addresses setting.")
    parser.add_target_argument("--with-pcie",      action="store_true",       help="Enable PCIe support.")
    parser.add_target_argument("--driver",         action="store_true",       help="Generate PCIe driver.")
    args = parser.parse_args()

    soc = DevSoC(
        sys_clk_freq   = args.sys_clk_freq,
        with_ethernet  = args.with_ethernet,
        with_etherbone = args.with_etherbone,
        with_10g       = args.with_10g,
        with_analyzer  = args.with_analyzer,
        eth_ip         = args.eth_ip,
        remote_ip      = args.remote_ip,
        eth_dynamic_ip = args.eth_dynamic_ip,
        with_pcie      = args.with_pcie,
        **parser.soc_argdict
    )
    builder = Builder(soc, **parser.builder_argdict)
    if args.build:
        builder.build(**parser.toolchain_argdict)

    if args.driver:
        generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:
        prog = soc.platform.create_programmer(args.programmer)
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"), device=1)

if __name__ == "__main__":
    main()
