#!/usr/bin/env python3
"""UniversalJIT26 host breakpoint processor used by the unattended harness.

This implementation is recovered from the physical-device debugging rollout that
successfully drove Amethyst's UniversalJIT26 protocol on iOS.  It speaks the
GDB remote serial protocol (RSP) to CoreDevice/debugserver and intentionally has
no dependency on pymobiledevice3 itself; `amethystd` owns the forwarding
process and this program owns the attached debug session.
"""
from __future__ import annotations

import argparse
import re
import socket
import sys

PAGE_SIZE = 0x4000


class Remote:
    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=30)
        self.sock.settimeout(None)
        self.no_ack = False
        # CoreDevice's debug proxy starts this service with two outstanding ACKs.
        self.sock.sendall(b"++")

    def close(self) -> None:
        self.sock.close()

    @staticmethod
    def frame(payload: str) -> bytes:
        raw = payload.encode("ascii")
        checksum = sum(raw) & 0xFF
        return b"$" + raw + b"#" + f"{checksum:02x}".encode("ascii")

    def _read_packet(self) -> str:
        while True:
            ch = self.sock.recv(1)
            if not ch:
                raise EOFError("debugserver closed the connection")
            if ch == b"$":
                break
        data = bytearray()
        wire = bytearray()
        while True:
            ch = self.sock.recv(1)
            if not ch:
                raise EOFError("debugserver closed while reading a packet")
            if ch == b"#":
                break
            wire.extend(ch)
            if ch == b"}":
                escaped = self.sock.recv(1)
                if not escaped:
                    raise EOFError("debugserver closed during RSP escaping")
                wire.extend(escaped)
                data.append(escaped[0] ^ 0x20)
            else:
                data.extend(ch)
        checksum_raw = self.sock.recv(2)
        if len(checksum_raw) != 2:
            raise EOFError("debugserver closed while reading packet checksum")
        received = int(checksum_raw, 16)
        expected = sum(wire) & 0xFF
        # CoreDevice's proxy has been observed returning 00 for stop replies.
        # Preserve the proven compatibility behavior while rejecting all other
        # corrupt packets.
        if received not in (0, expected):
            if not self.no_ack:
                self.sock.sendall(b"-")
            raise RuntimeError(f"RSP checksum mismatch: expected {expected:02x}, got {received:02x}")
        if not self.no_ack:
            self.sock.sendall(b"+")
        return data.decode("ascii")

    def command(self, payload: str) -> str:
        packet = self.frame(payload)
        self.sock.sendall(packet)
        if not self.no_ack:
            while True:
                ack = self.sock.recv(1)
                if not ack:
                    raise EOFError("debugserver closed while waiting for ACK")
                if ack == b"+":
                    break
                if ack == b"-":
                    self.sock.sendall(packet)
        response = self._read_packet()
        while payload == "QStartNoAckMode" and response != "OK":
            print(f"pre-negotiation packet: {response[:80]}", flush=True)
            response = self._read_packet()
        if payload == "QStartNoAckMode" and response == "OK":
            self.no_ack = True
        return response


def reg_value(stop: str, number: int) -> int:
    marker = f"{number:02x}:"
    for field in stop.split(";"):
        if field.startswith(marker):
            raw = bytes.fromhex(field[len(marker):])
            return int.from_bytes(raw, "little")
    raise RuntimeError(f"register {number:#x} missing from stop reply")


def reg_hex(value: int) -> str:
    return int(value).to_bytes(8, "little").hex()


def thread_id(stop: str) -> str:
    match = re.match(r"^T[0-9a-f]+thread:([0-9a-f]+);", stop)
    if match:
        return match.group(1)
    for field in stop.split(";"):
        if field.startswith("thread:"):
            return field.split(":", 1)[1]
    raise RuntimeError(f"thread id missing from stop reply: {stop}")


def require_ok(response: str, operation: str) -> None:
    if response != "OK":
        raise RuntimeError(f"{operation} failed: {response}")


def prepare_region(remote: Remote, address: int, length: int) -> int:
    if not address or not length:
        return 0
    pages = (length + PAGE_SIZE - 1) // PAGE_SIZE
    for page in range(pages):
        page_address = address + page * PAGE_SIZE
        require_ok(remote.command(f"M{page_address:x},1:69"), f"prepare page {page_address:#x}")
    print(f"prepared {pages} JIT page(s) at {address:#x}", flush=True)
    return pages


def allocate_rx(remote: Remote, length: int) -> int:
    response = remote.command(f"_M{length:x},rx")
    if not response or response.startswith("E"):
        raise RuntimeError(f"RX allocation failed: {response}")
    address = int(response, 16)
    print(f"allocated RX region {address:#x} length {length:#x}", flush=True)
    return address


def process(args: argparse.Namespace) -> int:
    remote = Remote(args.host, args.port)
    try:
        require_ok(remote.command("QStartNoAckMode"), "no-ack negotiation")
        stop = remote.command(f"vAttach;{args.pid:x}")
        print(f"attached to PID {args.pid}: {stop[:80]}", flush=True)
        print(f"AMETHYST_JIT_PROCESSOR_ATTACHED pid={args.pid}", flush=True)

        extension_loaded = False
        detach_after_first = False
        patch_regions = 0

        while True:
            stop = remote.command("c")
            if not stop.startswith("T"):
                print(f"process response: {stop}", flush=True)
                if stop.startswith(("W", "X")):
                    return 1
                continue

            tid = thread_id(stop)
            pc = reg_value(stop, 0x20)
            x0 = reg_value(stop, 0x00)
            x1 = reg_value(stop, 0x01)
            x16 = reg_value(stop, 0x10)
            instruction = remote.command(f"m{pc:x},4")
            insn = int.from_bytes(bytes.fromhex(instruction), "little")

            if (insn & 0xFFE0001F) != 0xD4200000:
                signal = stop[1:3]
                print(f"continuing past stop {signal} at {pc:#x}", flush=True)
                continue

            immediate = (insn >> 5) & 0xFFFF
            require_ok(remote.command(f"P20={reg_hex(pc + 4)};thread:{tid};"), "advance PC")
            print(f"BRK {immediate:#x}: x0={x0:#x} x1={x1:#x} x16={x16:#x}", flush=True)

            if immediate == 0x69:
                if not extension_loaded:
                    # UniversalJIT26 identifies the host by the raw little-endian
                    # register bytes E0000069 (x0 == 0x690000E0).
                    require_ok(remote.command(f"P0=E0000069;thread:{tid};"), "return universal sentinel")
                    print("returned UniversalJIT26 sentinel 0x690000E0", flush=True)
                    print("AMETHYST_JIT_UNIVERSAL_HANDSHAKE_OK", flush=True)
                    continue
                length = x0
                address = allocate_rx(remote, length)
                pages = prepare_region(remote, address, length)
                require_ok(remote.command(f"P0={reg_hex(address)};thread:{tid};"), "return JIT address")
                print(
                    f"AMETHYST_JIT_HOST_EXEC_READY rx={address:#x} length={length:#x} pages={pages}",
                    flush=True,
                )
                if detach_after_first:
                    require_ok(remote.command("D"), "detach")
                    print("detached after first JIT region", flush=True)
                    return 0
                continue

            if immediate != 0xF00D:
                print(f"unhandled BRK {immediate:#x}; leaving it skipped", flush=True)
                continue

            if x16 == 0:
                require_ok(remote.command("D"), "detach")
                print("target requested detach", flush=True)
                return 0
            if x16 == 1:
                address = x0 or allocate_rx(remote, x1)
                prepare_region(remote, address, x1)
                require_ok(remote.command(f"P0={reg_hex(address)};thread:{tid};"), "return JIT address")
            elif x16 == 2:
                extension_loaded = True
                print(f"accepted UniversalJIT26 extension ({x1} bytes)", flush=True)
                print(f"AMETHYST_JIT_EXTENSION_ACCEPTED bytes={x1}", flush=True)
            elif x16 == 3:
                detach_after_first = bool(x0)
                print(f"detach-after-first = {detach_after_first}", flush=True)
                print(f"AMETHYST_JIT_KEEP_ATTACHED value={str(not detach_after_first).lower()}", flush=True)
            elif x16 == 4:
                data = remote.command(f"m{x0:x},{x1:x}")
                require_ok(remote.command(f"M{x0:x},{x1:x}:{data}"), "prepare patch region")
                patch_regions += 1
                print(
                    f"AMETHYST_JIT_PATCH_REGION_READY address={x0:#x} length={x1:#x} count={patch_regions}",
                    flush=True,
                )
            else:
                print(f"unhandled UniversalJIT26 command {x16}", flush=True)
    finally:
        remote.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Process Amethyst UniversalJIT26 breakpoints over debugserver RSP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pid", type=int, required=True)
    return process(parser.parse_args())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
