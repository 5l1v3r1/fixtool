#! /usr/bin/env python3
##################################################################
# fixtool
# Copyright (C) 2017, David Arnold.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
#
##################################################################

"""Background agent that manages the fixtool sessions."""

# The client to agent protocol uses TCP, with a simple 32-bit big-endian
# length frame, and then JSON to encode messages.
#
# Any number of fixtool clients can connect to the agent simultaneously.
# This is no significant security implemented.
#
# Initially, at least, the agent will be invisible.  It might make sense
# to add a web UI later, as an alternative controller (via WebSockets).
#
# Requests:
# - login
# - ping
# - logout
# - restart
# - shutdown
# - status

# - client_create / client_created
# - client_connect / client_connected
# - client_disconnect / client_disconnected
# - client send / client_sent
# - client_get_pending_receive / client_pending_receive
# - client_receive / client_received

# - server_create / server_created
# - server_listen / server_listening
# - server_listen_stop / server_listen_stopped
# - server_get_pending_accept / server_pending_accept
# - server_accept / server_accepted
# - server_send / server_sent
# - server_get_pending_receive / server_pending_receive
# - server_receive / server_received
# - server_disconnect / server_disconnected



import asyncio
import logging
import os
import simplefix
import signal
import socket
import struct
import sys
import json

from fixtool.message import *


class Client:
    def __init__(self, name: str):
        """Constructor."""
        self._name = name
        self._comp_id = b''
        self._auto_heartbeat = True
        self._auto_sequence = True
        self._raw = False
        self._next_send_sequence = 0
        self._last_seen_sequence = 0
        self._host = None
        self._port = None
        self._is_connected = False

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setblocking(True)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self._parser = simplefix.FixParser()
        self._recv_queue = []
        return

    def destroy(self):
        if self._is_connected:
            self.disconnect()

        return

    def connect(self, host: str, port: int):
        self._host = host
        self._port = port
        self._socket.connect((self._host, self._port))
        self._is_connected = True

        asyncio.get_event_loop().add_reader(self._socket, self.readable)
        return

    def is_connected(self):
        return self._is_connected

    def disconnect(self):
        asyncio.get_event_loop().remove_reader(self._socket)
        self._socket.close()
        self._is_connected = False
        return

    def readable(self):
        buf = self._socket.recv(65536)
        if len(buf) == 0:
            self.disconnect()
            return

        self._parser.append_buffer(buf)
        message = self._parser.get_message()
        while message is not None:
            self._recv_queue.append(message)
            message = self._parser.get_message()
        return



class Server:
    def __init__(self):
        """Constructor."""
        self._auto_heartbeat = True
        self._auto_sequence = True
        self._raw = False
        self._next_send_sequence = 0
        self._last_seen_sequence = 0
        self._pending_sessions = []
        self._accepted_sessions = {}

        self._socket = None
        return

    def destroy(self):
        if self._socket is not None:
            self.unlisten()

        for session in self._pending_sessions:
            session.destroy()
        self._pending_sessions = []

        for session in self._accepted_sessions.values():
            session.destroy()
        self._accepted_sessions = {}
        return

    def is_raw(self):
        """Is this server configured in 'raw' mode?"""
        return self._raw

    def listen(self, port):
        """Listen for client connections.

        :param port: TCP port number to listen on."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setblocking(False)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(('', port))
        self._socket.listen(5)

        asyncio.get_event_loop().add_reader(self._socket, self.acceptable)
        return

    def unlisten(self, port: int):
        asyncio.get_event_loop().remove_reader(self._socket)
        self._socket.close()
        self._socket = None
        return

    def acceptable(self):
        """Handle readable event on listening socket."""
        sock, _ = self._socket.accept()
        session = ServerSession(self, sock)
        self._pending_sessions.append(session)
        return

    def pending_client_count(self):
        """Return number of pending client sessions."""
        return len(self._pending_sessions)

    def accept_client_session(self, name:str):
        """Accept a pending client session.

        :param name: Name for client session."""

        if self.pending_client_count() < 1:
            return None

        client = self._pending_sessions.pop(0)
        client.set_name(name)
        self._accepted_sessions[name] = client
        return client


class ServerSession:
    def __init__(self, server: Server, sock: socket.SocketType):
        """Constructor.

        :param server: Server instance that owns this session.
        :param sock: ephemeral sock for this session."""
        self._server = server
        self._socket = sock
        self._name = None
        self._parser = simplefix.FixParser()
        self._is_connected = True
        self._queue = []

        asyncio.get_event_loop().add_reader(sock, self.readable)
        return

    def destroy(self):
        if self._is_connected:
            self.disconnect()
        self._queue = []
        return

    def set_name(self, name: str):
        self._name = name
        return

    def readable(self):
        """Handle readable event on session's socket."""
        buf = self._socket.recv(65536)
        if len(buf) == 0:
            self._is_connected = False
            return

        self._parser.append_buffer(buf)
        msg = self._parser.get_message()
        while msg is not None:
            self._queue.append(msg)
            msg = self._parser.get_message()
        return

    def is_connected(self):
        """Return True if session is connected."""
        return self._is_connected

    def disconnect(self):
        """Close this session."""
        asyncio.get_event_loop().remove_reader(self._socket)
        self._socket.close()
        self._socket = None
        self._is_connected = False
        return

    def receive_queue_length(self):
        """Return the number of messages on the received message queue."""
        return len(self._queue)

    def get_message(self):
        """Return the first message from the received message queue."""
        if self.receive_queue_length() < 1:
            return None
        return self._queue.pop(0)

    def send_message(self, message: simplefix.FixMessage):
        """Send a message to the connected client."""
        buffer = message.encode()
        self._socket.sendall(buffer)
        return


class ControlSession:
    """Control client session."""
    def __init__(self, sock: socket.SocketType):
        """Constructor.

        :param sock: Accepted socket."""
        self._socket = sock
        self._buffer = b''
        return

    def append_bytes(self, buffer: bytes):
        """Receive a buffer of bytes from this control client.

        :param buffer: Array of bytes from client."""
        self._buffer += buffer
        if len(self._buffer) <= 4:
            # No payload yet
            return

        payload_length = struct.unpack(b'>L', self._buffer[:4])[0]
        print("payload_length " + str(payload_length))
        if len(buffer) < 4 + payload_length:
            # Not received full message yet
            return
        self._buffer = self._buffer[4:]

        payload = self._buffer[:payload_length]
        print(payload)
        self._buffer = self._buffer[payload_length:]
        return payload

    def send(self, payload: bytes):
        """Send a buffer to the control client.

        :param payload: Array of bytes to send to client."""

        payload_length = len(payload)
        header = struct.pack(">L", payload_length)
        self._socket.sendall(header + payload)
        return

    def close(self):
        """Close this connection."""
        self._socket.close()
        return


class FixToolAgent(object):
    """ """

    def __init__(self):
        """Constructor."""
        self._port = 11011
        self._socket = None
        self._loop = None

        self._control_sessions = {}
        self._clients = {}
        self._servers = {}
        self._server_sessions = {}

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setblocking(False)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(('0.0.0.0', self._port))
        self._socket.listen(5)

        self._loop = asyncio.get_event_loop()
        self._loop.add_reader(self._socket, self.accept)
        return

    def run(self):
        """Enter mainloop."""
        self._loop.run_forever()
        return

    def stop(self):
        """Exit mainloop."""
        self._loop.stop()
        return

    def accept(self):
        """Accept a new control client connection."""
        sock, addr = self._socket.accept()
        self._loop.add_reader(sock, self.readable, sock)
        self._control_sessions[sock] = ControlSession(sock)

        logging.log(logging.INFO, "Accepted control session from %s" % addr[0])
        return

    def readable(self, sock):
        """Handle readable event on a control client socket."""
        logging.log(logging.DEBUG, "Control session readable")
        control_session = self._control_sessions[sock]
        buf = sock.recv(65536)
        if len(buf) == 0:
            self._loop.remove_reader(sock)
            del self._control_sessions[sock]
            control_session.close()
            logging.log(logging.INFO, "Disconnected control session.")
            return

        print(buf)

        payload = control_session.append_bytes(buf)
        while payload is not None:
            message = json.loads(payload)
            self.handle_request(control_session, message)
            payload = None  # FIXME: deal with multiple messages
        return

    def handle_request(self, client, message):
        """Process a received message."""

        message_type = message["type"]
        logging.log(logging.DEBUG, "Dispatching [%s]" % message_type)
        if message_type == "client_create":
            return self.handle_client_create(client, message)

        elif message_type == "client_connect":
            return self.handle_client_connect(client, message)

        elif message_type == "client_is_connected_request":
            return self.handle_client_is_connected_request(client, message)

        elif message_type == "client_destroy":
            return self.handle_client_destroy(client, message)

        elif message_type == "server_create":
            return self.handle_server_create(client, message)

        elif message_type == "server_destroy":
            return self.handle_server_destroy(client, message)

        elif message_type == "server_listen":
            return self.handle_server_listen(client, message)

        elif message_type == "server_unlisten":
            return self.handle_server_unlisten(client, message)

        elif message_type == "server_pending_accept_request":
            return self.handle_server_pending_accept_request(client, message)

        elif message_type == "server_accept":
            return self.handle_server_accept(client, message)

        elif message_type == "server_is_connected_request":
            return self.handle_server_is_connected_request(client, message)

        elif message_type == "server_disconnect":
            return self.handle_server_disconnect(client, message)

        elif message_type == "server_queue_length":
            return

        elif message_type == "server_get_message":
            return

        elif message_type == "server_send_message":
            return

        else:
            return


    def handle_client_create(self, control: ControlSession, message: dict):

        name = message.get("name")
        logging.log(logging.INFO, "client_create(%s)" % name)
        if name in self._clients:
            response = ClientCreatedMessage(name, False,
                                            "Client %s already exists" % name)
            control.send(response.to_json().encode())
            return

        self._clients[name] = Client(name)

        response = ClientCreatedMessage(name, True, '')
        control.send(response.to_json().encode())
        return

    def handle_client_destroy(self, control: ControlSession, message: dict):
        name = message.get("name")
        client = self._clients.get(name)
        if client is None:
            response = ClientDestroyedMessage(name, False,
                                              "No such client '$s'" % name)
            control.send(response.to_json().encode())
            return

        client.destroy()
        del self._clients[name]

        response = ClientDestroyedMessage(name, True, '')
        control.send(response.to_json().encode())
        return

    def handle_client_connect(self, control: ControlSession, message: dict):
        name = message.get("name")
        client = self._clients.get(name)
        if client is None:
            response = ClientConnectedMessage(name, False,
                                              "No such client '$s'" % name)
            control.send(response.to_json().encode())
            return

        client.connect(message.get("host"), message.get("port"))

        response = ClientConnectedMessage(name, True, '')
        control.send(response.to_json().encode())
        return

    def handle_client_is_connected_request(self, control: ControlSession,
                                           message: dict):
        name = message.get("name")
        client = self._clients.get(name)
        if client is None:
            response = ClientIsConnectedResponse(name, False,
                                                 "No such client %s" % name,
                                                 False)
            control.send(response.to_json().encode())
            return

        is_connected = client.is_connected()

        response = ClientIsConnectedResponse(name, True, '', is_connected)
        control.send(response.to_json().encode())
        return

    def handle_server_create(self, client: ControlSession, message: dict):
        """Process a server_create message.

        :param client: Reference to the sending client.
        :param message: """
        name = message.get("name")
        if name in self._servers:
            response = ServerCreatedMessage(name, False,
                                            "Server '%s' already exists" % name)
            client.send(response.to_json().encode())
            return

        # Create server.
        server = Server()

        # Register in table.
        self._servers[name] = server

        # Send reply.
        response = ServerCreatedMessage(name, True, '')
        client.send(response.to_json().encode())
        return

    def handle_server_destroy(self, control: ControlSession, message: dict):
        name = message.get("name")
        server = self._servers.get(name)
        if server is None:
            response = ServerDestroyedMessage(name, False,
                                              "No such server '$s'" % name)
            control.send(response.to_json().encode())
            return

        server.destroy()
        del self._servers[name]

        response = ServerDestroyedMessage(name, True, '')
        control.send(response.to_json().encode())
        return

    def handle_server_listen(self, client: ControlSession, message: dict):
        name = message["name"]
        server = self._servers.get(name)
        if server is None:
            response = ServerListenedMessage(name, False,
                                             "No such server '%s'" % name)
            client.send(response.to_json().encode())
            return

        port = message.get("port")
        if port is None or port < 0 or port > 65535:
            response = ServerListenedMessage(name, False,
                                             "Bad or missing port")
            client.send(response.to_json().encode())
            return

        server.listen(port)

        response = ServerListenedMessage(name, True, '')
        client.send(response.to_json().encode())
        return

    def handle_server_unlisten(self, client: ControlSession, message: dict):
        name = message["name"]
        server = self._servers.get(name)
        if server is None:
            response = ServerUnlistenedMessage(name, False,
                                               "No such server '%s'" % name)
            client.send(response.to_json().encode())
            return

        port = message.get("port")
        if port is None or port < 0 or port > 65535:
            response = ServerUnlistenedMessage(name, False,
                                               "Bad or missing port")
            client.send(response.to_json().encode())
            return

        server.unlisten(port)

        response = ServerUnlistenedMessage(name, True, '')
        client.send(response.to_json().encode())
        return

    def handle_server_pending_accept_request(self,
                                             control: ControlSession,
                                             message: dict):
        name = message["name"]
        server = self._servers.get(name)
        if server is None:
            response = ServerPendingAcceptCountResponse(
                name, False, "No such server '%s'" % name, 0)
            control.send(response.to_json().encode())
            return

        count = server.pending_client_count()
        response = ServerPendingAcceptCountResponse(name, True, '', count)
        control.send(response.to_json().encode())
        return

    def handle_server_accept(self, control: ControlSession, message: dict):
        name = message["name"]
        server = self._servers.get(name)
        if server is None:
            response = ServerAcceptedMessage(
                name, False, "No such server '%s'" % name, '')
            control.send(response.to_json().encode())
            return

        session_name = message.get("session_name")
        session = server.accept_client_session(session_name)
        self._server_sessions[session_name] = session
        response = ServerAcceptedMessage(name, True, '', session_name)
        control.send(response.to_json().encode())
        return

    def handle_server_is_connected_request(self, control: ControlSession,
                                           message: dict):
        name = message.get("name")
        server_session = self._server_sessions.get(name)
        if server_session is None:
            response = ServerIsConnectedResponse(name, False,
                                                 "No such session %s" % name,
                                                 False)
            control.send(response.to_json().encode())
            return

        is_connected = server_session.is_connected()

        response = ServerIsConnectedResponse(name, True, '', is_connected)
        control.send(response.to_json().encode())
        return

    def handle_server_disconnect(self, control: ControlSession, message: dict):
        name = message.get("name")
        server_session = self._server_sessions.get(name)
        if server_session is None:
            response = ServerDisconnectedMessage(name, False,
                                                 "No such session %s" % name)
            control.send(response.to_json().encode())
            return

        server_session.disconnect()

        response = ServerDisconnectedMessage(name, True, '')
        control.send(response.to_json().encode())
        return


def main():
    """Main function for agent."""

    # FIXME: use logging, but write to stdout for systemd.
    logging.basicConfig(level=logging.DEBUG)
    logging.log(logging.INFO, "Starting")

    # FIXME: use similar requests as rnps FIX module?
    # FIXME: use asyncio?  cjson over TCP?
    # FIXME: use type annotations?

    # FIXME: replace all this pidfile malarky with a shutdown to a port number
    pid_file_name = "/tmp/%s-fixtool-agent.pid" % os.environ.get("LOGNAME")

    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "shutdown":
            pid_file = open(pid_file_name)
            pid = int(pid_file.readline())
            os.kill(pid, signal.SIGINT)
            sys.exit(0)

    pid_file = open(pid_file_name, "wb")
    pid_file.write(("%u\n" % os.getpid()).encode())
    pid_file.close()

    try:
        agent = FixToolAgent()
        agent.run()
    finally:
        os.remove(pid_file_name)

    return


if __name__ == "__main__":
    main()


##################################################################
