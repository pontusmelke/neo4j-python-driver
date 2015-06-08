#!/usr/bin/env python
#! -*- encoding: UTF-8 -*-

# Copyright (c) 2002-2015 "Neo Technology,"
# Network Engine for Objects in Lund AB [http://neotechnology.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import deque
from io import BytesIO
import logging
import struct
import sys

from ..session import SocketSession
from .chunking import ChunkedIO
from .error import CypherError
from .packstream import Packer, Unpacker
from .types import Record


__all__ = ["SocketSessionV1"]


# Signature bytes for each message type
INIT = b"\x01"             # 0000 0001 // INIT
ACK_FAILURE = b"\x0F"      # 0000 1111 // ACK_FAILURE
RUN = b"\x10"              # 0001 0000 // RUN <statement> <parameters>
DISCARD_ALL = b"\x2F"      # 0010 1111 // DISCARD *
PULL_ALL = b"\x3F"         # 0011 1111 // PULL *
SUCCESS = b"\x70"          # 0111 0000 // SUCCESS <metadata>
RECORD = b"\x71"           # 0111 0001 // RECORD <value>
IGNORED = b"\x7E"          # 0111 1110 // IGNORED <metadata>
FAILURE = b"\x7F"          # 0111 1111 // FAILURE <metadata>

# Textual names for each message type (for logging)
message_names = {
    INIT: "INIT",
    ACK_FAILURE: "ACK_FAILURE",
    RUN: "RUN",
    DISCARD_ALL: "DISCARD *",
    PULL_ALL: "PULL *",
    SUCCESS: "SUCCESS",
    RECORD: "RECORD",
    IGNORED: "IGNORED",
    FAILURE: "FAILURE",
}

# Set of response messages classed as "summary" messages
summary_signatures = {SUCCESS, FAILURE, IGNORED}


log = logging.getLogger("neo4j")


if sys.version_info >= (3,):

    def ustr(s, encoding="utf-8"):
        """ Convert argument to unicode string.
        """
        if isinstance(s, str):
            return s
        try:
            return s.decode(encoding)
        except AttributeError:
            return str(s)

else:

    def ustr(s, encoding="utf-8"):
        """ Convert argument to unicode string.
        """
        if isinstance(s, str):
            return s.decode(encoding)
        else:
            return unicode(s)


class SocketSessionV1(SocketSession):
    """ Session client for Neo4j Data Protocol V1.
    """
    version = 1

    def __init__(self, s):
        super(SocketSessionV1, self).__init__(s)
        #if __debug__:
        #    log.info("~ Connected (V1)")

        self.responses = deque()

        read = self._read
        responses = self.responses

        def incoming():
            """ Generator function for incoming messages. Each call
            yields the next available message as a (signature, args)
            tuple. Each call will also block until a message is
            available.
            """
            raw = BytesIO()
            unpack = Unpacker(raw).unpack
            response = responses.popleft()
            while True:
                # Mark position in the raw byte buffer
                position = raw.tell()
                # Read chunks of data until chunk_size == 0
                chunk_size = -1
                while chunk_size != 0:
                    # Read chunk size from two-byte header
                    chunk_header = read(2, StopIteration)
                    chunk_size, = struct.unpack_from(">H", chunk_header)
                    # Read chunk data
                    if chunk_size > 0:
                        chunk_data = read(chunk_size, StopIteration)
                        raw.write(chunk_data)
                # Rewind to the marked position
                raw.seek(position)
                # Unpack message structures from the raw byte stream and yield
                for signature, args in unpack():
                    # Append this message to the current response
                    response.append((signature, args))
                    #if __debug__:
                    #    log.info("< %s %s", message_names[signature], " ".join(map(repr, args)))
                    yield signature, args
                    # If this is a summary message, begin the next response
                    if signature in summary_signatures:
                        response = responses.popleft()

        # Incoming message generator instance
        self.incoming = incoming()

        # Initialise the connection
        self.init("neo4j-python/1.0")

    def close(self):
        """ Close the connection.
        """
        super(SocketSessionV1, self).close()
        #if __debug__:
        #    log.info("~ Closed (V1)")

    def _send(self, *messages):
        """ Send one or more request messages to the server.

        :param messages: the messages to send
        :return: tuple of response lists, one per outgoing message
        """
        raw = ChunkedIO()
        packer = Packer(raw)
        responses = []
        for signature, args in messages:
            # Pack each message structure in turn
            packer.pack_struct_header(len(args), signature)
            for arg in args:
                packer.pack(arg)
            # Followed by a zero chunk
            raw.flush(zero_chunk=True)
            #if __debug__:
            #    log.info("> %s %s", message_names[signature], " ".join(map(repr, args)))
            # Create a response message list for this request
            responses.append(deque())
        # Write all the data items
        self._write(raw.getvalue())
        # Append all the new response message lists
        self.responses += responses
        # Return the response message lists as a tuple
        return tuple(responses)

    def _receive(self, response):
        """ Yield each message for a given response list, in turn.

        :param response: the response list to receive
        :return:
        """
        incoming = self.incoming
        # Keep reading until we get something for this response
        while not response:
            next(incoming)
        # Yield everything we have received already
        for message in response:
            yield message
        # Carry on reading and yielding until we hit a summary message
        while response[-1][0] not in summary_signatures:
            message = next(incoming)
            yield message

    def _receive_summary(self, response):
        """ Receive all messages for a given response list, up to and
        including the final summary message.

        :param response: the response list to receive
        :return: the summary message as a (signature, args) tuple
        """
        incoming = self.incoming
        # Keep reading until we get something for this response
        while not response:
            next(incoming)
        # Carry on reading until we hit a summary message
        while response[-1][0] not in summary_signatures:
            next(incoming)
        # Return the last (summary) response
        return response[-1]

    def init(self, user_agent):
        """ Initialise a connection with the details supplied.

        :param user_agent: The user agent to register for this connection
        """
        # Send RUN and PULL_ALL messages
        init_response, = self._send((INIT, (ustr(user_agent),)))
        # Receive the summary message from the RUN request
        signature, (metadata,) = self._receive_summary(init_response)
        if signature == SUCCESS:
            return
        elif signature == FAILURE:
            # TODO: ACK_FAILURE
            raise CypherError(metadata["message"])
        else:
            raise RuntimeError("Cypher error")

    def run(self, statement, parameters=None):
        """ Run a Cypher statement and return the response records.

        :param statement:
        :param parameters:
        :return:
        """
        record_signature = RECORD
        # Send RUN and PULL_ALL messages
        run_response, pull_response = self._send(
            (RUN, (ustr(statement), dict(parameters or {}))),
            (PULL_ALL, ())
        )
        # Receive the summary message from the RUN request
        signature, (metadata,) = self._receive_summary(run_response)
        if signature == SUCCESS:
            # Extract the fields for the upcoming records
            keys = metadata["fields"]
            # Return a list of all result records
            return [Record(keys, values)
                    for signature, (values,) in self._receive(pull_response)
                    if signature == record_signature]
        elif signature == FAILURE:
            # TODO: ACK_FAILURE
            raise CypherError(metadata["message"])
        else:
            raise RuntimeError("Cypher error")