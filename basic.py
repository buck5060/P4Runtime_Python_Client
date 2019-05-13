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
import datetime
import struct
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
from p4.tmp import p4config_pb2

import helper 

# See https://gist.github.com/carymrobbins/8940382
# functools.partialmethod is introduced in Python 3.4
class partialmethod(partial):
    def __get__(self, instance, owner):
        if instance is None:
            return self
        return partial(self.func, instance,*(self.args or ()), **(self.keywords or {}))

# Used to indicate that the gRPC error Status object returned by the server has
# an incorrect format.
class P4RuntimeErrorFormatException(Exception):
    def __init__(self, message):
        super(P4RuntimeErrorFormatException, self).__init__(message)


# Used to iterate over the p4.Error messages in a gRPC error Status object
class P4RuntimeErrorIterator:
    def __init__(self, grpc_error):
        assert (grpc_error.code() == grpc.StatusCode.UNKNOWN)
        self.grpc_error = grpc_error

        error = None
        # The gRPC Python package does not have a convenient way to access the
        # binary details for the error: they are treated as trailing metadata.
        for meta in self.grpc_error.trailing_metadata():
            if meta[0] == "grpc-status-details-bin":
                error = status_pb2.Status()
                error.ParseFromString(meta[1])
                break
        if error is None:
            raise P4RuntimeErrorFormatException("No binary details field")

        if len(error.details) == 0:
            raise P4RuntimeErrorFormatException(
                "Binary details field has empty Any details repeated field")
        self.errors = error.details
        self.idx = 0

    def __iter__(self):
        return self

    def next(self):
        while self.idx < len(self.errors):
            p4_error = p4runtime_pb2.Error()
            one_error_any = self.errors[self.idx]
            if not one_error_any.Unpack(p4_error):
                raise P4RuntimeErrorFormatException(
                    "Cannot convert Any message to p4.Error")
            if p4_error.canonical_code == code_pb2.OK:
                continue
            v = self.idx, p4_error
            self.idx += 1
            return v
        raise StopIteration


# P4Runtime uses a 3-level message in case of an error during the processing of
# a write batch. This means that if we do not wrap the grpc.RpcError inside a
# custom exception, we can end-up with a non-helpful exception message in case
# of failure as only the first level will be printed. In this custom exception
# class, we extract the nested error message (one for each operation included in
# the batch) in order to print error code + user-facing message.  See P4 Runtime
# documentation for more details on error-reporting.
class P4RuntimeWriteException(Exception):
    def __init__(self, grpc_error):
        assert (grpc_error.code() == grpc.StatusCode.UNKNOWN)
        super(P4RuntimeWriteException, self).__init__()
        self.errors = []
        try:
            error_iterator = P4RuntimeErrorIterator(grpc_error)
            for error_tuple in error_iterator:
                self.errors.append(error_tuple)
        except P4RuntimeErrorFormatException:
            raise  # just propagate exception for now

    def __str__(self):
        message = "Error(s) during Write:\n"
        for idx, p4_error in self.errors:
            code_name = code_pb2._CODE.values_by_number[
                p4_error.canonical_code].name
            message += "\t* At index {}: {}, '{}'\n".format(
                idx, code_name, p4_error.message)
        return message

class P4RuntimeClient():
    def __init__(self, grpc_addr, device_id, device, election_id, role_id, config_path, p4info_path, ctx_json):
        self.grpc_addr = grpc_addr
        if self.grpc_addr is None:
            self.grpc_addr = 'localhost:50051'

        self.device_id = int(device_id)
        if self.device_id is None:
            print("Device ID is not set")

        self.device = device

        self.config_path = config_path
        self.p4info_path = p4info_path
        self.ctx_json_path = ctx_json

        self.channel = grpc.insecure_channel(self.grpc_addr)
        self.stub = p4runtime_pb2.P4RuntimeStub(self.channel)

        print "Importing p4info proto from", p4info_path
        self.p4info = p4info_pb2.P4Info()
        with open(p4info_path, "rb") as fin:
            google.protobuf.text_format.Merge(fin.read(), self.p4info)

        self.p4info_helper = helper.P4InfoHelper(p4info_path)

        self.import_p4info_names()

        # used to store write requests sent to the P4Runtime server, useful for
        # autocleanup of tests (see definition of autocleanup decorator below)
        self.reqs = []

        self.election_id = election_id
        self.role_id = role_id
        self.set_up_stream()

    def import_p4info_names(self):
        self.p4info_obj_map = {}
        suffix_count = Counter()
        for p4_obj_type in ["tables", "action_profiles", "actions", "counters",
                            "direct_counters"]:
            for obj in getattr(self.p4info, p4_obj_type):
                pre = obj.preamble
                suffix = None
                for s in reversed(pre.name.split(".")):
                    suffix = s if suffix is None else s + "." + suffix
                    key = (p4_obj_type, suffix)
                    self.p4info_obj_map[key] = obj
                    suffix_count[key] += 1
        for key, c in suffix_count.items():
            if c > 1:
                del self.p4info_obj_map[key]

    def set_up_stream(self):
        self.stream_out_q = Queue.Queue()
        self.stream_in_q = Queue.Queue()
	self.packetin_rdy = threading.Event()
        def stream_req_iterator():
            while True:
                p = self.stream_out_q.get()
                if p is None:
                    break
                yield p

        def stream_recv(stream):
            for p in stream:
                self.stream_in_q.put(p)
		print(datetime.datetime.now(), "Something is going into queue")
		# self.packetin_rdy.set()

        self.stream = self.stub.StreamChannel(stream_req_iterator())
        self.stream_recv_thread = threading.Thread(
            target=stream_recv, args=(self.stream,))
        self.stream_recv_thread.start()

    def handshake(self, roleconfig = None):
        req = p4runtime_pb2.StreamMessageRequest()
        arbitration = req.arbitration
        arbitration.device_id = self.device_id
        role = arbitration.role
        role.id = self.role_id
        if roleconfig is not None:
           role.config.Pack(roleconfig)
        election_id = arbitration.election_id
        election_id.high = 0
        election_id.low = self.election_id
        self.stream_out_q.put(req)

        rep = self.get_stream_packet("arbitration", timeout=2)
        if rep is None:
            print("Failed to establish handshake")

    def build_bmv2_config(self):
        """
        Builds the device config for BMv2
        """
        device_config = p4config_pb2.P4DeviceConfig()
        device_config.reassign = True
        with open(self.config_path) as f:
            device_config.device_data = f.read()
        return device_config

    def build_tofino_config(self, prog_name, bin_path, cxt_json_path):
        device_config = p4config_pb2.P4DeviceConfig()
        with open(bin_path, 'rb') as bin_f:
            with open(cxt_json_path, 'r') as cxt_json_f:
                device_config.device_data = ""
                device_config.device_data += struct.pack("<i", len(prog_name))
                device_config.device_data += prog_name
                tofino_bin = bin_f.read()
                device_config.device_data += struct.pack("<i", len(tofino_bin))
                device_config.device_data += tofino_bin
                cxt_json = cxt_json_f.read()
                device_config.device_data += struct.pack("<i", len(cxt_json))
                device_config.device_data += cxt_json
        return device_config

    def update_config(self):
        print("Setting Forwarding Pipeline")
        request = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        request.device_id = self.device_id
        election_id = request.election_id
        election_id.high = 0
        election_id.low = self.election_id
        request.role_id = self.role_id
        config = request.config
        with open(self.p4info_path, 'r') as p4info_f:
            google.protobuf.text_format.Merge(p4info_f.read(), config.p4info)
        if self.device == "bmv2":
            device_config = self.build_bmv2_config()
        else:
            device_config = self.build_tofino_config("name", self.config_path, self.ctx_json_path)
        config.p4_device_config = device_config.SerializeToString()
        request.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT
        try:
            response = self.stub.SetForwardingPipelineConfig(request)
        except Exception as e:
            raise
            return False
        return True

    def tearDown(self):
        self.tear_down_stream()

    def tear_down_stream(self):
        self.stream_out_q.put(None)
        self.stream_recv_thread.join()
        sys.exit(0)

    # --- Packet IO ---

    def get_packet_in(self, timeout=2):
        msg = self.get_stream_packet("packet", timeout)
        if msg is None:
            print("Packet in not received\r")
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

    # [TODO] Implement not complete yet
    def send_packet_out(self, packet):
        packet_out_req = p4runtime_pb2.StreamMessageRequest()
        packet_out_req.packet.CopyFrom(packet)
        self.stream_out_q.put(packet_out_req)

   # --- Packet IO End ---

   # --- Role.config ---
    def get_new_roleconfig(self):
        config = p4runtime_pb2.RoleConfig()
        return config

    def add_roleconfig_entry(self, config, table_name, isShared):
        entry = config.entries.add()
        entry.shared = isShared
        table_entry = entry.entity.table_entry
        table_entry.table_id = self.get_table_id(table_name)

   # --- End of Role ---

   # --- Table ---

    def get_obj(self, p4_obj_type, p4_name):
        key = (p4_obj_type, p4_name)
        obj = self.p4info_obj_map.get(key, None)
        if obj is None:
            raise Exception("Unable to find %s '%s' in p4info" % (p4_obj_type, p4_name))
        return obj

    def get_obj_id(self, p4_obj_type, p4_name):
        obj = self.get_obj(p4_obj_type, p4_name)
        return obj.preamble.id

    def get_param_id(self, action_name, param_name):
        a = self.get_obj("actions", action_name)
        for p in a.params:
            if p.name == param_name:
                return p.id
        raise Exception("Param '%s' not found in action '%s'" % (param_name, action_name))

    def get_mf_id(self, table_name, mf_name):
        t = self.get_obj("tables", table_name)
        if t is None:
            return None
        for mf in t.match_fields:
            if mf.name == mf_name:
                return mf.id
        raise Exception("Match field '%s' not found in table '%s'" % (mf_name, table_name))

    # These are attempts at convenience functions aimed at making writing
    # P4Runtime PTF tests easier.

    class MF(object):
        def __init__(self, mf_name):
            self.name = mf_name

    class Exact(MF):
        def __init__(self, mf_name, v):
            super(P4RuntimeClient.Exact, self).__init__(mf_name)
            self.v = v

        def add_to(self, mf_id, mk):
            mf = mk.add()
            mf.field_id = mf_id
            mf.exact.value = self.v

    class Lpm(MF):
        def __init__(self, mf_name, v, pLen):
            super(P4RuntimeClient.Lpm, self).__init__(mf_name)
            self.v = v
            self.pLen = pLen

        def add_to(self, mf_id, mk):
            # P4Runtime mandates that the match field should be omitted for
            # "don't care" LPM matches (i.e. when prefix length is zero)
            if self.pLen == 0:
                return
            mf = mk.add()
            mf.field_id = mf_id
            mf.lpm.prefix_len = self.pLen
            mf.lpm.value = ''

            # P4Runtime now has strict rules regarding ternary matches: in the
            # case of LPM, trailing bits in the value (after prefix) must be set
            # to 0.
            first_byte_masked = self.pLen / 8
            for i in xrange(first_byte_masked):
                mf.lpm.value += self.v[i]
            if first_byte_masked == len(self.v):
                return
            r = self.pLen % 8
            mf.lpm.value += chr(
                ord(self.v[first_byte_masked]) & (0xff << (8 - r)))
            for i in range(first_byte_masked + 1, len(self.v)):
                mf.lpm.value += '\x00'

    class Ternary(MF):
        def __init__(self, mf_name, v, mask):
            super(P4RuntimeClient.Ternary, self).__init__(mf_name)
            self.v = v
            self.mask = mask

        def add_to(self, mf_id, mk):
            # P4Runtime mandates that the match field should be omitted for
            # "don't care" ternary matches (i.e. when mask is zero)
            if all(c == '\x00' for c in self.mask):
                return
            mf = mk.add()
            mf.field_id = mf_id
            assert (len(self.mask) == len(self.v))
            mf.ternary.mask = self.mask
            mf.ternary.value = ''
            # P4Runtime now has strict rules regarding ternary matches: in the
            # case of Ternary, "don't-care" bits in the value must be set to 0
            for i in xrange(len(self.mask)):
                mf.ternary.value += chr(ord(self.v[i]) & ord(self.mask[i]))

    class Range(MF):
        def __init__(self, mf_name, low, high):
            super(P4RuntimeClient.Range, self).__init__(mf_name)
            self.low = low
            self.high = high

        def add_to(self, mf_id, mk):
            # P4Runtime mandates that the match field should be omitted for
            # "don't care" range matches (i.e. when all possible values are
            # included in the range)
            # TODO(antonin): negative values?
            low_is_zero = all(c == '\x00' for c in self.low)
            high_is_max = all(c == '\xff' for c in self.high)
            if low_is_zero and high_is_max:
                return
            mf = mk.add()
            mf.field_id = mf_id
            assert (len(self.high) == len(self.low))
            mf.range.low = self.low
            mf.range.high = self.high


    # Sets the match key for a p4::TableEntry object. mk needs to be an iterable
    # object of MF instances
    def set_match_key(self, table_entry, t_name, mk):
       for mf in mk:
           if "role_id" in mf.name:
               if ord(mf.v) != self.role_id and self.role_id is not 0:
                   print("Wrong Role_ID in flow rule!!")
                   return
           mf_id = self.get_mf_id(t_name, mf.name)
           mf.add_to(mf_id, table_entry.match)

    def set_action(self, action, a_name, params):
       action.action_id = self.get_action_id(a_name)
       for p_name, v in params:
           param = action.params.add()
           param.param_id = self.get_param_id(a_name, p_name)
           param.value = v

    # Sets the action & action data for a p4::TableEntry object. params needs to
    # be an iterable object of 2-tuples (<param_name>, <value>).
    def set_action_entry(self, table_entry, a_name, params):
        self.set_action(table_entry.action.action, a_name, params)

    def get_new_write_request(self):
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self.device_id
        req.role_id = self.role_id
        election_id = req.election_id
        election_id.high = 0
        election_id.low = self.election_id
        return req

    def push_update_add_entry_to_action(self, req, t_name, mk, a_name, params, priority):
        update = req.updates.add()
        update.type = p4runtime_pb2.Update.INSERT
        table_entry = update.entity.table_entry
        table_entry.table_id = self.get_table_id(t_name)
        table_entry.priority = priority
        if mk is None or len(mk) == 0:
            table_entry.is_default_action = True
        else:
            self.set_match_key(table_entry, t_name, mk)
        self.set_action_entry(table_entry, a_name, params)

    def write_request(self, req, store=True):
        rep = self._write(req)
        if store:
            self.reqs.append(req)
        return rep

    def _write(self, req):
        try:
            return self.stub.Write(req)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNKNOWN:
                raise e
        raise P4RuntimeWriteException(e)

    def ReadTableEntries(self, table_id=None, dry_run=False):
        request = p4runtime_pb2.ReadRequest()
        request.device_id = self.device_id
        request.role_id = self.role_id
        entity = request.entities.add()
        table_entry = entity.table_entry
        if table_id is not None:
            table_entry.table_id = table_id
        else:
            table_entry.table_id = 0
        if dry_run:
            print "P4Runtime Read:", request
        else:
            for response in self.stub.Read(request):
                yield response

   # --- Table End ---


# Add p4info object and object id "getters" for each object type; these are just
# wrappers around P4RuntimeTest.get_obj and P4RuntimeTest.get_obj_id.
# For example: get_table(x) and get_table_id(x) respectively call
# get_obj("tables", x) and get_obj_id("tables", x)
for obj_type, nickname in [("tables", "table"),
                           ("action_profiles", "ap"),
                           ("actions", "action"),
                           ("counters", "counter"),
                           ("direct_counters", "direct_counter")]:
    name = "_".join(["get", nickname])
    setattr(P4RuntimeClient, name, partialmethod(
        P4RuntimeClient.get_obj, obj_type))
    name = "_".join(["get", nickname, "id"])
    setattr(P4RuntimeClient, name, partialmethod(
    P4RuntimeClient.get_obj_id, obj_type))
