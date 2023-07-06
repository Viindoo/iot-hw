import scapy.all as scapy
import sys

ip = sys.argv[1]

arp_req = scapy.ARP(pdst=ip)
broadcast = scapy.Ether(dst = "ff:ff:ff:ff:ff:ff")
arp_req_bro = broadcast/arp_req
ans = scapy.srp(arp_req_bro, timeout=1, verbose = False)[0]
for e in ans:
    print(e[1].hwsrc)
