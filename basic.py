# Copyright 2013-present Barefoot Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Author:
# Antonin Bas (antonin@barefootnetworks.com)
#
# Modify by Buck Chung (buck5060@gmail.com)

import Queue
import sys
import threading
import time
from StringIO import StringIO
from collections import Counter
from functools import wraps, partial
from unittest import SkipTest

import google.protobuf.text_format
import grpc
import scapy.packet
import scapy.utils
from google.rpc import status_pb2, code_pb2
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2


class P4RuntimeClient():
    def __init__(self, grpc_addr, device_id , cpu_port):
        self.grpc_addr = grpc_addr
        if self.grpc_addr is None:
            self.grpc_addr = 'localhost:50051'

        self.device_id = int(device_id)
        if self.device_id is None:
            print("Device ID is not set")

        self.cpu_port = int(cpu_port)
        if self.cpu_port is None:
            print("CPU port is not set")

        self.channel = grpc.insecure_channel(self.grpc_addr)
        self.stub = p4runtime_pb2.P4RuntimeStub(self.channel)

        # used to store write requests sent to the P4Runtime server, useful for
        # autocleanup of tests (see definition of autocleanup decorator below)
        self.reqs = []

        self.election_id = 1
        self.set_up_stream()

    def set_up_stream(self):
        self.stream_out_q = Queue.Queue()
        self.stream_in_q = Queue.Queue()

        def stream_req_iterator():
            while True:
                p = self.stream_out_q.get()
                if p is None:
                    break
                yield p

        def stream_recv(stream):
            for p in stream:
                self.stream_in_q.put(p)

        self.stream = self.stub.StreamChannel(stream_req_iterator())
        self.stream_recv_thread = threading.Thread(
            target=stream_recv, args=(self.stream,))
        self.stream_recv_thread.start()

        self.handshake()

    def handshake(self):
        req = p4runtime_pb2.StreamMessageRequest()
        arbitration = req.arbitration
        arbitration.device_id = self.device_id
        election_id = arbitration.election_id
        election_id.high = 0
        election_id.low = self.election_id
        self.stream_out_q.put(req)
	
        rep = self.get_stream_packet("arbitration", timeout=2)
        if rep is None:
            print("Failed to establish handshake")

    def tearDown(self):
        self.tear_down_stream()

    def tear_down_stream(self):
        self.stream_out_q.put(None)
        self.stream_recv_thread.join()
	sys.exit(0)
	

    def get_packet_in(self, timeout=2):
        msg = self.get_stream_packet("packet", timeout)
        if msg is None:
            print("Packet in not received")
        else:
	    print("Packet mon Getcha !")
            return msg.packet

    def get_stream_packet(self, type_, timeout=1):
        start = time.time()
        try:
            while True:
                remaining = timeout - (time.time() - start)
                if remaining < 0:
                    break
                msg = self.stream_in_q.get(timeout=remaining)
                if not msg.HasField(type_):
                    continue
                return msg
        except:  # timeout expired
            pass
        return None

    def send_packet_out(self, packet):
        packet_out_req = p4runtime_pb2.StreamMessageRequest()
        packet_out_req.packet.CopyFrom(packet)
        self.stream_out_q.put(packet_out_req)

