#!/usr/bin/env python3
"""Ask each resolver for one name and report whether it actually answers.

Runs on the MIRROR ECS, not the operator host: the mirror sits in the same VPC
as the cluster nodes, so it is the only vantage point that answers the question
the nodes will ask.  Uses a raw UDP query rather than dig/nslookup because
neither is guaranteed to be installed on an Alibaba Cloud Linux image, and
installing a package to run a pre-flight check needs egress this environment
may not have.

    dns-probe.py <name> <resolver> [<resolver> ...]

Exit 0 if at least one resolver returns an answer for <name>, 1 otherwise.
"""
import socket
import struct
import sys


def query(resolver, name, timeout=3.0):
    """Return the answer count, or None when the resolver does not reply."""
    header = b"\xab\xcd" + b"\x01\x00" + struct.pack("!HHHH", 1, 0, 0, 0)
    question = b"".join(
        bytes([len(label)]) + label.encode() for label in name.rstrip(".").split(".")
    ) + b"\x00" + struct.pack("!HH", 1, 1)          # QTYPE=A, QCLASS=IN

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(header + question, (resolver, 53))
        reply, _ = sock.recvfrom(2048)
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()

    if len(reply) < 12 or reply[:2] != header[:2]:
        return None
    return struct.unpack("!H", reply[6:8])[0]


def main(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: dns-probe.py <name> <resolver> [<resolver> ...]\n")
        return 2
    name, resolvers = argv[1], argv[2:]

    answered = False
    for resolver in resolvers:
        count = query(resolver, name)
        if count is None:
            print("  %-16s no reply" % resolver)
        elif count == 0:
            print("  %-16s replied, but no answer for %s" % (resolver, name))
            answered = True        # the resolver works; the name is the problem
        else:
            print("  %-16s OK (%d answer(s) for %s)" % (resolver, count, name))
            answered = True
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
