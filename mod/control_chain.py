#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import socket
from tornado import gen, iostream
from mod import symbolify

CC_MODE_TOGGLE      = 0x01
CC_MODE_TRIGGER     = 0x02
CC_MODE_ENUMERATION = 0x04

# ---------------------------------------------------------------------------------------------------------------------

class ControlChainDeviceListener(object):
    socket_path = "/tmp/control-chain.sock"

    def __init__(self, hw_added_cb, act_added_cb, act_removed_cb):
        self.crashed        = False
        self.idle           = False
        self.initialized    = False
        self.initialized_cb = None
        self.hw_added_cb    = hw_added_cb
        self.act_added_cb   = act_added_cb
        self.act_removed_cb = act_removed_cb
        self.hw_versions    = {}
        self.write_queue    = []

        self.start()

    # -----------------------------------------------------------------------------------------------------------------

    def start(self):
        if not os.path.exists(self.socket_path):
            print("cc start socket missing")
            self.initialized = True
            return

        self.initialized = False

        self.socket = iostream.IOStream(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        self.socket.set_close_callback(self.connection_closed)
        self.socket.set_nodelay(True)

        # put device_list message in queue, so it's handled asap
        self.send_request("device_list", None, self.device_list_init)

        # ready to roll
        self.socket.connect(self.socket_path, self.connection_started)

    def restart_if_crashed(self):
        if not self.crashed:
            return

        self.crashed = False
        self.start()

    def wait_initialized(self, callback):
        if self.initialized:
            callback()
            return

        self.initialized_cb = callback

    # -----------------------------------------------------------------------------------------------------------------

    def connection_started(self):
        if len(self.write_queue):
            self.process_write_queue()
        else:
            self.idle = True

        # FIXME: remove this after we stop forcing arduino-usb
        if os.path.exists("/etc/udev/rules.d/60-arduino.rules") and not os.path.exists("/dev/arduino"):
            self.set_initialized()

    def connection_closed(self):
        print("control-chain closed")
        self.socket  = None
        self.crashed = True
        self.set_initialized()

    def set_initialized(self):
        print("control-chain initialized")
        self.initialized = True

        if self.initialized_cb is not None:
            cb = self.initialized_cb
            self.initialized_cb = None
            cb()

    # -----------------------------------------------------------------------------------------------------------------

    def process_read_queue(self, ignored=None):
        self.socket.read_until(b"\0", self.check_read_response)

    @gen.coroutine
    def check_read_response(self, resp):
        try:
            data = json.loads(resp[:-1].decode("utf-8", errors="ignore"))
        except:
            print("ERROR: control-chain read response failed")
        else:
            if data['event'] == "device_status":
                data   = data['data']
                dev_id = data['device_id']

                if data['status']:
                    yield gen.Task(self.send_device_descriptor, dev_id)

                else:
                    self.act_removed_cb(dev_id)
                    try:
                        self.hw_versions.pop(dev_id)
                    except KeyError:
                        pass

        finally:
            self.process_read_queue()

    # -----------------------------------------------------------------------------------------------------------------

    def process_write_queue(self):
        try:
            to_send, request_name, callback = self.write_queue.pop(0)
        except IndexError:
            self.idle = True
            return

        if self.socket is None:
            self.process_write_queue()
            return

        def check_write_response(resp):
            if callback is not None:
                try:
                    data = json.loads(resp[:-1].decode("utf-8", errors="ignore"))
                except:
                    data = None
                    print("ERROR: control-chain write response failed")
                else:
                    if data is not None and data['reply'] != request_name:
                        print("ERROR: control-chain reply name mismatch")
                        data = None

                if data is not None:
                    callback(data['data'])

            self.process_write_queue()

        self.idle = False
        self.socket.write(to_send)
        self.socket.read_until(b"\0", check_write_response)

    # -----------------------------------------------------------------------------------------------------------------

    def send_request(self, request_name, request_data, callback=None):

        request = {
            'request': request_name,
            'data'   : request_data
        }

        to_send = bytes(json.dumps(request).encode('utf-8')) + b'\x00'
        self.write_queue.append((to_send, request_name, callback))

        if self.idle:
            self.process_write_queue()

    # -----------------------------------------------------------------------------------------------------------------

    @gen.coroutine
    def device_list_init(self, dev_list):
        for dev_id in dev_list:
            yield gen.Task(self.send_device_descriptor, dev_id)

        if not self.initialized:
            self.send_request('device_status', {'enable':1}, self.process_read_queue)

        self.set_initialized()

    # -----------------------------------------------------------------------------------------------------------------

    def send_device_descriptor(self, dev_id, callback):
        def dev_desc_cb(dev):
            dev_label = symbolify(dev['label'])
            dev_uri   = dev['uri']

            if " " in dev_uri:
                print("WARNING: Control Chain device URI '%s' is invalid" % dev_uri)
                callback()
                return

            # assign an unique id starting from 0
            dev_unique_id = 0
            for _dev_uri, _1, _2 in self.hw_versions.items():
                if _dev_uri == dev_uri:
                    dev_unique_id += 1

            self.hw_added_cb(dev_uri, dev_label, dev['version'])
            self.hw_versions[dev_id] = (dev_uri, dev_label, dev['version'])

            for actuator in dev['actuators']:
                modes_int = actuator['supported_modes']
                modes_str = ""

                if modes_int & CC_MODE_TOGGLE:
                    modes_str += ":bypass:toggled"
                if modes_int & CC_MODE_TRIGGER:
                    modes_str += ":trigger"
                if modes_int & CC_MODE_ENUMERATION:
                    modes_str += ":enumeration"

                if not modes_str:
                    continue

                modes_str += ":"

                metadata = {
                    'uri'  : "%s:%i:%i" % (dev_uri, dev_unique_id, actuator['id']),
                    'name' : "%s:%s" % (dev['label'], actuator['name']),
                    'modes': modes_str,
                    'steps': [],
                    'max_assigns': actuator['max_assignments']
                }
                self.act_added_cb(dev_id, actuator['id'], metadata)

            callback()

        self.send_request('device_descriptor', {'device_id':dev_id}, dev_desc_cb)

    # -----------------------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    from tornado.web import Application
    from tornado.ioloop import IOLoop

    def hw_added_cb(dev_uri, label, version):
        print("hw_added_cb", dev_uri, label, version)

    def act_added_cb(dev_id, actuator_id, metadata):
        print("act_added_cb", dev_id, actuator_id, metadata)

    def act_removed_cb(dev_id):
        print("act_removed_cb", dev_id)

    application = Application()
    cc = ControlChainDeviceListener(hw_added_cb, act_added_cb, act_removed_cb)
    IOLoop.instance().start()
