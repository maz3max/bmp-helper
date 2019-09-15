#!/usr/bin/python3
# Black Magic Probe helper script
# This script can detect connected Black Magic Probes and can be used as a flashloader and much more

import argparse
import os
import re
import sys

import humanize
import serial.tools.list_ports
from progressbar import Bar, Percentage, ProgressBar
from pygdbmi.gdbcontroller import GdbController

parser = argparse.ArgumentParser(description='Black Magic Tool helper script.')
parser.add_argument('--jtag', action='store_true', help='use JTAG transport')
parser.add_argument('--swd', action='store_true', help='use SWD transport (default)')
parser.add_argument('--connect-srst', action='store_true', help='reset target while connecting')
parser.add_argument('--tpwr', action='store_true', help='enable target power')
parser.add_argument('--serial', help='choose specific probe by serial number')
parser.add_argument('--port', help='choose specific probe by port')
parser.add_argument('--attach', help='choose specific target by number', default='1')
parser.add_argument('--gdb-path', help='path to GDB', default='gdb-multiarch')
parser.add_argument('--term-cmd', help='serial terminal command', default='screen %s 115200')
parser.add_argument('action', help='choose a task to perform', nargs='?',
                    choices=['list', 'flash', 'erase', 'debug', 'term', 'reset'],
                    default='list')
parser.add_argument('file', help='file to load to target (hex or elf)', nargs='?')

TIMEOUT = 100  # seconds


# find all connected BMPs and store both GDB and UART interfaces
def detect_probes():
    GDBs = []
    UARTs = []
    for p in serial.tools.list_ports.comports():
        if p.vid == 0x1D50 and p.pid in {0x6018, 0x6017}:
            if p.interface == 'Black Magic GDB Server' \
                    or re.fullmatch(r'/dev/cu\.usbmodem([A-F0-9]*)1', p.device) \
                    or p.location[-1] == '0':
                GDBs.append(p)
            else:
                UARTs.append(p)
    return GDBs, UARTs


# search device with specific serial number <snr> in list <l>
def search_serial(snr, l):
    for p in l:
        if snr in p.serial_number:
            return p.device


# parse GDB output for targets
def detect_targets(gdbmi, res):
    targets = []
    while True:
        for msg in res:
            if msg['type'] == 'target':
                m = re.fullmatch(pattern=r"\s*(\d)+\s*(.*)\\n", string=msg['payload'])
                if m:
                    targets.append(m.group(2))
            elif msg['type'] == 'result':
                assert msg['message'] == 'done', str(msg)
                return targets

        res = gdbmi.get_gdb_response(timeout_sec=TIMEOUT)


def gdb_write_and_wait_for_result(gdbmi, cmd, description, expected_result='done'):
    res = gdbmi.write(cmd, timeout_sec=TIMEOUT)
    no_result = True
    while True:
        for msg in res:
            print(msg)
            if msg['type'] == 'result':
                no_result = False
                if msg['message'] == expected_result:
                    print(description, "successful.")
                    return True
                else:
                    print(description, "failed.", file=sys.stderr)
                    return False
        res = gdbmi.get_gdb_response(timeout_sec=TIMEOUT)


if __name__ == '__main__':
    args = parser.parse_args()
    assert not (args.swd and args.jtag), "you may only choose one protocol"
    assert not (args.serial and args.port), "you may only specify the probe by port or by serial"
    g, u = detect_probes()
    assert len(g) > 0, "no Black Magic Probes found 😔"

    # terminal mode, opens TTY program
    if args.action == 'term':
        port = u[0].device
        if args.port:
            port = args.port
        elif args.serial:
            port = search_serial(args.serial, u)
            assert port, "no BMP with this serial found"
        os.system(args.term_cmd % port)
        sys.exit(0)
    else:
        print("found following Black Magic GDB servers:")
        for i, s in enumerate(g):
            print("\t[%s]" % s.device, end=' ')
            if len(s.serial_number) > 1:
                print("Serial:", s.serial_number, end=' ')
            if i == 0:
                print("<- default", end=' ')
            print('')

        port = g[0].device
        if args.port:
            port = args.port
        elif args.serial:
            port = search_serial(args.serial, g)
            assert port, "no BMP with this serial found"

        print('connecting to [%s]...' % port)

        fname = args.file if args.file else ''

        # debug mode, opens GDB shell with options
        if args.action == 'debug':
            gdb_args = ['-ex \'target extended-remote %s\'' % port]
            if args.tpwr:
                gdb_args.append('-ex \'monitor tpwr enable\'')
            if args.connect_srst:
                gdb_args.append('-ex \'monitor connect_srst enable\'')
            if args.jtag:
                gdb_args.append('-ex \'monitor jtag_scan\'')
            else:
                gdb_args.append('-ex \'monitor swdp_scan\'')
            gdb_args.append('-ex \'attach %s\'' % args.attach)
            os.system(" ".join([args.gdb_path] + gdb_args + [fname]))
            sys.exit(0)

        # open GDB in machine interface mode
        gdbmi = GdbController(gdb_path=args.gdb_path, gdb_args=["--nx", "--quiet", "--interpreter=mi2", fname])
        assert gdb_write_and_wait_for_result(gdbmi, '-target-select extended-remote %s' % port, 'connecting',
                                             expected_result='connected')
        # set options
        if args.connect_srst:
            gdbmi.write('monitor connect_srst enable', timeout_sec=TIMEOUT)
        if args.tpwr:
            gdbmi.write('monitor tpwr enable', timeout_sec=TIMEOUT)

        # scan for targets
        if not args.jtag:
            print("scanning using SWD...")
            res = gdbmi.write('monitor swdp_scan', timeout_sec=TIMEOUT)
        else:
            print("scanning using JTAG...")
            res = gdbmi.write('monitor jtag_scan', timeout_sec=TIMEOUT)
        targets = detect_targets(gdbmi, res)
        assert len(targets) > 0, "no targets found"
        print("found following targets:")
        for t in targets:
            print("\t%s" % t)
        print("")

        if args.action == 'list':
            sys.exit(0)

        assert gdb_write_and_wait_for_result(gdbmi, '-target-attach %s' % args.attach, 'attaching to target')

        # reset mode: reset device using reset pin
        if args.action == 'reset':
            assert gdb_write_and_wait_for_result(gdbmi, 'monitor hard_srst', 'resetting target')
            sys.exit(0)
        # erase mode
        elif args.action == 'erase':
            print('erasing...')
            assert gdb_write_and_wait_for_result(gdbmi, '-target-flash-erase', 'erasing target')
            sys.exit(0)
        # flashloader mode: flash, check and restart
        elif args.action == 'flash':
            # download to flash
            res = gdbmi.write('-target-download', timeout_sec=TIMEOUT)
            downloading = True  # flag to leave outer loop
            first = True  # whether this is the first status message
            current_sec = None  # name of current section
            pbar = ProgressBar()
            while downloading:
                for msg in res:
                    if msg['type'] == 'result':
                        downloading = False
                        assert msg['message'] == 'done', "download failed: %s" % str(msg)
                        if pbar.start_time:
                            pbar.finish()
                        print("downloading finished")
                        break
                    elif msg['type'] == 'output':
                        m = re.fullmatch(
                            pattern=r"\+download,\{(?:section=\"(.*?)\")?,?(?:section-sent=\"(.*?)\")?,?(?:section-size=\"(.*?)\")?,?(?:total-sent=\"(.*?)\")?,?(?:total-size=\"(.*?)\")?,?\}",
                            string=msg['payload'])
                        if m:
                            if first:
                                first = False
                                print("downloading... total size: %s" % humanize.naturalsize(int(m.group(5)), gnu=True))
                            if m.group(1) != current_sec:
                                if pbar.start_time:
                                    pbar.finish()
                                pbar = ProgressBar(widgets=[Percentage(), Bar()], maxval=int(m.group(3))).start()
                                current_sec = m.group(1)
                                print(
                                    "downloading section [%s] (%s)" % (
                                        m.group(1), humanize.naturalsize(int(m.group(3)), gnu=True)))
                            if m.group(2):
                                pbar.update(int(m.group(2)))
                if downloading:
                    res = gdbmi.get_gdb_response(timeout_sec=TIMEOUT)

            # check flash
            assert gdb_write_and_wait_for_result(gdbmi, 'compare-sections', 'checking flash')
            # kill and reset
            assert gdb_write_and_wait_for_result(gdbmi, 'kill', 'killing')
