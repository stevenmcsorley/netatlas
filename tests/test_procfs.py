import socket
import unittest

from netatlas.procfs import parse_socket_table


class ProcfsTests(unittest.TestCase):
    def test_parses_ipv4_tcp_endpoints_and_state(self):
        table = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 08080808:01BB 01 00000000:00000000 00:00000000 00000000 1000 0 445566
   1: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 3
   2: 0100007F:9C40 08080808:0035 06 00000000:00000000 00:00000000 00000000 1000 0 0
"""
        rows = parse_socket_table(table, "TCP", socket.AF_INET)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["local"], "127.0.0.1:8080")
        self.assertEqual(rows[0]["remote"], "8.8.8.8:443")
        self.assertEqual(rows[0]["state"], "ESTABLISHED")
        self.assertEqual(rows[0]["inode"], "445566")

    def test_parses_ipv6_and_connected_udp(self):
        tcp6 = """sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode
0: 00000000000000000000000001000000:1F90 B80D0120000000000000000001000000:01BB 01 0:0 0:0 0 1000 0 44
"""
        udp = """sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode
0: 00000000:9C40 08080808:0035 01 0:0 0:0 0 1000 0 77
"""
        row6 = parse_socket_table(tcp6, "TCP", socket.AF_INET6)[0]
        row_udp = parse_socket_table(udp, "UDP", socket.AF_INET)[0]
        self.assertEqual(row6["remote"], "[2001:db8::1]:443")
        self.assertEqual(row_udp["remote"], "8.8.8.8:53")
        self.assertEqual(row_udp["state"], "CONNECTED")


if __name__ == "__main__":
    unittest.main()
