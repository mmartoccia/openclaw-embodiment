import random

from openclaw_wearable.transport.ble import Fragmenter


def test_fragment_roundtrip_ordered():
    f = Fragmenter(mtu=64)
    packet = b"x" * 1000
    frags = f.fragment(packet, message_id=42, packet_seq=7)
    out = None
    for frag in frags:
        out = f.defragment(frag)
    assert out == packet


def test_fragment_roundtrip_fuzz_reordered():
    random.seed(0)
    for n in range(50):
        f = Fragmenter(mtu=random.randint(32, 128))
        packet = bytes([random.randint(0, 255) for _ in range(random.randint(200, 3000))])
        frags = f.fragment(packet, message_id=n + 1, packet_seq=n % 255)
        random.shuffle(frags)
        out = None
        for frag in frags:
            out = f.defragment(frag)
        assert out == packet
