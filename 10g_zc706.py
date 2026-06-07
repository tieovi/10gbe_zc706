#!/usr/bin/env python3

import os

from migen import Module, Instance, Cat, Memory, Signal, ClockDomain
from migen.genlib.cdc import PulseSynchronizer, MultiReg
from migen.genlib.misc import WaitTimer

from litex.soc.cores.freqmeter import FreqMeter

# from litex_boards.targets.xilinx_kc705 import BaseSoC
from litex_boards.targets.xilinx_zc706 import BaseSoC

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.builder import builder_args, builder_argdict, Builder
from litex.soc.cores import uart
from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *

from liteeth.phy.xgmii import LiteEthPHYXGMII
from liteeth.core import LiteEthUDPIPCore
from liteeth.frontend.etherbone import LiteEthEtherbone
from liteeth.common import convert_ip

# from litex_boards.platforms.xilinx_kc705 import Platform
from litex_boards.platforms.xilinx_zc706 import Platform


class XilinxXGMII(Module, AutoCSR):
    """
    pg068-ten-gig-eth-pcs-pma.pdf
    With shared logic inside the core for simplicity
    """

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
        core_status_sync = Signal(8)

        txusrclk = Signal()
        txusrclk2 = Signal()

        drp_req = Signal()

        drp_daddr_o = Signal(16)
        drp_den_o = Signal()
        drp_di_o = Signal(16)
        drp_dwe_o = Signal()
        drp_drpdo_i = Signal(16)
        drp_drdy_i = Signal()
        drp_den_i = Signal()

        # Request TX disable pin (before sfp_rx/tx to ensure it's available)
        self.sfp_tx_disable_n_pad = platform.request("sfp_tx_disable_n", loose=True)

        refclk_pads = platform.request("refclk")  # ZC706
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
        # Synchronize core_status bits [5:0] from clkmgt to sys domain
        core_status_sync = Signal(6)
        self.specials += MultiReg(self.core_status, core_status_sync)
        self.comb += [
            self.ps.i.eq(self._pcs_config.fields.pcs_clear),
            pcs_clear.eq(self.ps.o),
            self._pcs_status.fields.pcs_fault_rx.eq(pma_multi[0]),
            self._pcs_status.fields.pcs_fault_tx.eq(pma_multi[1]),
            self._pcs_status.fields.pcs_fault.eq(pma_multi[2]),
            # Core status bits [5:0] synchronized from clkmgt domain
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
            # o_resetdone_out=
            o_coreclk_out=self.coreclk,
            # rxrecclkout_0=rxrecclkout_0, # What is
            i_rxp=rx_pads.p,
            i_rxn=rx_pads.n,
            o_txp=tx_pads.p,
            o_txn=tx_pads.n,
            i_dclk=cd_sys.clk,
            i_sim_speedup_control=0,
            o_txusrclk_out=txusrclk,
            o_txusrclk2_out=txusrclk2,  # UltraScale only
            # o_areset_datapathclk_out=
            o_qplllock_out=self.qplllock,
            o_txuserrdy_out=self.txusrrdy,
            # o_reset_counter_done=,  # UltraScale only
            o_gttxreset_out=self.gttxreset,
            o_gtrxreset_out=self.gtrxreset,  # TODO Set it to 5 seconds?
            o_xgmii_rxd=self.rx_data,
            o_xgmii_rxc=self.rx_ctl,
            i_xgmii_txd=self.tx_data,
            i_xgmii_txc=self.tx_ctl,
            i_configuration_vector=config_vector,
            o_status_vector=_pma_status,
            o_core_status= self.core_status,
            # tx_resetdone=,
            # rx_resetdone=,
            # Connects to sfp+
            i_signal_detect=1,
            i_tx_fault=0,  # Unused inside the core
            o_tx_disable=self.tx_disable,
            i_pma_pmd_type=0b111,
            # DRP Stuff
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

        # Connect TX disable pad if available
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


class TenGbeTestSoC(BaseSoC, AutoCSR):
    def __init__(self, ethernet_mode="10g", **kwargs):
        self.ethernet_mode = ethernet_mode

        # Convert ethernet_mode to with_ethernet flag for BaseSoC
        if ethernet_mode == "10g":
            kwargs["with_ethernet"] = False
        elif ethernet_mode == "1g":
            kwargs["with_ethernet"] = kwargs.get("with_ethernet", True)
        elif ethernet_mode == "none":
            kwargs["with_ethernet"] = False
        else:
            raise ValueError(f"Unknown ethernet_mode: {ethernet_mode}. Must be '10g', '1g', or 'none'")

        # Call parent init (BaseSoC will set uart_name="crossover" and with_jtagbone)
        super().__init__(with_led_chaser=False, **kwargs)

        # DEBUG: Check which platform we actually got
        print(
            "DEBUG: self.platform is",
            type(self.platform).__module__ + "." + type(self.platform).__name__,
        )

        self.user_leds = user_leds = [
            self.platform.request("user_led", i) for i in range(4)
        ]
        self.counter = counter = Signal(27)
        self.sync += counter.eq(counter + 1)
        self.comb += user_leds[0].eq(counter[26])

        # Only add 10G Ethernet if requested
        if ethernet_mode == "10g":
            self.add_xgmii()

            # Frequency meter for XGMII clock
            self.submodules.f_sample = FreqMeter(self.sys_clk_freq)
            self.comb += self.f_sample.clk.eq(self.xgmii.cd_clkmgt.clk)
            self.add_csr("f_sample")

            # Monitor clkmgt domain
            self.counter_mgt = counter_mgt = Signal(27)
            self.sync.clkmgt += counter_mgt.eq(counter_mgt + 1)
            self.comb += user_leds[2].eq(counter_mgt[26])
        else:
            print(f"DEBUG: Skipping 10G Ethernet, using mode: {ethernet_mode}")

        # Add 10G IP only for 10G mode
        if ethernet_mode == "10g":
            self.platform.add_ip("ip/ten_gig_eth_pcs_pma_0.xci")

    def add_xgmii(self):
        self.submodules.xgmii = XilinxXGMII(self.crg.cd_sys, self.platform)
        self.add_csr("xgmii")
        self.platform.toolchain.pre_placement_commands.add(
            "set_clock_groups "
            "-group [get_clocks -include_generated_clocks -of [get_nets {sys_clk}]] "
            "-group [get_clocks -include_generated_clocks refclk_p] "
            "-asynchronous",
            sys_clk=self.crg.cd_sys.clk,
        )

        # Use ClockDomainsRenamer to make PHY use clkmgt domain directly
        # This avoids creating extra clock domains that trigger constraint warnings
        self.submodules.xgmiiphy = ClockDomainsRenamer("clkmgt")(
            LiteEthPHYXGMII(self.xgmii.pads, self.xgmii.pads)
        )

        # Note: NOT calling add_ethernet() for 10G mode because:
        # 1. add_ethernet() always creates eth_rx/eth_tx clock domains (unwanted constraints)
        # 2. teng_udp_core already provides the full L2/L3/L4 stack in clkmgt domain
        # 3. We access Ethernet via UDP port interface (no need for standard EthMAC)

        # Keeping this away from sys_clk domain, and strictly leaving it in 156.25e6
        self.submodules.teng_udp_core = ClockDomainsRenamer("clkmgt")(
            LiteEthUDPIPCore(
                self.xgmiiphy,
                0xAA1233445566,
                convert_ip("10.1.0.3"),
                self.sys_clk_freq,
                dw=64,
            )
        )
        self.add_csr("xgmiiphy")
        self.udp_port = udp_port = self.teng_udp_core.udp.crossbar.get_port(3000, 64)

        send_pkt = Signal(reset=0)
        always_xmit = True
        if always_xmit:
            send_pkt_counter_d = Signal()
            self.sync.clkmgt += [
                send_pkt_counter_d.eq(self.counter_mgt[26]),
                send_pkt.eq(send_pkt_counter_d ^ self.counter_mgt[26]),
            ]

        sink_counter = Signal(16)
        SINK_LENGTH = 64  # 8 words
        shift = log2_int(64 // 8)  # bits required to represent bytes per word
        words_per_packet = SINK_LENGTH >> shift #8
        # Note the clkmgt domain
        self.sync.clkmgt += [
            If(send_pkt, sink_counter.eq(words_per_packet)),
            If(
                (sink_counter > 0) & (udp_port.sink.ready == 1),
                sink_counter.eq(sink_counter - 1),
            ).Else(udp_port.sink.valid.eq(0), udp_port.sink.last.eq(0)),
            udp_port.sink.valid.eq(sink_counter > 0),
            udp_port.sink.last.eq(sink_counter == 1),
            If(sink_counter == 1, udp_port.sink.last_be.eq(0x80)).Else(
                udp_port.sink.last_be.eq(0x0)
            ),
        ]

        self.comb += self.user_leds[1].eq(udp_port.sink.valid)
        self.comb += [
            # param
            udp_port.sink.src_port.eq(3000),
            udp_port.sink.dst_port.eq(7778),
            udp_port.sink.ip_address.eq(convert_ip("10.1.0.4")),
            udp_port.sink.length.eq(SINK_LENGTH),
            # payload
            udp_port.sink.data.eq(Cat(0xC0FFEEC1FFEE, sink_counter)),
            udp_port.sink.error.eq(0),
        ]
        self.add_csr("teng_udp_core")


class DevSoC(TenGbeTestSoC):
    def __init__(self, **kwargs):
        from litescope import LiteScopeAnalyzer

        super().__init__(**kwargs)

        # Only create analyzer if using 10G mode
        if self.ethernet_mode == "10g":
            analyzer_signals = [
                self.udp_port.sink.valid,
                self.udp_port.sink.ready,
                self.udp_port.sink.last,
                self.udp_port.sink.data,
                # self.xgmii.rx_ctl,
                # self.xgmii.rx_data,
                # self.xgmii.tx_ctl,
                # self.xgmii.tx_data,
                self.xgmii.pma_status,
                self.xgmii.tx_disable,
                self.xgmii.qplllock  ,
                self.xgmii.gtrxreset ,
                self.xgmii.gttxreset ,
                self.xgmii.txusrrdy  ,
                self.xgmii.pcs_clear  ,
                self.xgmii.pma_multi  ,
                self.xgmiiphy.rx.source.valid,
                self.xgmiiphy.rx.source.data,
                self.xgmiiphy.tx.sink.valid,
                self.xgmiiphy.tx.sink.data,
                # self.teng_udp_core.mac.core.sink.valid,
                # self.teng_udp_core.mac.core.sink.data,
                # self.teng_udp_core.arp.rx.sink.valid,
                # self.teng_udp_core.arp.rx.sink.data,
                # self.teng_udp_core.arp.tx.source.valid,
                # self.teng_udp_core.arp.tx.source.data,
            ]
            print(self.xgmii.cd_clkmgt.clk)
            self.submodules.analyzer = LiteScopeAnalyzer(
                analyzer_signals, 1024, clock_domain="clkmgt", csr_csv="analyzer.csv"
            )
            self.add_csr("analyzer")
        else:
            print(f"DEBUG: Skipping LiteScopeAnalyzer for {self.ethernet_mode} mode")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="10GbE test setup on ZC706")
    parser.add_argument("--build", action="store_true", help="Build bitstream")
    parser.add_argument("--load", action="store_true", help="Load bitstream")
    parser.add_argument(
        "--with-ethernet", action="store_true", help="Enable Ethernet support (deprecated, use --ethernet-mode instead)"
    )
    parser.add_argument(
        "--ethernet-mode",
        choices=["10g", "1g", "none"],
        default="10g",
        help="Ethernet mode: 10g (10GbE with XilinxXGMII), 1g (standard 1GbE), or none (no Ethernet). Default: 10g"
    )
    builder_args(parser)
    soc_core_args(parser)
    args = parser.parse_args()
    print(args)

    if args.build:
        soc = DevSoC(
            sys_clk_freq=int(125e6),
            ethernet_mode=args.ethernet_mode,
            with_ethernet=(args.ethernet_mode == "1g"),  # Keep with_ethernet for compatibility with 1g mode
            **soc_core_argdict(args),
        )
        builder = Builder(soc, **builder_argdict(args))
        vns = builder.build(run=args.build)
        # soc.analyzer.do_exit(vns)

    if args.load:
        prog = Platform().create_programmer()
        prog.load_bitstream("build/xilinx_zc706/gateware/xilinx_zc706.bit")


if __name__ == "__main__":
    main()
