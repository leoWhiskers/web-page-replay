#!/usr/bin/env python
# Copyright 2010 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import daemonserver
import errno
import logging
import socket
import SocketServer
import threading

import third_party
import dns.resolver
import dns.rdatatype
import ipaddr


class DnsProxyException(Exception):
  pass


class RealDnsLookup(object):
  def __init__(self, name_servers):
    if '127.0.0.1' in name_servers:
      raise DnsProxyException(
          'Invalid nameserver: 127.0.0.1 (causes an infinte loop)')
    self.resolver = dns.resolver.get_default_resolver()
    self.resolver.nameservers = name_servers
    self.dns_cache_lock = threading.Lock()
    self.dns_cache = {}

  def __call__(self, hostname, rdtype=dns.rdatatype.A):
    """Return real IP for a host.

    Args:
      host: a hostname ending with a period (e.g. "www.google.com.")
      rdtype: the query type (1 for 'A', 28 for 'AAAA')
    Returns:
      the IP address as a string (e.g. "192.168.25.2")
    """
    self.dns_cache_lock.acquire()
    ip = self.dns_cache.get(hostname)
    self.dns_cache_lock.release()
    if ip:
      return ip
    try:
      answers = self.resolver.query(hostname, rdtype)
    except (dns.resolver.NoAnswer,
            dns.resolver.NXDOMAIN,
            dns.resolver.Timeout) as ex:
      logging.debug('_real_dns_lookup(%s) -> None (%s)',
                    hostname, ex.__class__.__name__)
      return None
    if answers:
      ip = str(answers[0])
    self.dns_cache_lock.acquire()
    self.dns_cache[hostname] = ip
    self.dns_cache_lock.release()
    return ip


class DnsPrivatePassthroughFilter:
  """Allow private hosts to resolve to their real IPs.

  This only supports IPv4 lookups.
  """
  def __init__(self, web_proxy_ip, real_dns_lookup, skip_passthrough_hosts=()):
    """Initialize DnsPrivatePassthroughFilter.

    Args:
      web_proxy_ip: the IP address returned by __call__ for non-private hosts.
      real_dns_lookup: a function that resolves a host to an IP.
      skip_passthrough_hosts: an iterable of hosts that skip
        the private determination (i.e. avoids a real dns lookup
        for them).
    """
    self.web_proxy_ip = web_proxy_ip
    self.real_dns_lookup = real_dns_lookup
    self.skip_passthrough_hosts = set(
        host + '.' for host in skip_passthrough_hosts)

  def __call__(self, host):
    """Return real IPv4 for host if private.

    Args:
      host: a hostname ending with a period (e.g. "www.google.com.")
    Returns:
      ip address as a string or None (if lookup fails)
    """
    ip = self.web_proxy_ip
    if host not in self.skip_passthrough_hosts:
      real_ip = self.real_dns_lookup(host)
      if real_ip:
        if ipaddr.IPAddress(real_ip).is_private:
          ip = real_ip
      else:
        ip = None
    return ip


class UdpDnsHandler(SocketServer.DatagramRequestHandler):
  """Resolve DNS queries to localhost.

  Possible alternative implementation:
  http://howl.play-bow.org/pipermail/dnspython-users/2010-February/000119.html
  """

  STANDARD_QUERY_OPERATION_CODE = 0

  def handle(self):
    """Handle a DNS query.

    IPv6 requests (with rdtype AAAA) receive mismatched IPv4 responses
    (with rdtype A). To properly support IPv6, the http proxy would
    need both types of addresses. By default, Windows XP does not
    support IPv6.
    """
    self.data = self.rfile.read()
    self.transaction_id = self.data[0]
    self.flags = self.data[1]
    self.qa_counts = self.data[4:6]
    self.domain = ''
    operation_code = (ord(self.data[2]) >> 3) & 15
    if operation_code == self.STANDARD_QUERY_OPERATION_CODE:
      self.wire_domain = self.data[12:]
      self.domain = self._domain(self.wire_domain)
    else:
      logging.debug("DNS request with non-zero operation code: %s",
                    operation_code)
    ip = self.server.passthrough_filter(self.domain)
    if ip is None:
      # For failed dns resolutions, return the replay web proxy ip anyway.
      # TODO(slamm): make failed dns resolutions return an error.
      ip = self.server.server_address[0]
    if ip == self.server.server_address[0]:
      logging.debug('dnsproxy: %s -> %s (replay web proxy)', self.domain, ip)
    else:
      logging.debug('dnsproxy: %s -> %s', self.domain, ip)
    self.wfile.write(self.get_dns_response(ip))

  @classmethod
  def _domain(cls, wire_domain):
    domain = ''
    index = 0
    length = ord(wire_domain[index])
    while length:
      domain += wire_domain[index + 1:index + length + 1] + '.'
      index += length + 1
      length = ord(wire_domain[index])
    return domain

  def get_dns_response(self, ip):
    packet = ''
    if self.domain:
      packet = (
          self.transaction_id +
          self.flags +
          '\x81\x80' +        # standard query response, no error
          self.qa_counts * 2 + '\x00\x00\x00\x00' +  # Q&A counts
          self.wire_domain +
          '\xc0\x0c'          # pointer to domain name
          '\x00\x01'          # resource record type ("A" host address)
          '\x00\x01'          # class of the data
          '\x00\x00\x00\x3c'  # ttl (seconds)
          '\x00\x04' +        # resource data length (4 bytes for ip)
          socket.inet_aton(ip)
          )
    return packet


class DnsProxyServer(SocketServer.ThreadingUDPServer,
                     daemonserver.DaemonServer):
  def __init__(self, passthrough_filter=None, host='', port=53):
    """Initialize DnsProxyServer.

    Args:
      passthrough_filter: a function that resolves a host to its real IP,
        or None, if it should resolve to the dnsproxy's address.
      host: a host string (name or IP) to bind the dns proxy and to which
        DNS requests will be resolved.
      port: an integer port on which to bind the proxy.
    """
    try:
      SocketServer.ThreadingUDPServer.__init__(
          self, (host, port), UdpDnsHandler)
    except socket.error, (error_number, msg):
      if error_number == errno.EACCES:
        raise DnsProxyException(
            'Unable to bind DNS server on (%s:%s)' % (host, port))
      raise
    self.passthrough_filter = passthrough_filter or (
        lambda host: self.server_address[0])
    logging.info('Started DNS server on %s...', self.server_address)

  def cleanup(self):
    self.shutdown()
    logging.info('Shutdown DNS server')
