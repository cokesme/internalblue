#!/usr/bin/env python2

from multiprocessing import Process, Queue
from os import popen
from Queue import Empty as QueueEmpty
import re
from time import sleep

from pwn import *

from core import InternalBlue
from hci import HCI_Cmd, HCI_Event
from htresponse import HTResponse


class HTCore(InternalBlue):

    def __init__(self, queue_size=1000, btsnooplog_filename='btsnoop.log', log_level='debug', fix_binutils='True'):
        super(HTCore, self).__init__(queue_size, btsnooplog_filename, log_level, fix_binutils)

        # shift ogf 2 bits to the right
        HCI_Cmd.HCI_CMD_STR = {(((divmod(k, 0x100)[0] >> 2) % pow(2, 8)) << 8) + divmod(k, 0x100)[1]: v for k, v in HCI_Cmd.HCI_CMD_STR.iteritems()}
        HCI_Cmd.HCI_CMD_STR_REVERSE = {v: k for k, v in HCI_Cmd.HCI_CMD_STR.iteritems()}

        # get vsc commands from hci class
        self.init_vsc_variables()

        # wait a few seconds after reattach after crash to check if the device reappeared
        self.sanitycheckonreboot = False
        self.sanitychecksleep = 8

    def device_list(self):
        """
        Return a list of connected hci devices
        """

        response = self._run('hcitool dev').split()

        device_list = []
        # checks if a hci device is connected
        if len(response) > 1 and len(response) % 2 == 1:
            response = response[1:]
            for interface, address in zip(response[0::2], response[1::2]):
                device_list.append([self, interface, 'hci: %s (%s)' % (address, interface)])

        if len(device_list) == 0:
            log.info('No connected hci device found')
            return []
        elif len(device_list) == 1:
            log.info('Found 1 hci devic, %s' % device_list[0][2])
        else:
            log.info('Found multiple hci devices')

        return device_list

    def local_connect(self):
        """
        Start the framework by connecting to the Bluetooth Stack of the Android
        device via adb and the debugging TCP ports.
        """

        if not self.interface:
            log.warn("No hci identifier is set")
            return False

        # Import fw depending on device
        global fw    # put the imported fw into global namespace
        import fw_rpi3 as fw

        self.fw = fw    # Other scripts (such as cmds.py) can use fw through a member variable

        return True

    def _run_async(self, cmd, queue):
        """
        Is called by _run_command and prevents the program not to hang if the bluetooth controller has crashed
        """

        log.debug('Run cmd: %s' % cmd)
        queue.put(popen(cmd).read())

    def _process(self, cmd, queue):
        p = Process(target=self._run_async, args=(cmd, queue,))
        p.start()
        return p

    def _run(self, cmd, timeout=1):
        """
        Runs provided cmd
        """

        # define output queue where hcitool response is passed
        queue = Queue()

        # define and start process
        process = self._process(cmd, queue)

        # check if process hangs (wait 1 second)
        try:
            response = queue.get(True, 1)
            process.join()

            log.debug('Cmd: %s, response: \n%s' % (cmd, response))

            return response

        except QueueEmpty:
            # failed because bluetooth chip crashed
            log.warning('Hci device crashed from cmd: %s', cmd)
            log.info('Reattach device, this will take a few seconds')

            # how many devices? n = devices * 2 + 1
            if self.sanitycheckonreboot:
                n = len(self._run('hcitool dev').split())

            # need to wait a few seconds otherwise command fails
            self._process('sleep 5 && sudo systemctl restart hciuart.service', queue)

            # blocks between 5 and 10 seconds
            queue.get(True)

            if self.sanitycheckonreboot:
                log.info('Check if the device has been reattached, this will take some seoncds')

                sleep(self.sanitychecksleep)

                # check if device is rebooted
                if len(self._run('hcitool dev').split()) != n:
                    log.critical('Could not reboot bluetooth chip, terminating internalblue')

                    exit(-1)

                log.info('device is reattached')

        return False

    def sendHciCommand(self, opcode, data, timeout=1):
        """
        Send an arbitrary HCI packet
        """

        # split opcode into first and second byte
        ogf, ocf = divmod(opcode, 0x100)

        # convert back to hex
        ogf = hex(ogf)
        ocf = hex(ocf)

        data = ' '.join(['0x' + hex(ord(data[i]))[2:].zfill(2) for i in range(len(data))])

        # finalize cmd
        cmd = 'hcitool -i %s cmd %s %s %s' % (self.interface, ogf, ocf, data)

        response = self._run(cmd, timeout)

        if not response or not HTResponse.is_valid(response):
            # something went wrong
            log.critical('Command failed: %s' % cmd)
            return False

        # otherwise return response packet
        event_payload = HTResponse(response).event.payload

        log.info('%s, payload: %s' % (cmd, event_payload))

        return event_payload


class HciCmd(object):

    def __str__(self):
        return "hci command: %s\n" \
               "\topcode: %s (ogf: %s, ocf: %s)\n" \
               "\tplen: %s\n" \
               "\tpayload: %s" \
               % (self.name, self.opcode, self.ogf, self.ocf, self.payload_length, self.payload)

    def __init__(self, ogf, ocf, payload_length, payload):
        self.ogf = ogf
        self.ocf = ocf
        self.payload_length = payload_length
        self.payload = payload

        self.opcode = '0x' + hex((int(ogf, 16) << 8) + int(ocf, 16))[2:].zfill(4)
        self.name = HCI_Cmd.cmd_name(self.opcode)


class HciEvent(object):

    def __str__(self):
        return "hci event: %s\n" \
               "\tcode: %s\n" \
               "\tplen: %s\n" \
               "\tpayload: %s" \
               % (self.name, self.code, self.payload_length, self.payload)

    def __init__(self, code, payload_length, payload):
        self.code = code
        self.payload_length = payload_length
        self.payload = payload

        self.name = HCI_Event.event_name(self.code)


class HTResponse(object):

    hex_pattern = re.compile(r'(0x[0-9a-fA-F]*)')
    plen_pattern = re.compile(r'plen (\d*)')
    payload_pattern = re.compile(r'(?<=\s)[0-9a-fA-F]{2}(?![0-9a-fA-F])')

    @staticmethod
    def is_valid(response):
        """
        Checks if the provided input is a valid hci response
        :param response: response from hcitool cmd ... as string
        :return: boolean
        """

        # convert to lower case
        response = response.lower()

        if response.find('< hci command:') == -1 or response.find('> hci event:') == -1:
            return False

        return True

    def __str__(self):
        return "%s\n%s" % (self.cmd, self.event)

    def __init__(self, ht_response):
        """
        Creates a hcitool response
        :param ht_response: valid response from hcitool cmd ... as string
        """

        self.ht_response = ht_response

        # remove lower case
        ht_response = ht_response.lower()

        ogf, ocf, event_code = re.findall(HTResponse.hex_pattern, ht_response)

        cmd_plen, event_plen = re.findall(HTResponse.plen_pattern, ht_response)
        cmd_plen = int(cmd_plen)
        event_plen = int(event_plen)

        separator = ht_response.find('>')

        command = ' '.join(ht_response[0:separator].split('\n')[1:])
        event = ' '.join(ht_response[separator:].split('\n')[1:])

        cmd_payload = ''.join(re.findall(HTResponse.payload_pattern, command))
        event_payload = ''.join(re.findall(HTResponse.payload_pattern, event))

        self.cmd = HciCmd(
            ogf,
            ocf,
            cmd_plen,
            cmd_payload
        )

        self.event = HciEvent(
            event_code,
            event_plen,
            event_payload
        )

        log.debug(self)

        # if plen and payload does not match log and exit
        # cmd_plen and event_plen are in byte, cmd_payload, event_payload in nibble
        if cmd_plen*2 != len(cmd_payload) or event_plen*2 != len(event_payload):
            log.critical('HCI Command plen %s (%s) or HCI Event plen %s (%s) does not match: \n%s' % (cmd_plen, len(cmd_payload), event_plen, len(event_payload), self))
            exit(-1)
