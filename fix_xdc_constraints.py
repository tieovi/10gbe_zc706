#!/usr/bin/env python3
"""
Remove invalid clock constraints from generated XDC files.

This script removes:
1. create_clock constraints on internal clock nets
2. set_clock_groups constraints that reference removed internal clocks

These are typically created by LiteX for clock domains that shouldn't have
primary clock constraints (they're derived from other clocks).
"""
import sys

def fix_xdc_file(xdc_file, verbose=True):
    """Fix invalid clock constraints in XDC file."""

    # Internal clock nets that should NOT have primary clock constraints
    internal_nets = [
        'eth_rx_clk',
        'eth_tx_clk',
        'clkmgt_clk',
        'sys_clk',
    ]

    with open(xdc_file, 'r') as f:
        lines = f.readlines()

    filtered_lines = []
    removed_count = 0

    for i, line in enumerate(lines):
        is_bad_line = False
        reason = ""

        # Check for create_clock on internal nets
        for net in internal_nets:
            if f'create_clock' in line and f'[get_nets {net}]' in line:
                is_bad_line = True
                reason = f"create_clock on internal net {net}"
                break

            # Check for set_clock_groups that reference internal nets
            if 'set_clock_groups' in line and f'[get_nets {net}]' in line:
                is_bad_line = True
                reason = f"set_clock_groups referencing internal net {net}"
                break

        if not is_bad_line:
            filtered_lines.append(line)
        else:
            removed_count += 1
            if verbose:
                print(f"Line {i+1}: Removed {reason}: {line.strip()}")

    with open(xdc_file, 'w') as f:
        f.writelines(filtered_lines)

    return removed_count

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: fix_xdc_constraints.py <xdc_file>")
        sys.exit(1)

    xdc_file = sys.argv[1]
    removed = fix_xdc_file(xdc_file)
    print(f"Fixed {removed} bad constraint line(s) in {xdc_file}")
