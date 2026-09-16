import base64
import json
import unittest

from trenball.selenium_collector import decode_bc_binary, parse_payloads


def varint(value):
    out = bytearray()
    while value > 0x7f:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


class CollectorParserTests(unittest.TestCase):
    def test_nested_json(self):
        item = parse_payloads('{"data":{"game_id":123,"crash":"2.37x","created_at":1700000000}}')[0]
        self.assertEqual((item.game_id, item.multiplier, item.timestamp), ("123", 2.37, 1700000000000))

    def test_socket_io_and_odds_preference(self):
        item = parse_payloads('42["end",{"gameId":8,"odds":6.63,"crash":663}]')[0]
        self.assertEqual((item.game_id, item.multiplier), ("8", 6.63))

    def test_bc_binary(self):
        body = b"\x08" + varint(5527938) + b"\x30" + varint(663) + b"\x3a\x03abc"
        raw = b"\x04\x02\x05/g/cm\x02st" + body
        item = decode_bc_binary(base64.b64encode(raw).decode())
        self.assertEqual((item.game_id, item.multiplier, item.hash), ("5527938", 6.63, "abc"))


if __name__ == "__main__":
    unittest.main()
