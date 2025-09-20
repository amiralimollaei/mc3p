# This source file is part of mc3p, the Minecraft Protocol Parsing Proxy.
#
# Copyright (C) 2011 Matthew J. McGill, AmirAli Mollaei

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License v2 as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import asyncio
import logging
import re
import signal
import socket
import sys
import traceback
from optparse import OptionParser
from time import time

from . import messages, util
from .parsing import parse_unsigned_byte
from .plugins import PluginConfig, PluginManager
from .util import PartialPacketException, Stream

logger = logging.getLogger("mc3p")

def sigint_handler(signum, stack):
    print("Received signal %d, shutting down" % signum)
    sys.exit(0)


def parse_args():
    """Return host and port, or print usage and exit."""
    usage = "usage: %prog [options] host [port]"
    desc = """
Create a Minecraft proxy listening for a client connection,
and forward that connection to <host>:<port>."""
    parser = OptionParser(usage=usage,
                          description=desc)
    parser.add_option("-l", "--log-level", dest="loglvl", metavar="LEVEL",
                      choices=["debug","info","warn","error"],
                      help="Override logging.conf root log level")
    parser.add_option("--log-file", dest='logfile', metavar="FILE", default=None,
                      help="logging configuration file (optional)")
    parser.add_option("-p", "--local-port", dest="locport", metavar="PORT", default="34343",
                      type="int", help="Listen on this port")
    parser.add_option("--plugin", dest="plugins", metavar="ID:PLUGIN(ARGS)", type="string",
                      action="append", help="Configure a plugin", default=[])
    parser.add_option("--profile", dest="perf_data", metavar="FILE", default=None,
                      help="Enable profiling, save profiling data to FILE")
    (opts,args) = parser.parse_args()

    if not 1 <= len(args) <= 2:
        parser.error("Incorrect number of arguments.") # Calls sys.exit()

    host = args[0]
    port = 25565
    if len(args) > 1:
        try:
            port = int(args[1])
        except ValueError:
            parser.error("Invalid port %s" % args[1])

    pcfg = PluginConfig()
    pregex = re.compile('((?P<id>\\w+):)?(?P<plugin_name>[\\w\\.\\d_]+)(\\((?P<argstr>.*)\\))?$')
    for pstr in opts.plugins:
        m = pregex.match(pstr)
        if not m:
            logger.error('Invalid --plugin option: %s' % pstr)
            sys.exit(1)
        else:
            parts = {'argstr': ''}
            parts.update(m.groupdict())
            pcfg.add(**parts)

    return (host, port, opts, pcfg)


def wait_for_client(port):
    """Listen on port for client connection, return resulting socket."""
    srvsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srvsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srvsock.bind( ("", port) )
    srvsock.listen(1)
    logger.info("mitm_listener bound to %d" % port)
    (sock, addr) = srvsock.accept()
    srvsock.close()
    logger.info("mitm_listener accepted connection from %s" % repr(addr))
    return sock


class MinecraftSession(object):
    """A client-server Minecraft session."""

    def __init__(self, pcfg, clientsock, dsthost, dstport):
        """Open connection to dsthost:dstport, and return client and server proxies."""
        logger.info("creating proxy from client to %s:%d" % (dsthost,dstport))
        self.srv_proxy = None
        try:
            serversock = socket.create_connection( (dsthost,dstport) )
            self.cli_proxy = MinecraftProxy(clientsock)
        except Exception as e:
            clientsock.close()
            logger.error("Couldn't connect to %s:%d - %s", dsthost, dstport, str(e))
            logger.info(traceback.format_exc())
            return
        self.srv_proxy = MinecraftProxy(serversock, self.cli_proxy)
        self.plugin_mgr = PluginManager(pcfg, self.cli_proxy, self.srv_proxy)
        self.cli_proxy.plugin_mgr = self.plugin_mgr
        self.srv_proxy.plugin_mgr = self.plugin_mgr

class UnsupportedPacketException(Exception):
    def __init__(self,pid):
        Exception.__init__(self,"Unsupported packet id 0x%x" % pid)

class MinecraftProxy(asyncio.Protocol):
    """
    Proxies a packet stream from a Minecraft client or server.
    """

    def __init__(self, other_side=None):
        """Proxies one side of a client-server connection.

        MinecraftProxy instances are created in pairs that have references to
        one another. Since a client initiates a connection, the client side of
        the pair is always created first, with other_side = None. The creator
        of the client proxy is then responsible for connecting to the server
        and creating a server proxy with other_side=client. Finally, the
        proxy creator should do client_proxy.other_side = server_proxy.
        """
        self.transport = None
        self.plugin_mgr = None
        self.other_side = other_side
        if other_side == None:
            self.side = 'client'
            self.msg_spec = messages.protocol[0][0]
        else:
            self.side = 'server'
            self.msg_spec = messages.protocol[0][1]
            self.other_side.other_side = self
        self.stream = Stream()
        self.last_report = 0
        self.msg_queue = []
        self.out_of_sync = False

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        """Read all available bytes, and process as many packets as possible.
        """
        t = time()
        if self.last_report + 5 < t and self.stream.tot_bytes > 0:
            self.last_report = t
            logger.debug("%s: total/wasted bytes is %d/%d (%f wasted)" % (
                 self.side, self.stream.tot_bytes, self.stream.wasted_bytes,
                 100 * float(self.stream.wasted_bytes) / self.stream.tot_bytes))
        self.stream.append(data)

        if self.out_of_sync:
            data = self.stream.read(len(self.stream))
            self.stream.packet_finished()
            if self.other_side:
                self.other_side.transport.write(data)
            return

        try:
            packet = parse_packet(self.stream, self.msg_spec, self.side)
            while packet != None:
                if packet['msgtype'] == 0x01 and self.side == 'client':
                    # Determine which protocol message definitions to use.
                    proto_version = packet['proto_version']
                    logger.info('Client requests protocol version %d' % proto_version)
                    if not proto_version in messages.protocol:
                        logger.error("Unsupported protocol version %d" % proto_version)
                        self.connection_lost(None)
                        return
                    self.msg_spec, self.other_side.msg_spec = messages.protocol[proto_version]
                forwarding = True
                if self.plugin_mgr:
                    forwarding = self.plugin_mgr.filter(packet, self.side)
                    if forwarding and packet.modified:
                        packet['raw_bytes'] = self.msg_spec[packet['msgtype']].emit(packet)
                if forwarding and self.other_side:
                    self.other_side.transport.write(packet['raw_bytes'])
                
                if self.plugin_mgr:
                    # Since we know we're at a message boundary, we can inject
                    # any messages in the queue.
                    msgbytes = self.plugin_mgr.next_injected_msg_from(self.side)
                    while self.other_side and msgbytes is not None:
                        self.other_side.transport.write(msgbytes)
                        msgbytes = self.plugin_mgr.next_injected_msg_from(self.side)

                # Attempt to parse the next packet.
                packet = parse_packet(self.stream,self.msg_spec, self.side)
        except PartialPacketException:
            pass # Not all data for the current packet is available.
        except Exception:
            logger.error("MinecraftProxy for %s caught exception, out of sync" % self.side)
            logger.error(traceback.format_exc())
            logger.debug("Current stream buffer: %s" % repr(self.stream.buf))
            self.out_of_sync = True
            self.stream.reset()

    def connection_lost(self, exc):
        """Call shutdown handler."""
        logger.info("%s socket closed.", self.side)
        if self.transport:
            self.transport.close()
        if self.other_side is not None:
            logger.info("shutting down other side")
            self.other_side.other_side = None
            if self.other_side.transport:
                self.other_side.transport.close()
            self.other_side = None
            logger.info("shutting down plugin manager")
            self.plugin_mgr.destroy()


class Message(dict):
    def __init__(self, d):
        super(Message, self).__init__(d)
        self.modified = False

    def __setitem__(self, key, val):
        if key in self and self[key] != val:
            self.modified = True
        return super(Message, self).__setitem__(key, val)

def parse_packet(stream, msg_spec, side):
    """Parse a single packet out of stream, and return it."""
    # read Packet ID
    msgtype = parse_unsigned_byte(stream)
    
    if not msg_spec[msgtype]:
        logger.debug(msg_spec)
        raise UnsupportedPacketException(msgtype)
    logger.debug("%s trying to parse message type %x" % (side, msgtype))
    msg_parser = msg_spec[msgtype]
    msg = msg_parser.parse(stream)
    msg['raw_bytes'] = stream.packet_finished()
    return Message(msg)


async def main():
    logging.basicConfig(level=logging.INFO)
    (host, port, opts, pcfg) = parse_args()

    if opts.logfile:
        util.config_logging(opts.logfile)

    if opts.loglvl:
        logging.root.setLevel(getattr(logging, opts.loglvl.upper()))

    # Install signal handler.
    signal.signal(signal.SIGINT, sigint_handler)

    loop = asyncio.get_running_loop()

    async def handle_client(reader, writer):
        cli_proxy = MinecraftProxy()
        cli_proxy.transport = writer # Not a full transport, but has write/close.

        print("connecting...")
        try:
            # Create connection to the server
            transport, srv_proxy = await loop.create_connection(
                lambda: MinecraftProxy(other_side=cli_proxy),
                host, port
            )
        except Exception as e:
            print(f"Failed to connect to server: {e}")
            writer.close()
            await writer.wait_closed()
            return

        plugin_mgr = PluginManager(pcfg, cli_proxy, srv_proxy)
        cli_proxy.plugin_mgr = plugin_mgr
        srv_proxy.plugin_mgr = plugin_mgr
        print("connected!")

        # Forward data from client to server
        while not reader.at_eof():
            data = await reader.read(4096)
            if data:
                cli_proxy.data_received(data)
            else:
                break
        
        cli_proxy.connection_lost(None)


    server = await asyncio.start_server(
        handle_client,
        '127.0.0.1', opts.locport)

    addr = server.sockets[0].getsockname()
    print(f'Serving on {addr}')

    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

