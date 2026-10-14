'''
Demo program, shows use of kklib (access to native K+K library)
Read reports from K+K Device

TODO:	Configure program sequence and variables

Sets output path to _path.

Uses debug log file depending on _error_log.

Shows use of enumerating calls

Sets decimal separator to _dec_sep
    
Connects to K+K device via _connection with _blocking
    
Runs local TCP server depending on _start_local_server, _port

Generates binary file with test data depending on _create_bin_data
    
Reads reports
Send commands to adjust report mode, report interval and channel count
Processes special reports: version string, FHR settings


Created on 18.11.2021

2420-12-04	added: request connected user, reset device

@author: Loryn Brendes, lb@brendes.de
'''

import sys
import time
import os
from datetime import datetime

from kklib import NativeLib, NativeLibError, FHRSettings
from kklib import ErrorCode
from kklib import DebugLogType

print("ReportReceiver start")

# TODO config settings:

# set your own value in _path
#
_path = "."+os.sep+"Debug"

# keep all debug files
#_error_only = False
# keep only debug files with error(s)
_error_only = True

# decimal separator english
_dec_sep = "."
# decimal separator german
#_dec_sep = ","

# Please enter connection string
#
"""Some examples
a) network connection to K+K device - needs firewall settings
_connection = "192.168.178.98"  
b) network connection to K+K TCP server running on localhost, port 48896
_connection = "TCP:127.0.0.1:48896"
c) K+K device connected to serial port
connection = "COM1"  
d) K+K device with nickname connected to USB
_connection = <nickname>
e) network connection to K+K device via DHCP assigned ip using host name
_connection = "HOST:/FXE2-021D"
"""
# usb device name (assigned nickname)
#_connection = 'KK-FXE2'

# host name
_connection = "HOST:/FXE2-021D"

# Blocking I/O -> get_report returns with data or after timeout
_blocking = True
# without blocking I/O -> get_report returns immediately
#_blocking = False

# do not generate binary test data
_create_bin_data = False
# generate binary test data
#_create_bin_data = True

# do not start local server
_start_local_server = False
# start local server
#_start_local_server = True

# Please enter port number for local server
#
_port = 48896

# Limit report counts: end while-get_report-loop
_empty_string_limit = 50
_report_limit = 100

# Test reset device
_reset_device = True

try:
    # load K+K library
    _kknative = NativeLib();
except NativeLibError as exc:
    # stop execution
    sys.exit(str(exc))    
    
# show version
print("K+K library version "+_kknative.get_version())

# get source ID
_source_id = _kknative.get_source_id()

# set output path to _path
kkres = _kknative.set_output_path(_source_id, _path)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_output_path: unexpected Error")
    print("set_output_path: "+kkres.data)
else:
    print("set_output_path: ok")
# check it, not neccessary, for demo only
kkres = _kknative.get_output_path(_source_id)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("get_output_path: unexpected Error")
print("get_output_path: "+kkres.data)

# DebugLog
# report and low level log
kkres = _kknative.set_debug_flags(_source_id, True, True)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_flags failed: "+kkres.data)
kkres = _kknative.set_debug_log_limit(_source_id, 
                                      DebugLogType.LOG_CREATE_NEW, 
                                      0)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_log_limit failed: "+kkres.data)
kkres = _kknative.set_debug_log(_source_id, True, None, _error_only)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_log failed: "+kkres.data)
else:
    kkres = _kknative.get_debug_filename(_source_id)
    print("debug filename: "+kkres.data)

"""
# DebugStream: full log
kkres = _kknative.set_debug_stream(_source_id, True, None, False)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_stream failed: "+kkres.data)
else:
    kkres = _kknative.get_debug_filename(_source_id)
    print("debug filename: "+kkres.data)
"""

# test enumerate ips of local computer
l = []
kkres = _kknative.get_host_and_IPs(l)
if kkres.result_code == ErrorCode.KK_ERR:
    print("get_host_and_IPs failed: "+kkres.data)
else:
    print("get local host name and IP addresses: ")
first_ = True
for s in l:
    if first_:
        print("host: "+s)
        first_ = False
    else:
        print("IP: "+s)

# test enumerate devices
kkres = _kknative.enumerate_Devices(NativeLib.ENUM_FLAG_LOCAL_DEVICES)
if kkres.data is None:
    print("no devices enumerated")
else:
    print("COM ports and local K+K USB devices: "+kkres.data)

# set decimal separator
kkres = _kknative.set_decimal_separator(_source_id, _dec_sep)
if kkres.data != None:
    print("set decimal separator failed: "+kkres.data)

# open connection
print("open connection "+_connection)
kkres = _kknative.open_connection(_source_id, _connection, _blocking)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    # stop execution
    sys.exit("open_connection failed: "+kkres.data)

# assigned user id (-1 if not connected to K+K device)
print("user id:",_kknative.get_user_id(_source_id))

# start local server
#
if _start_local_server:
    kkres = _kknative.start_TCP_server(_source_id, _port)
    if kkres.result_code != ErrorCode.KK_NO_ERR:
        print("start local TCP server failed: "+kkres.data)
    else:
        print("local TCP server started on port:", kkres.int_value)

# define some commands
l = [0x81]      
cmdVersion  = bytes(l)  # firmware version, clear buffer
l = [0x41]
cmdPhaseAvg = bytes(l)  # report mode phase average
l = [0x43]
cmdFreqAvg  = bytes(l)  # report mode frequency average
l = [0x29]
cmdRate1s   = bytes(l)  # report interval 1s
l = [0x26]
cmdRate100ms= bytes(l)  # report interval 100ms
l = [0x34]
cmdChannel4 = bytes(l)  # limit reports to 4 channels
            
waitHeader = 0  # do not wait

# start with version command (including clear buffer)
kkres = _kknative.send_command(_source_id, cmdVersion)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("send command(FirmwareVersion) failed: "+kkres.data)
else:
    print("Cmd version pending commands count:",_kknative.get_pending_commands_count(_source_id))
    # wait for command response = 0x7001
    waitHeader = 0x7001
    print("wait for version string")

# receive reports and check command results
print("get string reports...")
end = False
emptyStringCounter = 0
reportCounter = 0
while (not end):
    kkres = _kknative.get_report(_source_id)
    # examine result code
    if kkres.result_code == ErrorCode.KK_ERR_BUFFER_OVERFLOW:
        print("get_report reports overflow error")
        end = True
    elif kkres.result_code == ErrorCode.KK_ERR_SERVER_DOWN:
        print("get_report reports no connection to TCP server")
        end = True
    elif kkres.result_code == ErrorCode.KK_ERR_WRITE:
        print("get_report reports write error")
        end = True
    elif kkres.result_code == ErrorCode.KK_ERR_DEVICE_NOT_CONNECTED:
        print("get_report reports unconnected device")
        end = True
    elif kkres.result_code == ErrorCode.KK_HARDWARE_FAULT:
        print("get_report reports hardware fault")
        end = True
    elif ((kkres.result_code != ErrorCode.KK_NO_ERR) and 
          (kkres.result_code != ErrorCode.KK_ERR_BUFFER_TOO_SMALL)):
        print("get_report failed: "+kkres.data)
        end = True
    elif kkres.data is None:
        # no data
        emptyStringCounter += 1
        if emptyStringCounter > _empty_string_limit:
            end = True
            print("no more data -> stop")
        else:
            print("no data emptyCount=", emptyStringCounter)
            time.sleep(0.1)		# 100ms
    else :
        #report header
        subS = kkres.data[:4]
        aHeader = int(subS, 16) # HEX!
        # ignore empty message (7000)
        if aHeader != 0x7000:
            print(kkres.data)
            reportCounter += 1
            # header measurement data
            # < 0x2000: phase data
            # < 0x4000: frequency data
            # < 0x6000: phase diff data

            # transmit measurement reports to TCP server
            if aHeader < 0x2000:
                _kknative.report_TCP_log(_source_id, ''+kkres.data[5:], 'PHASELOG')
            elif aHeader < 0x4000:
                _kknative.report_TCP_log(_source_id, ''+kkres.data[5:], 'FREQLOG')
            elif aHeader < 0x6000:
                _kknative.report_TCP_log(_source_id, ''+kkres.data[5:], 'PHASEDIFFLOG')
                
            # process special reports
            s = kkres.data
            if aHeader == 0x7001:
                # version string
                if waitHeader == 0x7001:
                    waitHeader = 0
                # request connected user (-> 0x7030 report)
                print("request connected user")
                kkres = _kknative.request_connected_user(_source_id)
                if kkres.result_code != ErrorCode.KK_NO_ERR:
                    print("request connected user failed: "+kkres.data)

                fw = _kknative.get_firmware_version(_source_id)
                print("Firmware version "+str(fw))
                if fw >= 67:
                    # ask for FHR settings
                    kkres = _kknative.get_FHR_settings(_source_id)
                    if kkres.result_code == ErrorCode.KK_NO_ERR:
                        waitHeader = 0x7902
                        print("wait for FHR settings")
                    else:
                        print("get FHR settings failed")
            elif aHeader == 0x7030:
                print("User connections of device: "+kkres.data)
            elif aHeader == 0x7902:
                # FHR settings -> delete first 5 character
                if waitHeader == 0x7902:
                    waitHeader = 0
                sFhr = s[5:]
                fhrSettings = FHRSettings()
                if fhrSettings.from_string(sFhr):
                    print('FHR settings received')
                else:
                    print('Failed to parse FHR settings '+sFhr)
            elif aHeader < 0x7000:
                # check FXE measurement data
                if (aHeader < 0x1000) or (aHeader >= 0x2000):
                    if (waitHeader == 0):
                        # switch to phase average
                        kkres = _kknative.send_command(_source_id, cmdPhaseAvg)
                        if kkres.result_code != ErrorCode.KK_NO_ERR:
                            print("send command(Mode PhaseAvg) failed: "+kkres.data)
                        else:
                            print("Cmd PhaseAvg pending commands count:",_kknative.get_pending_commands_count(_source_id))
                            waitHeader = 0x1000
                    # else waiting for another report
                elif (aHeader >= 0x1000) and (aHeader < 0x2000):
                    # phase average data, check interval
                    if (waitHeader == 0x1000):
                        waitHeader = 0
                    # interval 1s
                    if (aHeader < 0x1900) or (aHeader >= 0x1A00):
                        if (waitHeader == 0):
                            # switch to 1s
                            kkres = _kknative.send_command(_source_id, cmdRate1s)
                            if kkres.result_code != ErrorCode.KK_NO_ERR:
                                print("send command(Rate 1s) failed: "+kkres.data)
                            else:
                                print("Cmd 1s pending commands count:",_kknative.get_pending_commands_count(_source_id))
                                waitHeader = 0x1900
                        # else waiting for another report
                    elif (aHeader >= 0x1900) and (aHeader < 0x1A00):
                        # that's it
                        if waitHeader == 0x1900:
                            waitHeader = 0
                            print('desired report mode and interval received')
                        # limit to 4 channels
                        l_ = kkres.data.split(';')
                        if (len(l_) > 4):
                            if (waitHeader == 0):
                                print("limit to 4 channels")
                                kkres = _kknative.send_command(_source_id, cmdChannel4)
                                if kkres.result_code != ErrorCode.KK_NO_ERR:
                                    print("send command(Channels 4) failed: "+kkres.data)
                                else:
                                    print("Cmd Channels 4 pending commands count:",_kknative.get_pending_commands_count(_source_id))
                                    waitHeader = 4
                            # else cmd already sent
                        else:
                            if waitHeader == 4:
                                print("Channel count limited to 4")
                                waitHeader = 0
                    
            # generate binary test data
            if _create_bin_data and (reportCounter == 30):
                kkres = _kknative.start_save_binary_data(_source_id, None)
                if kkres.result_code != ErrorCode.KK_NO_ERR:
                    print("generate binary test data failed: "+kkres.data)
                else:
                    print("generate binary test data")
                
        # reset counter
        emptyStringCounter = 0

    if _reset_device and (reportCounter == 80):
        print("Reset device")
        _reset_device = False
        kkres = _kknative.reset_device(_source_id)
        if kkres.result_code != ErrorCode.KK_NO_ERR:
            print("reset device failed: "+kkres.data)
    if reportCounter > _report_limit:
        end = True
        print('more than', _report_limit, 'reports received, stop now')
        
print("get reports...finished")

# binary test data
if _create_bin_data:
    kkres = _kknative.stop_save_binary_data(_source_id)
    if kkres.result_code != ErrorCode.KK_NO_ERR:
        print("stop binary test data failed: "+kkres.data)
    
# stop local server
if _start_local_server:
    kkres = _kknative.stop_TCP_server(_source_id)
    if kkres.result_code != ErrorCode.KK_NO_ERR:
        print("stop local TCP server failed: "+kkres.data)
    else:
        print("local TCP server stopped")

# close connection
print("close connection")
_kknative.close_connection(_source_id)

print("ReportReceiver END")
